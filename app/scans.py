import re
import uuid
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import AppError, conflict, not_found
from app.models import Block, CalculationRun, Document, Job, Scan, Source
from app.queue import enqueue
from app.ratelimit import guard_run, guard_scan_create, guard_source_add
from app.schemas import ScanCreate, SourceCreate, WebsiteSourceCreate
from app.utils import stable_hash, text_hash
from app.website import crawl_site


def get_scan(session: Session, scan_id: uuid.UUID, lock: bool = False) -> Scan:
    query = select(Scan).where(Scan.id == scan_id)
    if lock:
        query = query.with_for_update()
    scan = session.scalar(query)
    if not scan:
        raise not_found("Scan")
    return scan


def create_scan(session: Session, data: ScanCreate) -> Scan:
    guard_scan_create()
    settings = get_settings()
    fast_model, _ = settings.model_for_agent("A2")
    smart_model, _ = settings.model_for_agent("NARRATIVE")
    scan = Scan(
        title=data.title.strip(),
        # Annotations record the model that actually wrote the client-facing text.
        model_version=smart_model,
        config_snapshot={
            "formula_version": "formulas_v1",
            "top_n": 3,
            "llm_provider": settings.llm_provider,
            "llm_model_fast": fast_model,
            "llm_model_smart": smart_model,
            "annotation_batch_size": settings.annotation_batch_size,
            "prompt_version": "prompt_v1",
        },
    )
    session.add(scan)
    session.commit()
    return scan


MAX_DOCUMENTS = 100
MAX_BLOCKS = 5000


def _check_draft(scan: Scan) -> None:
    if scan.status != "draft":
        raise conflict("Sources can only be added to a draft scan")


def _add_document(session: Session, source: Source, title: str, url: str | None, text: str) -> int:
    """Persist one document and its blocks; returns the number of blocks."""
    document = Document(source_id=source.id, title=title.strip(), url=url, text=text, content_hash=text_hash(text))
    session.add(document)
    session.flush()
    cursor = 0
    ordinal = 0
    for raw in re.split(r"(?:\r?\n){2,}", text):
        chunk = raw.strip()
        if not chunk:
            continue
        start = text.find(chunk, cursor)
        if start < 0:
            start = text.find(chunk)
        end = start + len(chunk)
        cursor = end
        session.add(Block(document_id=document.id, ordinal=ordinal, text=chunk, start_offset=start, end_offset=end))
        ordinal += 1
    if ordinal == 0:
        raise AppError(422, "validation_error", "Documents must contain non-empty text")
    return ordinal


def add_source(session: Session, scan_id: uuid.UUID, data: SourceCreate) -> Source:
    scan = get_scan(session, scan_id)
    _check_draft(scan)
    existing_docs = len(session.scalars(select(Document.id).join(Source).where(Source.scan_id == scan.id)).all())
    if existing_docs + len(data.documents) > MAX_DOCUMENTS:
        raise AppError(422, "validation_error", "A scan can contain at most 100 documents")
    source = Source(scan_id=scan.id, name=data.name.strip(), type=data.type)
    session.add(source)
    session.flush()
    block_total = 0
    for item in data.documents:
        block_total += _add_document(session, source, item.title, item.url, item.text)
    if block_total > MAX_BLOCKS:
        raise AppError(422, "validation_error", "A scan can contain at most 5000 blocks")
    session.commit()
    return source


def add_website_source(session: Session, scan_id: uuid.UUID, data: WebsiteSourceCreate) -> Source:
    """Fetch a site by URL and store its pages as documents.

    `role="competitor"` feeds the narrative landscape: the number of
    alternatives is capped, a selection reason is mandatory, pages per site are
    limited and requests are rate-limited. robots.txt is always honoured.
    """
    guard_source_add(scan_id)
    settings = get_settings()
    scan = get_scan(session, scan_id)
    _check_draft(scan)

    role = data.role
    if role == "competitor":
        if not (data.selection_reason or "").strip():
            raise AppError(422, "validation_error", "Для альтернативы укажите критерий включения")
        competitors = [
            source for source in session.scalars(select(Source).where(Source.scan_id == scan.id))
            if (source.source_metadata or {}).get("role") == "competitor"
        ]
        if len(competitors) >= settings.landscape_max_competitors:
            raise AppError(
                422, "validation_error",
                f"Не более {settings.landscape_max_competitors} альтернатив на один аудит",
            )
        pages_budget = min(data.max_pages, settings.landscape_max_pages)
        delay = settings.landscape_fetch_delay_seconds
    else:
        pages_budget = data.max_pages
        delay = settings.website_fetch_delay_seconds

    pages = crawl_site(
        data.url, pages_budget,
        respect_robots=settings.website_respect_robots,
        delay_seconds=delay,
    )
    if not pages:
        raise AppError(422, "validation_error", "Сайт не содержит текста для анализа")

    existing_docs = len(session.scalars(select(Document.id).join(Source).where(Source.scan_id == scan.id)).all())
    if existing_docs + len(pages) > MAX_DOCUMENTS:
        raise AppError(422, "validation_error", "A scan can contain at most 100 documents")

    parsed = urlparse(pages[0].url)
    name = (data.name or parsed.hostname or pages[0].url).strip()
    metadata = {
        "entry_url": pages[0].url,
        "pages": len(pages),
        "role": role,
    }
    if role == "competitor":
        metadata.update({
            "selection_reason": data.selection_reason.strip(),
            "robots_checked": settings.website_respect_robots,
            "fetch_delay_seconds": delay,
        })
    source = Source(
        scan_id=scan.id,
        name=name,
        type="website",
        external_id=parsed.hostname,
        source_metadata=metadata,
    )
    session.add(source)
    session.flush()

    block_total = 0
    for page in pages:
        block_total += _add_document(session, source, page.title, page.url, page.text)
    if block_total > MAX_BLOCKS:
        raise AppError(422, "validation_error", "A scan can contain at most 5000 blocks")
    session.commit()
    return source


