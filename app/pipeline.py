import uuid
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import AppError, conflict
from app.models import (
    Block,
    CalculationRun,
    Document,
    Job,
    Proposition,
    ReanalysisRequest,
    Scan,
    Source,
    now,
)
from app.queue import enqueue
from app.registers.models import Product
from app.scans import get_scan, new_calculation_run
from app.schemas import ReanalysisCreate
from app.utils import stable_hash


def source_for_proposition(session: Session, proposition_id: uuid.UUID) -> Source:
    source = session.scalar(
        select(Source)
        .join(Document, Document.source_id == Source.id)
        .join(Block, Block.document_id == Document.id)
        .join(Proposition, Proposition.block_id == Block.id)
        .where(Proposition.id == proposition_id)
    )
    if not source:
        raise AppError(404, "not_found", "Proposition not found")
    return source


def _initial_jobs(session: Session, scan_id: uuid.UUID, generation: int, agents: list[str]) -> list[Job]:
    return list(session.scalars(select(Job).where(
        Job.scan_id == scan_id,
        Job.analysis_generation == generation,
        Job.job_type == "initial",
        Job.agent.in_(agents),
    )))


def _done(jobs: list[Job]) -> bool:
    return all(job.status == "succeeded" for job in jobs)


def _sources(session: Session, scan: Scan) -> list[Source]:
    return list(session.scalars(select(Source).where(Source.scan_id == scan.id).order_by(Source.id)))


def _enqueue_for_sources(session: Session, scan: Scan, generation: int, agent: str) -> None:
    for source in _sources(session, scan):
        enqueue(session, scan, agent, "source", source.id, generation=generation)


FINAL_AGENTS = {
    "A8b", "VERIFY", "PRODUCT", "EVIDENCE", "LANGUAGE",
    "ACTANT", "JOURNEY", "CONSISTENCY", "FINDINGS", "CRITIC",
    "SCORECARD", "RECOMMEND", "NARRATIVE", "LANDSCAPE",
}


def _finalize(session: Session, scan: Scan) -> None:
    if scan.status == "succeeded":
        return
    scan.status = "succeeded"
    scan.completed_at = now()
    if scan.audit_phase in {"draft", "ingesting", "analyzing"}:
        scan.audit_phase = "registers_ready"
    session.flush()


def _finalize_if_ready(session: Session, scan: Scan, job: Job) -> None:
    """A scan succeeds only when no job of its generation is left unfinished.

    This is also what keeps the report consistent: registers, not the legacy
    metric chain, decide when the scan is complete.
    """
    if job.job_type != "initial":
        if job.agent == "A8b":
            _finalize(session, scan)
        return
    counts = dict(session.execute(
        select(Job.status, func.count(Job.id))
        .where(
            Job.scan_id == scan.id,
            Job.analysis_generation == job.analysis_generation,
            Job.job_type == "initial",
        )
        .group_by(Job.status)
    ).all())
    if not counts:
        return
    if counts.get("pending") or counts.get("running") or counts.get("failed") or counts.get("cancelled"):
        return
    _finalize(session, scan)


