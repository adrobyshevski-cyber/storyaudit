import json
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import metric_view, require_access, scan_view
from app.db import get_db
from app.errors import AppError
from app.models import Scan
from app.pipeline import create_reanalysis
from app.registers.models import OpenQuestion, StageGate
from app.reports import build_report_v2, render_html_v2
from app.reports.artifacts import get_or_render_pdf
from app.review import (
    BRIEF_FIELDS,
    REGISTER_CONFIG,
    create_open_question,
    decide_stage_gate,
    list_register,
    list_review_events,
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

ui = APIRouter(prefix="/ui", dependencies=[Depends(require_access)], include_in_schema=False)
templates = Jinja2Templates(directory="app/templates")


def render(request: Request, name: str, **context):
    context.setdefault("error", request.query_params.get("error"))
    return templates.TemplateResponse(request=request, name=name, context=context)


def redirect_to(url: str, error: str | None = None) -> RedirectResponse:
    separator = "&" if "?" in url else "?"
    target = f"{url}{separator}error={quote(error)}" if error else url
    return RedirectResponse(target, status_code=303)


def _review_rows(scan_id: uuid.UUID, register: str, session: Session) -> list[dict]:
    rows = []
    for data in list_register(session, scan_id, register):
        fields = []
        for name in REGISTER_CONFIG[register]["fields"]:
            value = data.get(name)
            kind = "text"
            if isinstance(value, bool):
                kind = "bool"
            elif isinstance(value, (list, dict)):
                kind = "json"
            elif name == "score":
                kind = "number"
            fields.append({
                "name": name,
                "kind": kind,
                "value": json.dumps(value, ensure_ascii=False) if kind == "json" else value,
            })
        rows.append({"data": data, "fields": fields})
    return rows


@ui.get("/scans", response_class=HTMLResponse)
def scans_page(request: Request, session: Session = Depends(get_db)):
    scans = list(session.scalars(select(Scan).order_by(Scan.created_at.desc()).limit(100)))
    return render(request, "scans.html", scans=scans)


@ui.post("/scans/actions/create")
def create_action(
    title: str = Form(...),
    url: str = Form(...),
    max_pages: int = Form(5),
    session: Session = Depends(get_db),
):
    """MVP flow: one step — create a scan and fetch the site."""
    scan = create_scan(session, ScanCreate(title=title))
    try:
        add_website_source(session, scan.id, WebsiteSourceCreate(url=url, max_pages=max_pages))
    except AppError as exc:
        session.delete(scan)
        session.commit()
        return redirect_to("/ui/scans", f"Не удалось загрузить сайт: {exc.message}")
    return redirect_to(f"/ui/scans/{scan.id}")


@ui.get("/scans/{scan_id}", response_class=HTMLResponse)
def scan_page(scan_id: uuid.UUID, request: Request, session: Session = Depends(get_db)):
    return render(request, "scan_detail.html", scan=scan_view(scan_id, session), client_request_id=uuid.uuid4())


@ui.post("/scans/{scan_id}/sources/actions/create")
def source_action(
    scan_id: uuid.UUID,
    url: str = Form(...),
    name: str = Form(""),
    max_pages: int = Form(5),
    session: Session = Depends(get_db),
):
    try:
        add_website_source(session, scan_id, WebsiteSourceCreate(
            url=url, name=name.strip() or None, max_pages=max_pages,
        ))
    except AppError as exc:
        return redirect_to(f"/ui/scans/{scan_id}", f"Не удалось загрузить сайт: {exc.message}")
    return redirect_to(f"/ui/scans/{scan_id}")


@ui.post("/scans/{scan_id}/sources/competitor")
def competitor_action(
    scan_id: uuid.UUID,
    url: str = Form(...),
    name: str = Form(""),
    selection_reason: str = Form(...),
    session: Session = Depends(get_db),
):
    try:
        add_website_source(session, scan_id, WebsiteSourceCreate(
            url=url, name=name.strip() or None, max_pages=3,
            role="competitor", selection_reason=selection_reason,
        ))
    except AppError as exc:
        return redirect_to(f"/ui/scans/{scan_id}", f"Не удалось добавить альтернативу: {exc.message}")
    return redirect_to(f"/ui/scans/{scan_id}")


@ui.post("/scans/{scan_id}/actions/run")
def run_action(scan_id: uuid.UUID, session: Session = Depends(get_db)):
    run_scan(session, scan_id)
    return RedirectResponse(f"/ui/scans/{scan_id}", status_code=303)


@ui.post("/scans/{scan_id}/actions/retry")
def retry_action(scan_id: uuid.UUID, session: Session = Depends(get_db)):
    retry_scan(session, scan_id)
    return RedirectResponse(f"/ui/scans/{scan_id}", status_code=303)


@ui.post("/scans/{scan_id}/actions/reanalysis")
def reanalysis_action(
    scan_id: uuid.UUID,
    agent: str = Form(...),
    object_type: str = Form(...),
    object_id: uuid.UUID = Form(...),
    issue_id: uuid.UUID = Form(...),
    client_request_id: uuid.UUID = Form(...),
    session: Session = Depends(get_db),
):
    create_reanalysis(session, scan_id, ReanalysisCreate(
        agent=agent, object_type=object_type, object_id=object_id,
        issue_id=issue_id, client_request_id=client_request_id,
    ))
    return RedirectResponse(f"/ui/scans/{scan_id}", status_code=303)


@ui.get("/scans/{scan_id}/review", response_class=HTMLResponse)
def review_page(
    scan_id: uuid.UUID,
    request: Request,
    register: str = "findings",
    session: Session = Depends(get_db),
):
    scan = get_scan(session, scan_id)
    gate = session.scalar(select(StageGate).where(StageGate.scan_id == scan_id))
    questions = list(session.scalars(
        select(OpenQuestion).where(OpenQuestion.scan_id == scan_id).order_by(OpenQuestion.ordinal)
    ))
    return render(
        request,
        "review.html",
        scan=scan,
        gate=gate,
        questions=questions,
        events=list_review_events(session, scan_id),
        registers=REGISTER_CONFIG,
        selected_register=register,
        rows=_review_rows(scan_id, register, session),
        brief=scan.client_brief or {},
        brief_fields=BRIEF_FIELDS,
    )


@ui.post("/scans/{scan_id}/review/registers/{register}/{entity_id}")
async def register_edit_action(
    scan_id: uuid.UUID,
    register: str,
    entity_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_db),
):
    target = f"/ui/scans/{scan_id}/review?register={quote(register)}"
    try:
        form = await request.form()
        json_fields = set(str(form.get("__json_fields", "")).split(","))
        bool_fields = set(str(form.get("__bool_fields", "")).split(","))
        number_fields = set(str(form.get("__number_fields", "")).split(","))
        changes = {}
        for name in REGISTER_CONFIG.get(register, {}).get("fields", ()):
            raw = form.get(f"field__{name}")
            if name in bool_fields:
                changes[name] = raw == "on"
            elif name in number_fields:
                changes[name] = None if raw in (None, "") else float(raw)
            elif name in json_fields:
                changes[name] = json.loads(str(raw or ""))
            else:
                changes[name] = raw
        patch_register(session, scan_id, register, entity_id, RegisterPatch(
            actor=str(form.get("actor", "")), note=str(form.get("note", "")) or None,
            changes=changes,
        ))
    except (AppError, ValueError) as exc:
        message = exc.message if isinstance(exc, AppError) else "Проверьте значения и JSON-поля"
        return redirect_to(target, message)
    return redirect_to(target)


