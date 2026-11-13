import base64
import binascii
import hmac
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.errors import AppError, not_found
from app.models import (
    Annotation,
    Block,
    Document,
    Finding,
    FindingEvidence,
    Job,
    Metric,
    Proposition,
    Recommendation,
    Scan,
    Source,
    SourceProfile,
)
from app.llm_usage import cost_by_generation, usage_summary
from app.pagination import Page, page_params, paginate
from app.pipeline import create_reanalysis
from app.ratelimit import guard_report_build, guard_report_pdf
from app.reports import build_report_v2, render_html_v2
from app.reports.artifacts import get_or_render_pdf
from app.reports.model_v2 import json_appendix
from app.review import (
    REGISTER_CONFIG,
    create_open_question,
    decide_stage_gate,
    entity_dict,
    event_dict,
    list_register_page,
    patch_open_question,
    patch_register,
    review_scan,
    update_client_brief,
)
from app.scans import add_website_source, create_scan, get_scan, retry_scan, run_scan
from app.schemas import (
    ClientBriefUpdate,
    OpenQuestionCreate,
    OpenQuestionPatch,
    ReanalysisCreate,
    RegisterPatch,
    ReviewAction,
    ScanCreate,
    StageGateDecision,
    WebsiteSourceCreate,
)
from app.registers.models import OpenQuestion, ReviewEvent

router = APIRouter()


def require_access(authorization: str | None = Header(default=None)) -> None:
    expected = get_settings().app_access_token
    valid = False
    basic = False
    if authorization and authorization.startswith("Bearer "):
        valid = hmac.compare_digest(authorization[7:], expected)
    elif authorization and authorization.startswith("Basic "):
        basic = True
        try:
            decoded = base64.b64decode(authorization[6:]).decode()
            username, password = decoded.split(":", 1)
            valid = username == "audit" and hmac.compare_digest(password, expected)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            valid = False
    if not valid:
        headers = {"WWW-Authenticate": 'Basic realm="StoryAudit"'} if not authorization or basic else None
        raise AppError(401, "unauthorized", "Valid access token required", headers=headers)


protected = APIRouter(dependencies=[Depends(require_access)])


def scan_dict(scan: Scan) -> dict[str, Any]:
    return {
        "id": str(scan.id), "title": scan.title, "status": scan.status,
        "analysis_generation": scan.analysis_generation, "corpus_hash": scan.corpus_hash,
        "methodology_version": scan.methodology_version, "error_summary": scan.error_summary,
        "audit_phase": scan.audit_phase, "audit_mode": scan.audit_mode,
        "client_brief": scan.client_brief,
        "created_at": scan.created_at, "completed_at": scan.completed_at,
    }


def job_dict(job: Job) -> dict[str, Any]:
    return {
        "id": str(job.id), "agent": job.agent, "status": job.status, "job_type": job.job_type,
        "object_type": job.object_type, "object_id": str(job.object_id),
        "attempt_count": job.attempt_count, "max_attempts": job.max_attempts,
        "analysis_generation": job.analysis_generation,
        "last_error_type": job.last_error_type, "last_error_message": job.last_error_message,
    }


@protected.post("/scans", status_code=201)
def post_scan(data: ScanCreate, session: Session = Depends(get_db)) -> dict[str, Any]:
    return scan_dict(create_scan(session, data))


