"""Public self-service audit requests.

The landing page is the only unauthenticated entry point into the product. A
request creates a normal draft scan and one ``INGEST`` job; from that point the
regular pipeline takes over. The public URL contains a random capability token
whose SHA-256 is stored instead of the token itself.

Publication is deliberately gated by the analyst review: a public link opens the
report only when ``StageGate`` is ``ready`` and ``scan.audit_phase`` is
``client_ready``. That keeps the methodology's human-in-the-loop rule even though
the request itself is automatic.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import not_found
from app.models import AuditRequest, Job, Scan, now
from app.queue import enqueue
from app.registers.models import StageGate
from app.scans import create_scan
from app.schemas import ScanCreate
from app.website import validate_site_url

TOKEN_BYTES = 32
ACTIVE_STATES = {"new", "ingesting", "queued", "analyzing", "review"}
TERMINAL_STATES = {"ready", "failed", "rejected", "expired"}

STEP_ORDER = [
    ("received", "Заявка принята", "Создана защищённая ссылка на эту страницу"),
    ("ingesting", "Сбор публичных страниц", "Скачиваем доступные страницы сайта"),
    ("analyzing", "Анализ и диагностика", "Собираем нарратив, находки и рекомендации"),
    ("review", "Проверка аналитиком", "Проверяем формулировки, источники и ограничения"),
    ("ready", "Отчёт готов", "Можно открыть полный отчёт и PDF"),
]


def progress_steps(state: str) -> list[dict]:
    """Presentation-only progress; the state itself is derived from the scan."""
    active = next((index for index, item in enumerate(STEP_ORDER) if item[0] == state), 0)
    if state == "failed":
        active = 0
    steps = []
    for index, (key, label, hint) in enumerate(STEP_ORDER):
        steps.append({
            "key": key,
            "label": label,
            "hint": hint,
            "status": "done" if index < active else ("active" if index == active else "pending"),
        })
    return steps


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_token(token: str) -> str:
    return _hash(token)


def hash_client(value: str | None) -> str | None:
    """Salted hash of an IP or User-Agent; raw values are never stored."""
    if not value:
        return None
    settings = get_settings()
    return _hash(f"{settings.public_hash_salt}:{value}")


def create_audit_request(
    session: Session,
    *,
    website_url: str,
    email: str | None,
    company: str | None,
    ip: str | None,
    user_agent: str | None,
    utm: dict | None = None,
) -> tuple[AuditRequest, str]:
    """Validate the URL, create a draft scan and queue its ingestion.

    Returns the stored request and the raw capability token. The raw token is
    shown once (in the redirect URL) and is not recoverable from the database.
    """
    settings = get_settings()
    url = validate_site_url(website_url)
    host = urlparse(url).hostname or url
    raw_token = secrets.token_urlsafe(TOKEN_BYTES)

    scan = create_scan(session, ScanCreate(title=f"Narrative Audit · {host}"))
    scan.brand = host
    scan.audit_phase = "ingesting"
    scan.analysis_generation = 1

    request = AuditRequest(
        token_hash=hash_token(raw_token),
        website_url=url,
        email=(email or "").strip() or None,
        company=(company or "").strip()[:200] or None,
        consent_at=now(),
        consent_version=settings.public_consent_version,
        status="ingesting",
        scan_id=scan.id,
        ip_hash=hash_client(ip) or "",
        user_agent_hash=hash_client(user_agent),
        utm=utm or {},
        expires_at=now() + timedelta(days=settings.public_request_token_ttl_days),
    )
    session.add(request)
    session.flush()

    enqueue(
        session,
        scan,
        "INGEST",
        "scan",
        scan.id,
        generation=1,
        job_type="ingest",
        snapshot={"website_url": url, "max_pages": settings.website_max_pages},
    )
    session.commit()
    return request, raw_token


def get_request_by_token(session: Session, token: str) -> AuditRequest:
    if not token:
        raise not_found("AuditRequest")
    request = session.scalar(
        select(AuditRequest).where(AuditRequest.token_hash == hash_token(token))
    )
    if not request:
        raise not_found("AuditRequest")
    return request


def _gate_is_ready(session: Session, scan: Scan) -> bool:
    if scan.audit_phase != "client_ready":
        return False
    gate = session.scalar(select(StageGate).where(StageGate.scan_id == scan.id))
    return bool(gate and gate.decision == "ready" and gate.approved_by)


def public_state(session: Session, request: AuditRequest) -> str:
    """Derive the public state from the scan, not from a second state machine.

    Persisting it on the request row keeps list queries simple, but the scan is
    always the source of truth: a failed job or an approved gate must be
    reflected immediately even if no public page was opened in between.
    """
    scan = session.get(Scan, request.scan_id) if request.scan_id else None
    if request.status == "rejected":
        return "rejected"
    if request.expires_at and request.expires_at < datetime.now(timezone.utc):
        return "expired"
    if scan is None:
        return "failed"
    if scan.status == "failed":
        state = "failed"
    elif scan.status == "succeeded":
        state = "ready" if _gate_is_ready(session, scan) else "review"
    elif scan.status in {"queued", "running"} or scan.audit_phase == "analyzing":
        state = "analyzing"
    elif scan.status == "draft":
        state = "ingesting"
    else:
        state = request.status or "new"

    if request.status != state:
        request.status = state
        if state == "ready" and not request.ready_at:
            request.ready_at = now()
        if state == "failed":
            request.error_summary = (scan.error_summary or _latest_error(session, scan.id))[:4000]
        session.commit()
    return state


def _latest_error(session: Session, scan_id: uuid.UUID) -> str:
    job = session.scalar(
        select(Job)
        .where(Job.scan_id == scan_id, Job.status == "failed")
        .order_by(Job.finished_at.desc().nullslast())
        .limit(1)
    )
    if job and job.last_error_message:
        return f"{job.agent}: {job.last_error_message}"
    return "Не удалось завершить анализ сайта."


def report_ready(session: Session, request: AuditRequest) -> bool:
    return public_state(session, request) == "ready"


def status_payload(session: Session, request: AuditRequest, token: str) -> dict:
    state = public_state(session, request)
    return {
        "state": state,
        "website_url": request.website_url,
        "created_at": request.created_at.isoformat() if request.created_at else None,
        "ready_at": request.ready_at.isoformat() if request.ready_at else None,
        "error": request.error_summary if state == "failed" else None,
        "report_url": f"/audit/{token}/report" if state == "ready" else None,
        "pdf_url": f"/audit/{token}/report.pdf" if state == "ready" else None,
    }
