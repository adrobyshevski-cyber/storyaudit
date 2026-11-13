"""Validated mode: consented interviews, redacted quotes, honest coverage.

Rules this module enforces (methodology v1.1, §1.2 and §6):

* interview material is usable only with recorded consent;
* the raw transcript is never stored — only redacted segments;
* a session cannot be approved without consent, an anonymised reference, a sample
  note and at least one segment;
* coverage is reported as counts, and every summary states that a small interview
  set is **not** a representative study.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.anonymize import assert_clean, anonymize
from app.errors import AppError, conflict, not_found
from app.models import Scan, now
from app.registers.models import InterviewSegment, InterviewSession
from app.scans import get_scan

LAYERS = {"experienced", "perceived"}
METHODS = {"interview", "survey", "service_records", "observation"}
CONSENT = {"pending", "granted", "declined", "withdrawn"}
NON_REPRESENTATIVE = (
    "Выборка интервью не является статистически репрезентативной; выводы ограничены "
    "описанными участниками и не доказывают распределение по всей аудитории."
)


def _validated_scan(session: Session, scan_id: uuid.UUID) -> Scan:
    scan = get_scan(session, scan_id)
    if scan.audit_mode != "validated":
        raise conflict(
            "Интервью доступны только в режиме validated: переключите режим аудита скана"
        )
    return scan


def _next_code(session: Session, model: Any, scan_id: uuid.UUID, prefix: str, column: Any) -> str:
    existing = session.scalars(select(column).where(model.scan_id == scan_id)).all()
    numbers = []
    for value in existing:
        digits = value[len(prefix):]
        if digits.isdigit():
            numbers.append(int(digits))
    return f"{prefix}{(max(numbers) + 1) if numbers else 1:02d}"


def set_audit_mode(session: Session, scan_id: uuid.UUID, mode: str) -> Scan:
    """Switch a scan between desk and validated research."""
    if mode not in {"desk", "validated"}:
        raise AppError(422, "validation_error", "Режим аудита: desk или validated")
    scan = get_scan(session, scan_id)
    if scan.status not in {"draft", "succeeded", "failed"}:
        raise conflict("Режим аудита меняется до запуска или после завершения")
    if mode == "desk":
        approved = session.scalar(
            select(func.count(InterviewSession.id)).where(
                InterviewSession.scan_id == scan.id,
                InterviewSession.status == "approved",
            )
        )
        if approved:
            raise conflict("В скане есть утверждённые интервью — режим validated нельзя отключить")
    scan.audit_mode = mode
    session.commit()
    return scan


def create_session(session: Session, scan_id: uuid.UUID, data: dict[str, Any]) -> InterviewSession:
    scan = _validated_scan(session, scan_id)
    layer = data.get("layer") or "experienced"
    method = data.get("method") or "interview"
    if layer not in LAYERS:
        raise AppError(422, "validation_error", "Слой: experienced или perceived")
    if method not in METHODS:
        raise AppError(422, "validation_error", "Метод: interview, survey, service_records или observation")
    anonymous_ref = (data.get("anonymous_ref") or "").strip()
    if not anonymous_ref:
        raise AppError(
            422, "validation_error",
            "Укажите обезличенную ссылку участника (например «P1: маркетолог, 8 лет опыта»)",
        )
    code = _next_code(session, InterviewSession, scan.id, "IV", InterviewSession.code)
    record = InterviewSession(
        scan_id=scan.id,
        code=code,
        title=(data.get("title") or f"Интервью {code}").strip()[:200],
        method=method,
        layer=layer,
        anonymous_ref=anonymous_ref[:300],
        sample_note=(data.get("sample_note") or None),
        limitations=(data.get("limitations") or None),
        collected_at=data.get("collected_at"),
    )
    session.add(record)
    session.commit()
    return record


def record_consent(
    session: Session,
    scan_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    status: str,
    scope: str | None = None,
    reference: str | None = None,
) -> InterviewSession:
    record = get_session(session, scan_id, session_id)
    if status not in CONSENT:
        raise AppError(422, "validation_error", "Статус согласия: pending, granted, declined или withdrawn")
    if status == "granted" and not (scope or "").strip():
        raise AppError(422, "validation_error", "Для согласия укажите, что именно разрешено цитировать")
    record.consent_status = status
    record.consent_scope = (scope or None)
    record.consent_reference = (reference or None)
    record.consent_recorded_at = now()
    if status in {"declined", "withdrawn"}:
        record.status = "draft"
        record.approved_at = None
        record.approved_by = None
    session.commit()
    return record


def get_session(session: Session, scan_id: uuid.UUID, session_id: uuid.UUID) -> InterviewSession:
    record = session.get(InterviewSession, session_id)
    if not record or record.scan_id != scan_id:
        raise not_found("Interview session")
    return record


def add_segment(
    session: Session,
    scan_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    text: str,
    theme: str | None = None,
    promise_code: str | None = None,
    extra_terms: dict[str, str] | None = None,
    dry_run: bool = False,
) -> InterviewSegment | dict[str, Any]:
    """Redact the pasted text and store only the redacted version."""
    record = get_session(session, scan_id, session_id)
    if record.consent_status != "granted":
        raise conflict("Без зафиксированного согласия материал интервью не принимается")
    if record.status == "approved":
        raise conflict("Интервью утверждено: снимите утверждение или создайте новую сессию")
    cleaned = anonymize(text, extra_terms=extra_terms)
    try:
        assert_clean(cleaned.text)
    except ValueError as exc:
        raise AppError(422, "validation_error", f"Обезличивание неполное: {exc}") from exc
    if dry_run:
        return cleaned.as_dict()
    if not cleaned.text.strip():
        raise AppError(422, "validation_error", "Пустой фрагмент")
    code = _next_code(session, InterviewSegment, scan_id, "Q", InterviewSegment.code)
    segment = InterviewSegment(
        scan_id=scan_id,
        session_id=record.id,
        code=code,
        text=cleaned.text.strip(),
        layer=record.layer,
        theme=theme,
        promise_code=promise_code,
        redactions=cleaned.findings,
        warnings=cleaned.warnings,
    )
    session.add(segment)
    session.commit()
    return segment


def approve_session(
    session: Session, scan_id: uuid.UUID, session_id: uuid.UUID, *, actor: str
) -> InterviewSession:
    record = get_session(session, scan_id, session_id)
    if not (actor or "").strip():
        raise AppError(422, "validation_error", "Укажите, кто утверждает материал")
    if record.consent_status != "granted":
        raise conflict("Утвердить интервью можно только при зафиксированном согласии")
    if not (record.sample_note or "").strip():
        raise AppError(
            422, "validation_error",
            "Опишите характеристики выборки: без них результат нельзя интерпретировать",
        )
    segments = session.scalar(
        select(func.count(InterviewSegment.id)).where(InterviewSegment.session_id == record.id)
    ) or 0
    if not segments:
        raise conflict("В интервью нет обезличенных фрагментов")
    record.status = "approved"
    record.approved_by = actor.strip()[:200]
    record.approved_at = now()
    session.commit()
    return record


def list_sessions(session: Session, scan_id: uuid.UUID) -> list[dict[str, Any]]:
    get_scan(session, scan_id)
    rows = session.scalars(
        select(InterviewSession)
        .where(InterviewSession.scan_id == scan_id)
        .order_by(InterviewSession.code)
    )
    return [session_dict(row) for row in rows]


def list_segments(session: Session, scan_id: uuid.UUID, session_id: uuid.UUID) -> list[dict[str, Any]]:
    get_session(session, scan_id, session_id)
    rows = session.scalars(
        select(InterviewSegment)
        .where(InterviewSegment.session_id == session_id)
        .order_by(InterviewSegment.code)
    )
    return [segment_dict(row) for row in rows]


def session_dict(row: InterviewSession) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "scan_id": str(row.scan_id),
        "code": row.code,
        "title": row.title,
        "method": row.method,
        "layer": row.layer,
        "anonymous_ref": row.anonymous_ref,
        "sample_note": row.sample_note,
        "consent_status": row.consent_status,
        "consent_scope": row.consent_scope,
        "consent_reference": row.consent_reference,
        "consent_recorded_at": row.consent_recorded_at,
        "status": row.status,
        "collected_at": row.collected_at,
        "approved_by": row.approved_by,
        "approved_at": row.approved_at,
        "limitations": row.limitations,
    }


def segment_dict(row: InterviewSegment) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "session_id": str(row.session_id),
        "code": row.code,
        "text": row.text,
        "layer": row.layer,
        "theme": row.theme,
        "promise_code": row.promise_code,
        "redactions": row.redactions,
        "warnings": row.warnings,
        "is_quoted": row.is_quoted,
    }


def coverage(session: Session, scan_id: uuid.UUID) -> dict[str, Any]:
    """What the validated track actually contributes, stated without inflation."""
    scan = get_scan(session, scan_id)
    sessions = list(session.scalars(
        select(InterviewSession).where(InterviewSession.scan_id == scan_id)
    ))
    approved = [row for row in sessions if row.status == "approved"]
    segments_by_layer = dict(session.execute(
        select(InterviewSegment.layer, func.count(InterviewSegment.id))
        .where(InterviewSegment.scan_id == scan_id)
        .group_by(InterviewSegment.layer)
    ).all())
    return {
        "audit_mode": scan.audit_mode,
        "sessions_total": len(sessions),
        "sessions_approved": len(approved),
        "sessions_with_consent": sum(1 for row in sessions if row.consent_status == "granted"),
        "sessions_by_layer": {
            layer: sum(1 for row in approved if row.layer == layer) for layer in sorted(LAYERS)
        },
        "segments_by_layer": {layer: int(segments_by_layer.get(layer, 0)) for layer in sorted(LAYERS)},
        "experienced_available": bool(segments_by_layer.get("experienced")) and any(
            row.layer == "experienced" for row in approved
        ),
        "perceived_available": bool(segments_by_layer.get("perceived")) and any(
            row.layer == "perceived" for row in approved
        ),
        "representative": False,
        "note": NON_REPRESENTATIVE,
        "limitations": [row.limitations for row in approved if row.limitations],
    }