@protected.get("/scans")
def list_scans(
    response: Response,
    request: Request,
    page: Page = Depends(page_params),
    session: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows, total = paginate(
        session, select(Scan).order_by(Scan.created_at.desc()), page
    )
    response.headers.update(page.headers(request, total))
    return [scan_dict(scan) for scan in rows]


@protected.get("/scans/{scan_id}")
def get_scan_status(
    scan_id: uuid.UUID,
    page: Page = Depends(page_params),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    scan = get_scan(session, scan_id)
    jobs, jobs_total = paginate(
        session, select(Job).where(Job.scan_id == scan.id).order_by(Job.created_at), page
    )
    return {
        **scan_dict(scan),
        "jobs": [job_dict(job) for job in jobs],
        "jobs_total": jobs_total,
        "jobs_limit": page.limit,
        "jobs_offset": page.offset,
    }


@protected.get("/scans/{scan_id}/jobs")
def get_jobs(
    scan_id: uuid.UUID,
    response: Response,
    request: Request,
    page: Page = Depends(page_params),
    session: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    get_scan(session, scan_id)
    rows, total = paginate(
        session, select(Job).where(Job.scan_id == scan_id).order_by(Job.created_at), page
    )
    response.headers.update(page.headers(request, total))
    return [job_dict(job) for job in rows]


@protected.post("/scans/{scan_id}/sources", status_code=201)
def post_source(scan_id: uuid.UUID, data: WebsiteSourceCreate, session: Session = Depends(get_db)) -> dict[str, Any]:
    """MVP: the only corpus source is a website fetched by URL."""
    source = add_website_source(session, scan_id, data)
    return {
        "id": str(source.id), "scan_id": str(source.scan_id), "name": source.name,
        "type": source.type, "external_id": source.external_id,
        "pages": source.source_metadata.get("pages", 0),
        "role": source.source_metadata.get("role", "client"),
        "selection_reason": source.source_metadata.get("selection_reason"),
    }


@protected.post("/scans/{scan_id}/run", status_code=202)
def post_run(scan_id: uuid.UUID, session: Session = Depends(get_db)) -> dict[str, Any]:
    return scan_dict(run_scan(session, scan_id))


@protected.post("/scans/{scan_id}/retry", status_code=202)
def post_retry(scan_id: uuid.UUID, session: Session = Depends(get_db)) -> dict[str, Any]:
    return scan_dict(retry_scan(session, scan_id))


@protected.post("/scans/{scan_id}/reanalysis", status_code=202)
def post_reanalysis(scan_id: uuid.UUID, data: ReanalysisCreate, session: Session = Depends(get_db)) -> dict[str, Any]:
    return create_reanalysis(session, scan_id, data)


@protected.get("/scans/{scan_id}/profile")
def get_profiles(scan_id: uuid.UUID, session: Session = Depends(get_db)) -> list[dict[str, Any]]:
    get_scan(session, scan_id)
    profiles = session.scalars(select(SourceProfile).where(SourceProfile.scan_id == scan_id, SourceProfile.is_current.is_(True)))
    return [{"source_id": str(profile.source_id), **profile.payload} for profile in profiles]


@protected.get("/scans/{scan_id}/metrics")
def get_metrics(scan_id: uuid.UUID, session: Session = Depends(get_db)) -> list[dict[str, Any]]:
    get_scan(session, scan_id)
    metrics = session.scalars(select(Metric).where(Metric.scan_id == scan_id, Metric.is_current.is_(True)).order_by(Metric.code))
    return [{"id": str(metric.id), "code": metric.code, "value": float(metric.value), "formula_version": metric.formula_version} for metric in metrics]


@protected.get("/scans/{scan_id}/annotations")
def get_annotations(
    scan_id: uuid.UUID,
    response: Response,
    request: Request,
    page: Page = Depends(page_params),
    session: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    get_scan(session, scan_id)
    annotations, total = paginate(
        session,
        select(Annotation).where(Annotation.scan_id == scan_id).order_by(Annotation.created_at.desc()),
        page,
    )
    response.headers.update(page.headers(request, total))
    return [{
        "id": str(item.id), "agent": item.agent, "object_type": item.object_type,
        "object_id": str(item.object_id), "analysis_generation": item.analysis_generation,
        "is_current": item.is_current, "supersedes_annotation_id": str(item.supersedes_annotation_id) if item.supersedes_annotation_id else None,
        "payload": item.payload,
    } for item in annotations]


def metric_view(scan_id: uuid.UUID, metric_id: uuid.UUID, session: Session) -> dict[str, Any]:
    get_scan(session, scan_id)
    metric = session.get(Metric, metric_id)
    if not metric or metric.scan_id != scan_id or not metric.is_current:
        raise not_found("Metric")
    findings = list(session.scalars(select(Finding).where(Finding.metric_id == metric.id, Finding.is_current.is_(True))))
    finding_rows = []
    for finding in findings:
        proposition_ids = list(session.scalars(select(FindingEvidence.proposition_id).where(FindingEvidence.finding_id == finding.id)))
        evidence = []
        for proposition_id in proposition_ids:
            row = session.execute(
                select(Proposition, Block, Document, Source)
                .join(Block, Proposition.block_id == Block.id)
                .join(Document, Block.document_id == Document.id)
                .join(Source, Document.source_id == Source.id)
                .where(Proposition.id == proposition_id)
            ).one()
            proposition, block, document, source = row
            evidence.append({
                "proposition": {"id": str(proposition.id), "text": proposition.text},
                "block": {"id": str(block.id), "ordinal": block.ordinal},
                "document": {"id": str(document.id), "title": document.title, "url": document.url},
                "source": {"id": str(source.id), "name": source.name},
            })
        recommendation = session.scalar(select(Recommendation).where(Recommendation.finding_id == finding.id))
        finding_rows.append({
            "id": str(finding.id), "code": finding.code, "severity": finding.severity,
            "title": finding.title, "explanation": finding.explanation, "evidence": evidence,
            "recommendation": None if not recommendation else {
                "title": recommendation.title, "action": recommendation.action,
                "expected_effect": recommendation.expected_effect, "priority": recommendation.priority,
            },
        })
    return {
        "metric": {"id": str(metric.id), "code": metric.code, "value": float(metric.value), "version": metric.formula_version},
        "breakdown": metric.breakdown,
        "findings": finding_rows,
    }


@protected.get("/scans/{scan_id}/report", response_class=HTMLResponse)
def get_report(scan_id: uuid.UUID, type: str | None = None, session: Session = Depends(get_db)) -> HTMLResponse:
    """The single client report (report_v2). `?type=` is accepted and ignored."""
    document = build_report_v2(session, scan_id)
    headers = {}
    if type is not None:
        headers = {"Deprecation": "true", "Link": f'</scans/{scan_id}/report>; rel="successor-version"'}
    return HTMLResponse(render_html_v2(document), headers=headers)


@protected.get("/scans/{scan_id}/report.pdf")
def get_report_pdf(scan_id: uuid.UUID, type: str | None = None, session: Session = Depends(get_db)) -> Response:
    guard_report_pdf(scan_id)
    document = build_report_v2(session, scan_id)
    pdf, cache_hit = get_or_render_pdf(session, scan_id, document)
    filename = f"narrative-audit-{scan_id}.pdf"
    headers = {
        "Content-Disposition": f'inline; filename="{filename}"',
        "Cache-Control": "no-store",
        "ETag": f'"{document.meta.content_hash}"',
        "X-Report-Cache": "HIT" if cache_hit else "MISS",
    }
    if type is not None:
        headers["Deprecation"] = "true"
    return Response(content=pdf, media_type="application/pdf", headers=headers)


@protected.get("/scans/{scan_id}/report.json")
def get_report_json(scan_id: uuid.UUID, session: Session = Depends(get_db)) -> dict[str, Any]:
    document = build_report_v2(session, scan_id)
    payload = document.model_dump(mode="json")
    payload["appendix"] = json_appendix(document)
    return payload


@protected.post("/scans/{scan_id}/report/build")
def post_report_build(scan_id: uuid.UUID, session: Session = Depends(get_db)) -> dict[str, Any]:
    guard_report_build(scan_id)
    document = build_report_v2(session, scan_id, persist=True)
    return {
        "report_version": document.meta.report_version,
        "report_id": document.meta.report_id,
        "report_status": document.meta.report_status,
        "content_hash": document.meta.content_hash,
        "coverage": document.coverage.model_dump(),
    }


@protected.get("/scans/{scan_id}/usage")
def get_usage(scan_id: uuid.UUID, session: Session = Depends(get_db)) -> dict[str, Any]:
    """Token and cost accounting for every LLM request made by this scan."""
    get_scan(session, scan_id)
    return usage_summary(session, scan_id) | {
        "by_generation": cost_by_generation(session, scan_id)
    }


@protected.get("/scans/{scan_id}/registers")
def get_registers(scan_id: uuid.UUID, session: Session = Depends(get_db)) -> dict[str, Any]:
    get_scan(session, scan_id)
    return {
        name: {"label": config["label"], "fields": list(config["fields"])}
        for name, config in REGISTER_CONFIG.items()
    }


@protected.get("/scans/{scan_id}/registers/{register}")
def get_register(
    scan_id: uuid.UUID,
    register: str,
    response: Response,
    request: Request,
    page: Page = Depends(page_params),
    session: Session = Depends(get_db),
):
    rows, total = list_register_page(
        session, scan_id, register, limit=page.limit, offset=page.offset
    )
    response.headers.update(page.headers(request, total))
    return rows


@protected.patch("/scans/{scan_id}/registers/{register}/{entity_id}")
def patch_register_entry(
    scan_id: uuid.UUID,
    register: str,
    entity_id: uuid.UUID,
    data: RegisterPatch,
    session: Session = Depends(get_db),
):
    return patch_register(session, scan_id, register, entity_id, data)


@protected.get("/scans/{scan_id}/review-events")
def get_review_events(
    scan_id: uuid.UUID,
    response: Response,
    request: Request,
    page: Page = Depends(page_params),
    session: Session = Depends(get_db),
):
    get_scan(session, scan_id)
    rows, total = paginate(
        session,
        select(ReviewEvent)
        .where(ReviewEvent.scan_id == scan_id)
        .order_by(ReviewEvent.created_at.desc()),
        page,
    )
    response.headers.update(page.headers(request, total))
    return [event_dict(row) for row in rows]


@protected.get("/scans/{scan_id}/client-brief")
def get_client_brief(scan_id: uuid.UUID, session: Session = Depends(get_db)) -> dict[str, Any]:
    return {"brief": get_scan(session, scan_id).client_brief or {}}


@protected.put("/scans/{scan_id}/client-brief")
def put_client_brief(
    scan_id: uuid.UUID, data: ClientBriefUpdate, session: Session = Depends(get_db)
):
    return update_client_brief(session, scan_id, data)


@protected.get("/scans/{scan_id}/open-questions")
def get_open_questions(scan_id: uuid.UUID, session: Session = Depends(get_db)):
    get_scan(session, scan_id)
    rows = session.scalars(
        select(OpenQuestion).where(OpenQuestion.scan_id == scan_id).order_by(OpenQuestion.ordinal)
    )
    return [entity_dict(row) for row in rows]


@protected.post("/scans/{scan_id}/open-questions", status_code=201)
def post_open_question(
    scan_id: uuid.UUID, data: OpenQuestionCreate, session: Session = Depends(get_db)
):
    return create_open_question(session, scan_id, data)


@protected.patch("/scans/{scan_id}/open-questions/{question_id}")
def patch_question(
    scan_id: uuid.UUID,
    question_id: uuid.UUID,
    data: OpenQuestionPatch,
    session: Session = Depends(get_db),
):
    return patch_open_question(session, scan_id, question_id, data)


@protected.post("/scans/{scan_id}/review")
def post_review(
    scan_id: uuid.UUID, data: ReviewAction, session: Session = Depends(get_db)
):
    return review_scan(session, scan_id, data)


@protected.post("/scans/{scan_id}/stage-gate")
def post_stage_gate(
    scan_id: uuid.UUID, data: StageGateDecision, session: Session = Depends(get_db)
):
    return decide_stage_gate(session, scan_id, data)


@protected.get("/scans/{scan_id}/metrics/{metric_id}")
def get_metric(scan_id: uuid.UUID, metric_id: uuid.UUID, session: Session = Depends(get_db)) -> dict[str, Any]:
    return metric_view(scan_id, metric_id, session)


def scan_view(scan_id: uuid.UUID, session: Session, *, jobs_limit: int = 200) -> dict[str, Any]:
    """UI view of a scan. Jobs are paginated: a real scan creates 700+ of them."""
    data = get_scan_status(scan_id, page=Page(limit=jobs_limit, offset=0), session=session)
    data["sources"] = list(session.scalars(select(Source).where(Source.scan_id == scan_id).order_by(Source.name)))
    data["profiles"] = get_profiles(scan_id, session)
    data["metrics"] = get_metrics(scan_id, session)
    data["usage"] = usage_summary(session, scan_id)
    # Counted in SQL: the truncated jobs list must not change the retry decision.
    data["can_retry"] = not session.scalar(
        select(func.count(Job.id)).where(
            Job.scan_id == scan_id,
            Job.analysis_generation == data["analysis_generation"],
            Job.job_type != "initial",
        )
    )
    return data
