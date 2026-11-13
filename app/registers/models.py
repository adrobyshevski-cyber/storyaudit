"""Register tables for the evidence-first report (report_v2).

These tables are additive: the immutable corpus (`sources` → `documents` →
`blocks` → `propositions`) keeps its update/delete triggers. Everything that a
human may revise lives here instead.

DDL reference: `docs/report-v2-schema.md`.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from app.db import Base


def uuid4() -> uuid.UUID:
    return uuid.uuid4()


def now() -> datetime:
    return datetime.now(timezone.utc)


class CodeSequence(Base):
    __tablename__ = "code_sequences"
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), primary_key=True)
    prefix: Mapped[str] = mapped_column(String(8), primary_key=True)
    next_value: Mapped[int] = mapped_column(Integer, default=1)


class SourceRegistryEntry(Base):
    __tablename__ = "source_registry"
    __table_args__ = (UniqueConstraint("scan_id", "code"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    code: Mapped[str] = mapped_column(String(12))
    source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sources.id"))
    url: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default="client")
    provenance: Mapped[str] = mapped_column(String(20), default="primary")
    availability: Mapped[str] = mapped_column(String(20), default="full")
    currency: Mapped[str] = mapped_column(String(20), default="current")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    live_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    confirms: Mapped[str | None] = mapped_column(Text)
    limitations: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    entry_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("scan_id", "code"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    code: Mapped[str] = mapped_column(String(12))
    name: Mapped[str] = mapped_column(String(300))
    audience: Mapped[str] = mapped_column(Text, default="unknown")
    trigger_situation: Mapped[str | None] = mapped_column(Text)
    promise: Mapped[str | None] = mapped_column(Text)
    format: Mapped[str | None] = mapped_column(Text)
    price: Mapped[str | None] = mapped_column(Text)
    procedure: Mapped[str | None] = mapped_column(Text)
    proof: Mapped[str | None] = mapped_column(Text)
    boundary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="current")
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    source_codes: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    entry_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LandscapeEntry(Base):
    __tablename__ = "landscape_entries"
    __table_args__ = (UniqueConstraint("scan_id", "code"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    code: Mapped[str] = mapped_column(String(12))
    name: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(Text)
    source_code: Mapped[str | None] = mapped_column(String(12))
    audience: Mapped[str | None] = mapped_column(Text)
    opponent: Mapped[str | None] = mapped_column(Text)
    object: Mapped[str | None] = mapped_column(Text)
    helper_role: Mapped[str | None] = mapped_column(Text)
    promise: Mapped[str | None] = mapped_column(Text)
    proof: Mapped[str | None] = mapped_column(Text)
    price: Mapped[str | None] = mapped_column(Text)
    inclusion_reason: Mapped[str] = mapped_column(Text)
    geographic_scope: Mapped[str | None] = mapped_column(Text)
    territory_notes: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Statement(Base):
    __tablename__ = "statements"
    __table_args__ = (UniqueConstraint("scan_id", "code"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    code: Mapped[str] = mapped_column(String(12))
    kind: Mapped[str] = mapped_column(String(2))
    sub_status: Mapped[str] = mapped_column(String(20), default="current")
    layer: Mapped[str] = mapped_column(String(20), default="na")
    text: Mapped[str] = mapped_column(Text)
    product_code: Mapped[str | None] = mapped_column(String(12))
    source_codes: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    confidence: Mapped[str] = mapped_column(String(10), default="medium")
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    review_status: Mapped[str] = mapped_column(String(20), default="machine")
    entry_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EvidenceRecord(Base):
    __tablename__ = "evidence_records"
    __table_args__ = (UniqueConstraint("scan_id", "code"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    code: Mapped[str] = mapped_column(String(12))
    statement_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("statements.id"))
    claim: Mapped[str] = mapped_column(Text)
    source_codes: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    provenance: Mapped[str] = mapped_column(String(30), default="primary")
    verification: Mapped[str] = mapped_column(String(30), default="publication_only")
    relevance: Mapped[str] = mapped_column(String(10), default="medium")
    freshness: Mapped[str] = mapped_column(String(10), default="current")
    limitations: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    allowed_conclusion: Mapped[str] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(String(10), default="medium")
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LayerClaim(Base):
    __tablename__ = "layer_claims"
    __table_args__ = (UniqueConstraint("scan_id", "code"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    code: Mapped[str] = mapped_column(String(12))
    promise: Mapped[str] = mapped_column(Text)
    declared: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    experienced: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    perceived: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    match_status: Mapped[str] = mapped_column(String(20), default="no_data")
    unknown_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ActantMap(Base):
    __tablename__ = "actant_maps"
    __table_args__ = (UniqueConstraint("scan_id", "code"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    code: Mapped[str] = mapped_column(String(12))
    product_code: Mapped[str | None] = mapped_column(String(12))
    narrative_arc: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ActantRole(Base):
    __tablename__ = "actant_roles"
    __table_args__ = (UniqueConstraint("map_id", "role", "object_level"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    map_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("actant_maps.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    object_level: Mapped[str | None] = mapped_column(String(20))
    reconstruction: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(2))
    alternative: Mapped[str | None] = mapped_column(Text)
    source_codes: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    evidence_codes: Mapped[list[Any]] = mapped_column(JSONB, default=list)


class JourneyStep(Base):
    __tablename__ = "journey_steps"
    __table_args__ = (UniqueConstraint("scan_id", "step"), UniqueConstraint("scan_id", "ordinal"))
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    step: Mapped[str] = mapped_column(String(20))
    client_question: Mapped[str] = mapped_column(Text)
    public_state: Mapped[str] = mapped_column(Text, default="нет данных")
    obstacle: Mapped[str | None] = mapped_column(Text)
    source_codes: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    check_method: Mapped[str | None] = mapped_column(Text)
    is_observed: Mapped[bool] = mapped_column(Boolean, default=False)


class ConsistencyCell(Base):
    __tablename__ = "consistency_cells"
    __table_args__ = (UniqueConstraint("scan_id", "actant_role", "dimension", "note"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    actant_role: Mapped[str] = mapped_column(String(20))
    dimension: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    source_codes: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    counterexample: Mapped[str | None] = mapped_column(Text)


class FindingV2(Base):
    __tablename__ = "findings_v2"
    __table_args__ = (UniqueConstraint("scan_id", "code"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    code: Mapped[str] = mapped_column(String(12))
    kind: Mapped[str] = mapped_column(String(20))
    statement: Mapped[str] = mapped_column(Text)
    basis_codes: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    consequence: Mapped[str | None] = mapped_column(Text)
    alternative_explanation: Mapped[str | None] = mapped_column(Text)
    verification: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(10), default="P2")
    confidence: Mapped[str] = mapped_column(String(10), default="medium")
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    review_status: Mapped[str] = mapped_column(String(20), default="machine")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LanguageCode(Base):
    __tablename__ = "language_codes"
    __table_args__ = (UniqueConstraint("scan_id", "code"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    code: Mapped[str] = mapped_column(String(12))
    topic: Mapped[str] = mapped_column(Text)
    professional: Mapped[str | None] = mapped_column(Text)
    lived: Mapped[str | None] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(Text)
    match_status: Mapped[str] = mapped_column(String(20), default="partial")
    is_illustration: Mapped[bool] = mapped_column(Boolean, default=False)
    source_codes: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ScorecardEntry(Base):
    __tablename__ = "scorecard_entries"
    __table_args__ = (
        UniqueConstraint("scan_id", "ordinal"),
        CheckConstraint("score IS NULL OR (score >= 0 AND score <= 10)"),
        CheckConstraint("score IS NOT NULL OR na_reason IS NOT NULL"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    dimension: Mapped[str] = mapped_column(String(20))
    criterion: Mapped[str] = mapped_column(Text)
    score: Mapped[Decimal | None] = mapped_column(Numeric(3, 1))
    na_reason: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[str | None] = mapped_column(String(10))
    rationale: Mapped[str] = mapped_column(Text)
    source_codes: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    entry_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class RecommendationV2(Base):
    __tablename__ = "recommendations_v2"
    __table_args__ = (UniqueConstraint("scan_id", "code"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    code: Mapped[str] = mapped_column(String(12))
    change: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text)
    expected_effect: Mapped[str | None] = mapped_column(Text)
    expected_effect_status: Mapped[str] = mapped_column(String(2), default="H")
    impact: Mapped[str | None] = mapped_column(String(10))
    evidence_confidence: Mapped[str | None] = mapped_column(String(10))
    urgency: Mapped[str | None] = mapped_column(String(10))
    effort: Mapped[str | None] = mapped_column(String(10))
    priority: Mapped[str] = mapped_column(String(10), default="P2")
    decision_class: Mapped[str] = mapped_column(String(10), default="fix")
    owner: Mapped[str | None] = mapped_column(String(200))
    kpi_codes: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    review_status: Mapped[str] = mapped_column(String(20), default="machine")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RecommendationFinding(Base):
    __tablename__ = "recommendation_findings"
    recommendation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recommendations_v2.id"), primary_key=True
    )
    finding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("findings_v2.id"), primary_key=True)


class Kpi(Base):
    __tablename__ = "kpis"
    __table_args__ = (UniqueConstraint("scan_id", "code"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    code: Mapped[str] = mapped_column(String(12))
    name: Mapped[str] = mapped_column(Text)
    definition: Mapped[str] = mapped_column(Text)
    baseline: Mapped[str] = mapped_column(Text, default="unknown")
    period: Mapped[str | None] = mapped_column(Text)
    data_source: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str | None] = mapped_column(Text)
    target: Mapped[str] = mapped_column(Text, default="определить после baseline")
    limitations: Mapped[str | None] = mapped_column(Text)


class RoadmapPhase(Base):
    __tablename__ = "roadmap_phases"
    __table_args__ = (UniqueConstraint("scan_id", "ordinal"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    dependencies: Mapped[str | None] = mapped_column(Text)
    actions: Mapped[str | None] = mapped_column(Text)
    artifact: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str | None] = mapped_column(Text)
    done_criteria: Mapped[str | None] = mapped_column(Text)
    kpi_codes: Mapped[list[Any]] = mapped_column(JSONB, default=list)


class StageGate(Base):
    __tablename__ = "stage_gate"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), unique=True)
    decision: Mapped[str] = mapped_column(String(20))
    rationale: Mapped[str] = mapped_column(Text)
    criteria: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    blockers: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    required_validations: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    approved_by: Mapped[str | None] = mapped_column(String(200))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AuditHypothesis(Base):
    __tablename__ = "audit_hypotheses"
    __table_args__ = (UniqueConstraint("scan_id", "code"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    code: Mapped[str] = mapped_column(String(12))
    statement: Mapped[str] = mapped_column(Text)
    how_to_verify: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open")


class OpenQuestion(Base):
    __tablename__ = "open_questions"
    __table_args__ = (UniqueConstraint("scan_id", "ordinal"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    question: Mapped[str] = mapped_column(Text)
    target: Mapped[str] = mapped_column(String(20), default="client")
    status: Mapped[str] = mapped_column(String(20), default="open")


class TraceLink(Base):
    __tablename__ = "trace_links"
    __table_args__ = (
        UniqueConstraint("from_type", "from_id", "to_type", "to_id", "relation"),
        Index("trace_links_to", "to_type", "to_id"),
        Index("trace_links_from", "from_type", "from_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    from_type: Mapped[str] = mapped_column(String(30))
    from_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    to_type: Mapped[str] = mapped_column(String(30))
    to_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    relation: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ReviewEvent(Base):
    __tablename__ = "review_events"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    actor: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(20))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ReportSnapshot(Base):
    __tablename__ = "report_snapshots"
    __table_args__ = (
        UniqueConstraint("scan_id", "content_hash"),
        Index("report_snapshots_latest", "scan_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    report_version: Mapped[str] = mapped_column(String(20), default="report_v2")
    methodology_version: Mapped[str] = mapped_column(String(40), default="socode_v1.1")
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="fresh")
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(200))


class ReportArtifact(Base):
    __tablename__ = "report_artifacts"
    __table_args__ = (UniqueConstraint("scan_id", "content_hash", "media_type"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(80), default="application/pdf")
    content: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ReportNarrative(Base):
    __tablename__ = "report_narratives"
    __table_args__ = (UniqueConstraint("scan_id", "section_id"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    section_id: Mapped[str] = mapped_column(String(8))
    paragraphs: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    prompt_version: Mapped[str] = mapped_column(String(40), default="narrative_v1")
    model_version: Mapped[str] = mapped_column(String(160), default="mock-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