@ui.post("/scans/{scan_id}/review/brief")
async def brief_action(
    scan_id: uuid.UUID, request: Request, session: Session = Depends(get_db)
):
    target = f"/ui/scans/{scan_id}/review"
    try:
        form = await request.form()
        brief = {name: str(form.get(name, "")) for name in BRIEF_FIELDS}
        update_client_brief(session, scan_id, ClientBriefUpdate(
            actor=str(form.get("actor", "")), note=str(form.get("note", "")) or None,
            brief=brief,
        ))
    except (AppError, ValueError) as exc:
        message = exc.message if isinstance(exc, AppError) else "Проверьте поля Client Brief"
        return redirect_to(target, message)
    return redirect_to(target)


@ui.post("/scans/{scan_id}/review/questions/create")
async def question_create_action(
    scan_id: uuid.UUID, request: Request, session: Session = Depends(get_db)
):
    target = f"/ui/scans/{scan_id}/review"
    try:
        form = await request.form()
        create_open_question(session, scan_id, OpenQuestionCreate(
            actor=str(form.get("actor", "")), note=str(form.get("note", "")) or None,
            question=str(form.get("question", "")), target=str(form.get("target", "client")),
        ))
    except (AppError, ValueError) as exc:
        message = exc.message if isinstance(exc, AppError) else "Проверьте поля открытого вопроса"
        return redirect_to(target, message)
    return redirect_to(target)