def corpus_payload(session: Session, scan_id: uuid.UUID) -> list[dict]:
    sources = list(session.scalars(select(Source).where(Source.scan_id == scan_id).order_by(Source.id)))
    result = []
    for source in sources:
        documents = list(session.scalars(select(Document).where(Document.source_id == source.id).order_by(Document.id)))
        result.append({
            "id": str(source.id),
            "name": source.name,
            "documents": [
                {
                    "id": str(doc.id),
                    "content_hash": doc.content_hash,
                    "blocks": [
                        {"id": str(block.id), "ordinal": block.ordinal, "text": block.text}
                        for block in session.scalars(select(Block).where(Block.document_id == doc.id).order_by(Block.ordinal))
                    ],
                }
                for doc in documents
            ],
        })
    return result


def run_scan(session: Session, scan_id: uuid.UUID) -> Scan:
    guard_run(scan_id)
    scan = get_scan(session, scan_id, lock=True)
    if scan.status != "draft":
        raise conflict("Only a draft scan can be run")
    payload = corpus_payload(session, scan.id)
    if not payload or not any(source["documents"] for source in payload):
        raise AppError(422, "validation_error", "Cannot run an empty scan")
    scan.corpus_hash = stable_hash(payload)
    scan.analysis_generation = 1
    scan.status = "queued"
    scan.audit_phase = "analyzing"
    scan.error_summary = None
    enqueue(session, scan, "A0", "scan", scan.id, generation=1, snapshot={"corpus_hash": scan.corpus_hash})
    session.commit()
    return scan


def retry_scan(session: Session, scan_id: uuid.UUID) -> Scan:
    scan = get_scan(session, scan_id, lock=True)
    if scan.status != "failed":
        raise conflict("Only a failed scan can be retried")
    generation = scan.analysis_generation + 1
    failed = list(session.scalars(select(Job).where(Job.scan_id == scan.id, Job.analysis_generation == scan.analysis_generation, Job.status == "failed")))
    if any(job.job_type != "initial" for job in failed):
        raise conflict("A failed reanalysis must be restarted with a new client_request_id")
    if not failed:
        raise conflict("No failed jobs to retry")
    first_generation = min(job.analysis_generation for job in failed)
    failed_agents = {job.agent for job in failed if job.analysis_generation == first_generation}
    if failed_agents.intersection({"A2", "A3", "A4"}):
        failed_agents = {"A2", "A3", "A4"}
    retryable = list(session.scalars(select(Job).where(
        Job.scan_id == scan.id,
        Job.analysis_generation == first_generation,
        Job.agent.in_(failed_agents),
        Job.status.in_(["failed", "cancelled"]),
    )))
    scan.analysis_generation = generation
    scan.status = "queued"
    scan.error_summary = None
    for old in retryable:
        enqueue(
            session, scan, old.agent, old.object_type, old.object_id,
            generation=generation,
            snapshot=old.input_snapshot,
            key=f"{scan.id}:{old.agent}:{old.object_type}:{old.object_id}:{generation}:{scan.prompt_version}",
        )
    session.commit()
    return scan


def new_calculation_run(session: Session, scan: Scan, generation: int, input_hash: str) -> CalculationRun:
    run = CalculationRun(scan_id=scan.id, analysis_generation=generation, config_hash=stable_hash(scan.config_snapshot), input_hash=input_hash)
    session.add(run)
    session.flush()
    return run