def _dispatch(session: Session, scan: Scan, job: Job) -> None:
    generation = job.analysis_generation
    if job.agent == "A0":
        documents = list(session.scalars(
            select(Document).join(Source).where(Source.scan_id == scan.id).order_by(Document.id)
        ))
        for document in documents:
            enqueue(session, scan, "A1", "document", document.id, generation=generation)
        return
    if job.agent == "A1":
        if not _done(_initial_jobs(session, scan.id, generation, ["A1"])):
            return
        # Registers do not depend on the per-proposition legacy agents.
        _enqueue_for_sources(session, scan, generation, "VERIFY")
        enqueue(session, scan, "PRODUCT", "scan", scan.id, generation=generation)
        for source in session.scalars(select(Source).where(Source.scan_id == scan.id)):
            if (source.source_metadata or {}).get("role") == "competitor":
                enqueue(session, scan, "LANDSCAPE", "source", source.id, generation=generation)
        propositions = list(session.scalars(
            select(Proposition)
            .join(Block, Proposition.block_id == Block.id)
            .join(Document, Block.document_id == Document.id)
            .join(Source, Document.source_id == Source.id)
            .where(Source.scan_id == scan.id)
            .order_by(Proposition.id)
        ))
        if not propositions:
            _enqueue_for_sources(session, scan, generation, "A5")
            return
        batch_size = max(1, get_settings().annotation_batch_size)
        if batch_size == 1:
            for proposition in propositions:
                for agent in ("A2", "A3", "A4"):
                    enqueue(session, scan, agent, "proposition", proposition.id, generation=generation)
            return
        # Chunked fan-out: one job (one provider call) per `annotation_batch_size`
        # propositions. Annotations are still stored per proposition.
        for start in range(0, len(propositions), batch_size):
            chunk = propositions[start : start + batch_size]
            snapshot = {"proposition_ids": [str(item.id) for item in chunk]}
            for agent in ("A2", "A3", "A4"):
                enqueue(
                    session, scan, agent, "proposition_batch", chunk[0].id,
                    generation=generation, snapshot=snapshot,
                )
        return
    if job.agent in {"A2", "A3", "A4"}:
        jobs = _initial_jobs(session, scan.id, generation, ["A2", "A3", "A4"])
        if _done(jobs):
            _enqueue_for_sources(session, scan, generation, "A5")
        return
    if job.agent == "A5":
        if _done(_initial_jobs(session, scan.id, generation, ["A5"])):
            _enqueue_for_sources(session, scan, generation, "PROFILE")
        return
    if job.agent == "PROFILE":
        if _done(_initial_jobs(session, scan.id, generation, ["PROFILE"])):
            _enqueue_for_sources(session, scan, generation, "A6")
        return
    if job.agent == "A6":
        if _done(_initial_jobs(session, scan.id, generation, ["A6"])):
            enqueue(session, scan, "A7", "scan", scan.id, generation=generation)
        return
    if job.agent == "A7":
        run = session.scalar(select(CalculationRun).where(
            CalculationRun.scan_id == scan.id,
            CalculationRun.analysis_generation == generation,
        ))
        enqueue(session, scan, "A8a", "scan", scan.id, generation=generation, snapshot={"calculation_run_id": str(run.id)})
        return
    if job.agent == "A8a":
        enqueue(session, scan, "A8b", "scan", scan.id, generation=generation, snapshot=job.input_snapshot)
        return
    if job.agent == "VERIFY":
        if _done(_initial_jobs(session, scan.id, generation, ["VERIFY"])):
            _enqueue_for_sources(session, scan, generation, "EVIDENCE")
        return
    if job.agent == "EVIDENCE":
        if _done(_initial_jobs(session, scan.id, generation, ["EVIDENCE"])):
            enqueue(session, scan, "LANGUAGE", "scan", scan.id, generation=generation)
            products = list(session.scalars(
                select(Product).where(Product.scan_id == scan.id).order_by(Product.code)
            ))
            for product in products:
                enqueue(session, scan, "ACTANT", "product", product.id, generation=generation)
            enqueue(session, scan, "JOURNEY", "scan", scan.id, generation=generation)
        return
    if job.agent in {"ACTANT", "JOURNEY"}:
        agents = ("ACTANT", "JOURNEY", "EVIDENCE")
        if all(_done(_initial_jobs(session, scan.id, generation, [agent])) for agent in agents):
            enqueue(session, scan, "CONSISTENCY", "scan", scan.id, generation=generation)
        return
    if job.agent == "CONSISTENCY":
        enqueue(session, scan, "FINDINGS", "scan", scan.id, generation=generation)
        return
    if job.agent == "FINDINGS":
        enqueue(session, scan, "CRITIC", "scan", scan.id, generation=generation)
        return
    if job.agent == "CRITIC":
        _maybe_scorecard(session, scan, generation)
        return
    if job.agent == "LANDSCAPE":
        _maybe_scorecard(session, scan, generation)
        return
    if job.agent == "SCORECARD":
        enqueue(session, scan, "RECOMMEND", "scan", scan.id, generation=generation)
        return
    if job.agent == "RECOMMEND":
        enqueue(session, scan, "NARRATIVE", "scan", scan.id, generation=generation)
        return


def _maybe_scorecard(session: Session, scan: Scan, generation: int) -> None:
    """Scorecard waits for both the findings critic and the landscape.

    `_done([])` is true, so the critic jobs must exist before the barrier can
    open; otherwise the landscape (which finishes long before the findings
    chain) would start the scorecard early and recommendations would be built
    without findings.
    """
    critic_jobs = _initial_jobs(session, scan.id, generation, ["CRITIC"])
    if not critic_jobs or not _done(critic_jobs):
        return
    landscape_jobs = _initial_jobs(session, scan.id, generation, ["LANDSCAPE"])
    if landscape_jobs and not _done(landscape_jobs):
        return
    enqueue(session, scan, "SCORECARD", "scan", scan.id, generation=generation)