@ui.post("/scans/{scan_id}/review/questions/{question_id}")
async def question_edit_action(
    scan_id: uuid.UUID,
    question_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_db),
):
    target = f"/ui/scans/{scan_id}/review"
    try:
        form = await request.form()
        patch_open_question(session, scan_id, question_id, OpenQuestionPatch(
            actor=str(form.get("actor", "")), note=str(form.get("note", "")) or None,
            status=str(form.get("status", "open")),
        ))
    except (AppError, ValueError) as exc:
        message = exc.message if isinstance(exc, AppError) else "Проверьте статус вопроса"
        return redirect_to(target, message)
    return redirect_to(target)


@ui.post("/scans/{scan_id}/review/action")
async def review_transition_action(
    scan_id: uuid.UUID, request: Request, session: Session = Depends(get_db)
):
    target = f"/ui/scans/{scan_id}/review"
    try:
        form = await request.form()
        review_scan(session, scan_id, ReviewAction(
            actor=str(form.get("actor", "")), note=str(form.get("note", "")) or None,
            action=str(form.get("action", "start")),
        ))
    except (AppError, ValueError) as exc:
        message = exc.message if isinstance(exc, AppError) else "Недопустимый переход ревью"
        return redirect_to(target, message)
    return redirect_to(target)


@ui.post("/scans/{scan_id}/review/stage-gate")
async def stage_gate_action(
    scan_id: uuid.UUID, request: Request, session: Session = Depends(get_db)
):
    target = f"/ui/scans/{scan_id}/review"
    try:
        form = await request.form()
        decide_stage_gate(session, scan_id, StageGateDecision(
            actor=str(form.get("actor", "")), note=str(form.get("note", "")) or None,
            decision=str(form.get("decision", "not_ready")),
            rationale=str(form.get("rationale", "")),
            blockers=str(form.get("blockers", "")).splitlines(),
            required_validations=str(form.get("required_validations", "")).splitlines(),
        ))
    except (AppError, ValueError) as exc:
        message = exc.message if isinstance(exc, AppError) else "Проверьте поля Stage Gate"
        return redirect_to(target, message)
    return redirect_to(target)


@ui.post("/scans/{scan_id}/review/build")
def review_build_action(scan_id: uuid.UUID, session: Session = Depends(get_db)):
    try:
        build_report_v2(session, scan_id, persist=True)
    except AppError as exc:
        return redirect_to(f"/ui/scans/{scan_id}/review", exc.message)
    return redirect_to(f"/ui/scans/{scan_id}/review")


@ui.get("/scans/{scan_id}/report", response_class=HTMLResponse)
def report_page(scan_id: uuid.UUID, type: str | None = None, session: Session = Depends(get_db)):
    document = build_report_v2(session, scan_id)
    headers = {"Deprecation": "true"} if type is not None else None
    return HTMLResponse(render_html_v2(document), headers=headers)


@ui.get("/scans/{scan_id}/report.pdf")
def report_pdf_page(scan_id: uuid.UUID, type: str | None = None, session: Session = Depends(get_db)):
    document = build_report_v2(session, scan_id)
    pdf, cache_hit = get_or_render_pdf(session, scan_id, document)
    headers = {
        "Content-Disposition": f'inline; filename="narrative-audit-{scan_id}.pdf"',
        "Cache-Control": "no-store",
        "ETag": f'"{document.meta.content_hash}"',
        "X-Report-Cache": "HIT" if cache_hit else "MISS",
    }
    if type is not None:
        headers["Deprecation"] = "true"
    return Response(content=pdf, media_type="application/pdf", headers=headers)


@ui.get("/scans/{scan_id}/metrics/{metric_id}", response_class=HTMLResponse)
def metric_page(scan_id: uuid.UUID, metric_id: uuid.UUID, request: Request, session: Session = Depends(get_db)):
    data = metric_view(scan_id, metric_id, session)
    return render(request, "metric_detail.html", scan_id=scan_id, **data)
