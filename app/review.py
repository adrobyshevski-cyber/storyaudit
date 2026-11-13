"""Human review operations for evidence-first registers.

Register edits never touch the immutable corpus and never enqueue LLM stages.
Every mutation writes a ReviewEvent, invalidates persisted report snapshots and
moves an approved audit back to analyst review.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import AppError, conflict, not_found
from app.models import Scan
from app.registers.models import (
    AuditHypothesis,
    EvidenceRecord,
    FindingV2,
    JourneyStep,
    LanguageCode,
    OpenQuestion,
    Product,
    RecommendationFinding,
    RecommendationV2,
    ReportSnapshot,
    ReviewEvent,
    ScorecardEntry,
    SourceRegistryEntry,
    StageGate,
    Statement,
)
from app.scans import get_scan
from app.schemas import (
    ClientBriefUpdate,
    OpenQuestionCreate,
    OpenQuestionPatch,
    RegisterPatch,
    ReviewAction,
    StageGateDecision,
)

REGISTER_CONFIG: dict[str, dict[str, Any]] = {
    "sources": {
        "label": "S — источники",
        "model": SourceRegistryEntry,
        "order": SourceRegistryEntry.code,
        "fields": ("availability", "currency", "confirms", "limitations", "entry_metadata"),
    },
    "products": {
        "label": "P — продукты и обещания",
        "model": Product,
        "order": Product.code,
        "fields": (
            "name", "audience", "trigger_situation", "promise", "format", "price",
            "procedure", "proof", "boundary", "status", "is_primary", "source_codes",
        ),
    },
    "evidence": {
        "label": "E — доказательства",
        "model": EvidenceRecord,
        "order": EvidenceRecord.code,
        "fields": (
            "claim", "source_codes", "provenance", "relevance", "freshness",
            "limitations", "allowed_conclusion", "confidence", "needs_review", "review_status",
        ),
    },
    "findings": {
        "label": "G/M/W — находки",
        "model": FindingV2,
        "order": FindingV2.code,
        "fields": (
            "statement", "basis_codes", "consequence", "alternative_explanation",
            "verification", "priority", "confidence", "needs_review", "review_status",
        ),
    },
    "scorecard": {
        "label": "Scorecard",
        "model": ScorecardEntry,
        "order": ScorecardEntry.ordinal,
        "fields": ("score", "na_reason", "confidence", "rationale", "source_codes"),
    },
    "recommendations": {
        "label": "R — рекомендации",
        "model": RecommendationV2,
        "order": RecommendationV2.code,
        "fields": (
            "change", "rationale", "expected_effect", "impact", "evidence_confidence",
            "urgency", "effort", "priority", "decision_class", "owner", "kpi_codes",
            "review_status",
        ),
    },
    "journey": {
        "label": "Customer Journey",
        "model": JourneyStep,
        "order": JourneyStep.ordinal,
        "fields": ("public_state", "obstacle", "source_codes", "check_method", "is_observed"),
    },
    "language": {
        "label": "L — язык бренда",
        "model": LanguageCode,
        "order": LanguageCode.code,
        "fields": ("professional", "lived", "brand", "match_status", "is_illustration", "source_codes"),
    },
    "hypotheses": {
        "label": "H — гипотезы",
        "model": AuditHypothesis,
        "order": AuditHypothesis.code,
        "fields": ("statement", "how_to_verify", "status"),
    },
}

ALLOWED_VALUES = {
    "availability": {"full", "partial", "unavailable"},
    "currency": {"current", "historical", "disputed", "unknown"},
    "confidence": {"high", "medium", "low"},
    "evidence_confidence": {"high", "medium", "low"},
    "relevance": {"high", "medium", "low"},
    "freshness": {"current", "historical", "unknown"},
    "impact": {"high", "medium", "low"},
    "urgency": {"high", "medium", "low"},
    "effort": {"high", "medium", "low"},
    "priority": {"P1", "P2", "P3", "blocker"},
    "decision_class": {"keep", "fix", "test", "stop"},
    "review_status": {"machine", "needs_review", "in_review", "approved", "rejected"},
    "match_status": {"match", "partial", "gap", "no_data"},
    "status": {
        "current", "secondary", "legacy", "historical", "future", "unknown",
        "open", "testing", "confirmed", "rejected",
    },
}

BRIEF_FIELDS = (
    "business_goal", "primary_audience", "priority_offers", "constraints",
    "resources", "decision_makers", "ethical_boundaries", "notes",
)


def _json_value(value: Any) -> Any:
    if isinstance(value, (uuid.UUID, datetime, date)):
        return str(value) if isinstance(value, uuid.UUID) else value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def entity_dict(entity: Any) -> dict[str, Any]:
    return {
        column.key: _json_value(getattr(entity, column.key))
        for column in entity.__table__.columns
    }


def event_dict(event: ReviewEvent) -> dict[str, Any]:
    return entity_dict(event)


def register_fields(register: str) -> tuple[str, ...]:
    return tuple(_config(register)["fields"])


def _config(register: str) -> dict[str, Any]:
    config = REGISTER_CONFIG.get(register)
    if config is None:
        raise not_found("Register")
    return config


def _succeeded_scan(session: Session, scan_id: uuid.UUID) -> Scan:
    scan = get_scan(session, scan_id)
    if scan.status != "succeeded":
        raise conflict("Review is available only for a succeeded scan")
    return scan


def list_register(session: Session, scan_id: uuid.UUID, register: str) -> list[dict[str, Any]]:
    get_scan(session, scan_id)
    config = _config(register)
    model = config["model"]
    rows = session.scalars(
        select(model).where(model.scan_id == scan_id).order_by(config["order"])
    )
    return [entity_dict(row) for row in rows]


def list_register_page(
    session: Session, scan_id: uuid.UUID, register: str, *, limit: int, offset: int
) -> tuple[list[dict[str, Any]], int]:
    """Same rows as ``list_register`` plus the total, for API pagination."""
    get_scan(session, scan_id)
    config = _config(register)
    model = config["model"]
    query = select(model).where(model.scan_id == scan_id).order_by(config["order"])
    total = session.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    rows = session.scalars(query.limit(limit).offset(offset))
    return [entity_dict(row) for row in rows], int(total)


def list_review_events(
    session: Session, scan_id: uuid.UUID, *, limit: int = 100
) -> list[dict[str, Any]]:
    get_scan(session, scan_id)
    rows = session.scalars(
        select(ReviewEvent)
        .where(ReviewEvent.scan_id == scan_id)
        .order_by(ReviewEvent.created_at.desc())
        .limit(limit)
    )
    return [event_dict(row) for row in rows]


def _invalidate_snapshots(session: Session, scan_id: uuid.UUID) -> None:
    for snapshot in session.scalars(select(ReportSnapshot).where(
        ReportSnapshot.scan_id == scan_id,
        ReportSnapshot.status.in_(("fresh", "approved")),
    )):
        snapshot.status = "needs_rebuild"


def _record_event(
    session: Session,
    scan: Scan,
    *,
    entity_type: str,
    entity_id: uuid.UUID | None,
    actor: str,
    action: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    note: str | None = None,
) -> ReviewEvent:
    event = ReviewEvent(
        scan_id=scan.id,
        entity_type=entity_type,
        entity_id=entity_id,
        actor=actor.strip(),
        action=action,
        before=before,
        after=after,
        note=note.strip() if note else None,
    )
    session.add(event)
    if action in {"edit", "reopen", "needs_review"}:
        _invalidate_snapshots(session, scan.id)
        if scan.audit_phase not in {"draft", "ingesting", "analyzing", "registers_ready"}:
            scan.audit_phase = "analyst_review"
        gate = session.scalar(select(StageGate).where(StageGate.scan_id == scan.id))
        if gate is not None:
            gate.approved_by = None
            gate.decided_at = None
    return event


def _invalid(message: str) -> AppError:
    return AppError(422, "invalid_register_patch", message)


def _coerce_field(entity: Any, field: str, value: Any) -> Any:
    column = entity.__table__.columns[field]
    if value is None:
        if not column.nullable:
            raise _invalid(f"Field {field} cannot be null")
        return None
    current_value = getattr(entity, field)
    is_json_list = isinstance(current_value, list) or field.endswith("_codes") or field == "limitations"
    is_json_dict = isinstance(current_value, dict) or field == "entry_metadata"
    python_type = list if is_json_list else dict if is_json_dict else column.type.python_type
    if python_type is str:
        if not isinstance(value, str):
            raise _invalid(f"Field {field} must be a string")
        value = value.strip()
        if not value and not column.nullable:
            raise _invalid(f"Field {field} cannot be empty")
        max_length = getattr(column.type, "length", None) or 4000
        if len(value) > max_length:
            raise _invalid(f"Field {field} exceeds {max_length} characters")
    elif python_type is bool:
        if not isinstance(value, bool):
            raise _invalid(f"Field {field} must be boolean")
    elif python_type is Decimal:
        if isinstance(value, bool):
            raise _invalid(f"Field {field} must be numeric")
        try:
            value = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise _invalid(f"Field {field} must be numeric") from exc
        if not value.is_finite():
            raise _invalid(f"Field {field} must be finite")
    elif python_type is list:
        if not isinstance(value, list) or any(not isinstance(item, (str, int, float, dict)) for item in value):
            raise _invalid(f"Field {field} must be a JSON array")
    elif python_type is dict:
        if not isinstance(value, dict):
            raise _invalid(f"Field {field} must be a JSON object")
    allowed = ALLOWED_VALUES.get(field)
    if allowed is not None and value not in allowed:
        raise _invalid(f"Field {field} has unsupported value {value!r}")
    return value


def patch_register(
    session: Session,
    scan_id: uuid.UUID,
    register: str,
    entity_id: uuid.UUID,
    data: RegisterPatch,
) -> dict[str, Any]:
    scan = _succeeded_scan(session, scan_id)
    config = _config(register)
    entity = session.get(config["model"], entity_id)
    if entity is None or entity.scan_id != scan_id:
        raise not_found("Register entry")
    allowed_fields = set(config["fields"])
    unknown = set(data.changes) - allowed_fields
    if unknown:
        raise _invalid(f"Fields are not editable: {', '.join(sorted(unknown))}")

    before = entity_dict(entity)
    for field, raw_value in data.changes.items():
        setattr(entity, field, _coerce_field(entity, field, raw_value))

    if register == "scorecard":
        if entity.score is None:
            if not entity.na_reason:
                raise _invalid("N/A score requires na_reason")
            entity.confidence = None
        else:
            if not 0 <= entity.score <= 10:
                raise _invalid("Score must be between 0 and 10")
            if not entity.confidence:
                raise _invalid("A numeric score requires confidence")
            entity.na_reason = None
    if scan.audit_mode == "desk":
        if register == "scorecard" and entity.criterion in {
            "Коммуникация ↔ фактический опыт", "Реальное узнавание у аудитории"
        } and entity.score is not None:
            raise _invalid("Customer experience and real recognition require Validated mode")
        if register == "journey" and entity.is_observed:
            raise _invalid("Observed customer experience requires Validated mode")
        if register == "language" and entity.lived:
            raise _invalid("Lived language requires interviews in Validated mode")

    session.flush()
    after = entity_dict(entity)
    note = data.note
    if register == "sources" and (
        before.get("currency") != after.get("currency")
        or before.get("availability") != after.get("availability")
    ):
        affected = _mark_source_dependencies(session, scan.id, entity)
        note = "; ".join(filter(None, [note, f"Зависимые записи отмечены needs_review: {affected}"]))
    _record_event(
        session, scan, entity_type=register, entity_id=entity.id, actor=data.actor,
        action="edit", before=before, after=after, note=note,
    )
    session.commit()
    return after


def _overlaps(values: Any, codes: set[str]) -> bool:
    return bool({str(value) for value in (values or [])} & codes)


def _mark_source_dependencies(
    session: Session, scan_id: uuid.UUID, source: SourceRegistryEntry
) -> int:
    """Flag dependent rows after a source is disputed or becomes unavailable."""
    affected = 0
    source_codes = {source.code}
    statements = list(session.scalars(select(Statement).where(Statement.scan_id == scan_id)))
    for row in statements:
        if _overlaps(row.source_codes, source_codes) and not row.needs_review:
            row.needs_review = True
            row.review_status = "needs_review"
            affected += 1
    evidence = list(session.scalars(select(EvidenceRecord).where(EvidenceRecord.scan_id == scan_id)))
    affected_evidence_codes: set[str] = set()
    for row in evidence:
        if _overlaps(row.source_codes, source_codes):
            affected_evidence_codes.add(row.code)
            if not row.needs_review:
                row.needs_review = True
                affected += 1
    products = list(session.scalars(select(Product).where(Product.scan_id == scan_id)))
    affected_product_codes = {
        row.code for row in products if _overlaps(row.source_codes, source_codes)
    }
    affected_codes = source_codes | affected_evidence_codes | affected_product_codes
    findings = list(session.scalars(select(FindingV2).where(FindingV2.scan_id == scan_id)))
    affected_finding_ids: set[uuid.UUID] = set()
    for row in findings:
        if _overlaps(row.basis_codes, affected_codes):
            affected_finding_ids.add(row.id)
            if not row.needs_review:
                row.needs_review = True
                row.review_status = "needs_review"
                affected += 1
    if affected_finding_ids:
        recommendation_ids = set(session.scalars(select(
            RecommendationFinding.recommendation_id
        ).where(RecommendationFinding.finding_id.in_(affected_finding_ids))))
        for row in session.scalars(select(RecommendationV2).where(
            RecommendationV2.id.in_(recommendation_ids)
        )):
            if row.review_status != "needs_review":
                row.review_status = "needs_review"
                affected += 1
    for row in session.scalars(select(ScorecardEntry).where(ScorecardEntry.scan_id == scan_id)):
        if _overlaps(row.source_codes, affected_codes):
            metadata = dict(row.entry_metadata or {})
            if not metadata.get("needs_review"):
                row.entry_metadata = {**metadata, "needs_review": True}
                affected += 1
    return affected


def update_client_brief(
    session: Session, scan_id: uuid.UUID, data: ClientBriefUpdate
) -> dict[str, Any]:
    scan = get_scan(session, scan_id)
    before = {"client_brief": scan.client_brief or {}}
    brief = {
        str(key): value.strip() if isinstance(value, str) else value
        for key, value in data.brief.items()
        if value is not None and (not isinstance(value, str) or value.strip())
    }
    scan.client_brief = brief
    _sync_brief_gate(session, scan.id, bool(brief))
    after = {"client_brief": brief}
    _record_event(
        session, scan, entity_type="client_brief", entity_id=scan.id, actor=data.actor,
        action="edit", before=before, after=after, note=data.note,
    )
    session.commit()
    return after


def _sync_brief_gate(session: Session, scan_id: uuid.UUID, present: bool) -> None:
    gate = session.scalar(select(StageGate).where(StageGate.scan_id == scan_id))
    if gate is None:
        return
    criteria = []
    for raw in gate.criteria or []:
        item = dict(raw)
        if item.get("criterion") == "Цели и границы клиента":
            item["result"] = "Ready" if present else "Not established"
            item["required"] = "" if present else "Заполнить Client Brief v2.0"
        criteria.append(item)
    gate.criteria = criteria
    validations = [
        item for item in (gate.required_validations or []) if item != "Client Brief v2.0"
    ]
    if not present:
        validations.append("Client Brief v2.0")
    gate.required_validations = validations


def create_open_question(
    session: Session, scan_id: uuid.UUID, data: OpenQuestionCreate
) -> dict[str, Any]:
    scan = get_scan(session, scan_id)
    ordinal = (session.scalar(select(func.max(OpenQuestion.ordinal)).where(
        OpenQuestion.scan_id == scan_id
    )) or 0) + 1
    question = OpenQuestion(
        scan_id=scan_id, ordinal=ordinal, question=data.question.strip(), target=data.target,
    )
    session.add(question)
    session.flush()
    after = entity_dict(question)
    _record_event(
        session, scan, entity_type="open_question", entity_id=question.id, actor=data.actor,
        action="edit", before=None, after=after, note=data.note,
    )
    session.commit()
    return after


def patch_open_question(
    session: Session, scan_id: uuid.UUID, question_id: uuid.UUID, data: OpenQuestionPatch
) -> dict[str, Any]:
    scan = get_scan(session, scan_id)
    question = session.get(OpenQuestion, question_id)
    if question is None or question.scan_id != scan_id:
        raise not_found("Open question")
    before = entity_dict(question)
    changes = data.model_dump(exclude_none=True, exclude={"actor", "note"})
    if not changes:
        raise _invalid("No open-question changes supplied")
    for field, value in changes.items():
        setattr(question, field, value.strip() if isinstance(value, str) else value)
    session.flush()
    after = entity_dict(question)
    _record_event(
        session, scan, entity_type="open_question", entity_id=question.id, actor=data.actor,
        action="edit", before=before, after=after, note=data.note,
    )
    session.commit()
    return after


def review_scan(session: Session, scan_id: uuid.UUID, data: ReviewAction) -> dict[str, Any]:
    scan = _succeeded_scan(session, scan_id)
    before = {"audit_phase": scan.audit_phase}
    if data.action in {"start", "reject", "reopen", "needs_review"}:
        scan.audit_phase = "analyst_review"
    else:
        scan.audit_phase = "stage_gate_decided"
    after = {"audit_phase": scan.audit_phase, "review_action": data.action}
    event_action = "reopen" if data.action == "start" else data.action
    _record_event(
        session, scan, entity_type="scan", entity_id=scan.id, actor=data.actor,
        action=event_action, before=before, after=after, note=data.note,
    )
    session.commit()
    return after


def decide_stage_gate(
    session: Session, scan_id: uuid.UUID, data: StageGateDecision
) -> dict[str, Any]:
    scan = _succeeded_scan(session, scan_id)
    blockers = [item.strip() for item in data.blockers if item.strip()]
    validations = [item.strip() for item in data.required_validations if item.strip()]
    if data.decision == "ready" and blockers:
        raise conflict("A gate with open blockers cannot become client_ready")
    gate = session.scalar(select(StageGate).where(StageGate.scan_id == scan_id))
    if gate is None:
        gate = StageGate(
            scan_id=scan_id, decision="not_ready", rationale="Решение ещё не зафиксировано",
            criteria=[], blockers=[], required_validations=[],
        )
        session.add(gate)
        session.flush()
    before = entity_dict(gate)
    gate.decision = data.decision
    gate.rationale = data.rationale.strip()
    gate.blockers = blockers
    gate.required_validations = validations
    gate.approved_by = data.actor.strip()
    gate.decided_at = datetime.now(timezone.utc)
    scan.audit_phase = "client_ready" if data.decision == "ready" else "stage_gate_decided"
    session.flush()
    after = entity_dict(gate)
    _invalidate_snapshots(session, scan.id)
    _record_event(
        session, scan, entity_type="stage_gate", entity_id=gate.id, actor=data.actor,
        action="approve", before=before, after=after, note=data.note,
    )
    session.commit()

    # Persist the human decision in the report snapshot. Non-ready reports remain drafts.
    from app.reports.build_v2 import build_report_v2

    document = build_report_v2(session, scan_id, persist=True)
    snapshot = session.scalar(select(ReportSnapshot).where(
        ReportSnapshot.scan_id == scan_id,
        ReportSnapshot.content_hash == document.meta.content_hash,
    ))
    if snapshot is not None and data.decision == "ready":
        snapshot.status = "approved"
        snapshot.approved_by = data.actor.strip()
        snapshot.approved_at = gate.decided_at
        session.commit()
    return {
        **after,
        "audit_phase": scan.audit_phase,
        "report_status": document.meta.report_status,
        "content_hash": document.meta.content_hash,
    }