def schedule_after_success(session: Session, job: Job) -> None:
    scan = session.get(Scan, job.scan_id)
    if not scan or scan.analysis_generation != job.analysis_generation:
        return
    if scan.status in {"failed", "cancelled"}:
        return
    if job.job_type != "initial":
        _finalize_if_ready(session, scan, job)
        return
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"schedule:{scan.id}:{job.analysis_generation}"})
    _dispatch(session, scan, job)
    _finalize_if_ready(session, scan, job)


def create_reanalysis(session: Session, scan_id: uuid.UUID, data: ReanalysisCreate) -> dict[str, Any]:
    scan = get_scan(session, scan_id, lock=True)
    if scan.status not in {"succeeded", "failed"}:
        raise conflict("Reanalysis requires a completed or failed scan")
    allowed = {
        "A2": "proposition", "A3": "proposition", "A4": "proposition",
        "A6": "source", "A7": "scan",
    }
    if data.agent not in allowed or data.object_type != allowed[data.agent]:
        raise AppError(422, "validation_error", "Unsupported agent/object scope")
    existing = session.scalar(select(ReanalysisRequest).where(
        ReanalysisRequest.scan_id == scan.id,
        ReanalysisRequest.client_request_id == data.client_request_id,
    ))
    identity = (data.agent, data.object_type, data.object_id, data.issue_id)
    if existing:
        if (existing.agent, existing.object_type, existing.object_id, existing.issue_id) != identity:
            raise conflict("client_request_id was already used with another payload")
        jobs = list(session.scalars(select(Job).where(Job.reanalysis_request_id == existing.id)))
        return {"request_id": str(existing.id), "analysis_generation": existing.analysis_generation, "job_ids": [str(job.id) for job in jobs]}

    source: Source | None = None
    if data.object_type == "proposition":
        source = source_for_proposition(session, data.object_id)
        if source.scan_id != scan.id:
            raise AppError(404, "not_found", "Object not found in scan")
    elif data.object_type == "source":
        source = session.get(Source, data.object_id)
        if not source or source.scan_id != scan.id:
            raise AppError(404, "not_found", "Source not found in scan")
    elif data.object_id != scan.id:
        raise AppError(422, "validation_error", "Scan scope must use scanId as object_id")

    generation = scan.analysis_generation + 1
    scan.analysis_generation = generation
    scan.status = "queued"
    scan.completed_at = None
    scan.error_summary = None
    request = ReanalysisRequest(
        scan_id=scan.id,
        client_request_id=data.client_request_id,
        issue_id=data.issue_id,
        agent=data.agent,
        object_type=data.object_type,
        object_id=data.object_id,
        analysis_generation=generation,
    )
    session.add(request)
    session.flush()
    input_hash = stable_hash(data.model_dump(mode="json"))
    calculation = new_calculation_run(session, scan, generation, input_hash)
    snapshot = {"calculation_run_id": str(calculation.id), "input_hash": input_hash}

    chain: list[tuple[str, str, uuid.UUID]] = [(data.agent, data.object_type, data.object_id)]
    if data.agent in {"A2", "A3", "A4"}:
        assert source is not None
        chain.extend([("A5", "source", source.id), ("PROFILE", "source", source.id), ("A6", "source", source.id)])
    if data.agent != "A7":
        chain.append(("A7", "scan", scan.id))
    chain.extend([("A8a", "scan", scan.id), ("A8b", "scan", scan.id)])

    parent: Job | None = None
    jobs: list[Job] = []
    for agent, object_type, object_id in chain:
        key = f"{scan.id}:{agent}:{object_type}:{object_id}:{data.issue_id}:{generation}:{scan.prompt_version}"
        job = enqueue(
            session, scan, agent, object_type, object_id,
            generation=generation,
            job_type="reanalysis",
            issue_id=data.issue_id,
            request_id=request.id,
            snapshot=snapshot,
            key=key,
            depends_on=[parent.id] if parent else None,
        )
        jobs.append(job)
        parent = job
    session.commit()
    return {"request_id": str(request.id), "analysis_generation": generation, "job_ids": [str(job.id) for job in jobs]}
