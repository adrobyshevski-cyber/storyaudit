import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from app.db import Base


def uuid4() -> uuid.UUID:
    return uuid.uuid4()


def now() -> datetime:
    return datetime.now(timezone.utc)


class Scan(Base):
    __tablename__ = "scans"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    brand: Mapped[str | None] = mapped_column(String(200))
    audit_mode: Mapped[str] = mapped_column(String(12), default="desk", server_default="desk")
    snapshot_date: Mapped[date | None] = mapped_column(Date)
    geography: Mapped[str | None] = mapped_column(String(300))
    category_status: Mapped[str] = mapped_column(
        String(12), default="unknown", server_default="unknown"
    )
    missing_materials: Mapped[list[Any]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    client_brief: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    audit_phase: Mapped[str] = mapped_column(String(30), default="draft", server_default="draft")
    internal_metrics: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    corpus_hash: Mapped[str | None] = mapped_column(String(64))
    current_calculation_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    analysis_generation: Mapped[int] = mapped_column(Integer, default=0)
    methodology_version: Mapped[str] = mapped_column(String(80), default="methodology_v1")
    schema_version: Mapped[str] = mapped_column(String(80), default="schema_v1")
    prompt_version: Mapped[str] = mapped_column(String(80), default="prompt_v1")
    model_version: Mapped[str] = mapped_column(String(160), default="mock-v1")
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_summary: Mapped[str | None] = mapped_column(Text)


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    name: Mapped[str] = mapped_column(String(300))
    type: Mapped[str] = mapped_column(String(50), default="website")
    external_id: Mapped[str | None] = mapped_column(String(300))
    source_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    doc_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Block(Base):
    __tablename__ = "blocks"
    __table_args__ = (
        UniqueConstraint("document_id", "ordinal"),
        CheckConstraint("start_offset >= 0 AND end_offset > start_offset"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Proposition(Base):
    __tablename__ = "propositions"
    __table_args__ = (
        UniqueConstraint("block_id", "ordinal"),
        CheckConstraint("confidence >= 0 AND confidence <= 1"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    block_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("blocks.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Annotation(Base):
    __tablename__ = "annotations"
    __table_args__ = (
        UniqueConstraint("supersedes_annotation_id"),
        Index(
            "one_current_annotation",
            "scan_id", "agent", "object_type", "object_id",
            unique=True,
            postgresql_where=text("is_current = true"),
        ),
        Index("annotation_lookup", "scan_id", "agent", "object_type", "object_id", "is_current"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    agent: Mapped[str] = mapped_column(String(40))
    object_type: Mapped[str] = mapped_column(String(40))
    object_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    issue_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    analysis_generation: Mapped[int] = mapped_column(Integer)
    scope_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    methodology_version: Mapped[str] = mapped_column(String(80))
    schema_version: Mapped[str] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(80))
    model_version: Mapped[str] = mapped_column(String(160))
    supersedes_annotation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("annotations.id"))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class StageRun(Base):
    __tablename__ = "stage_runs"
    __table_args__ = (
        UniqueConstraint(
            "scan_id", "agent", "analysis_generation", "scope_type", "scope_id", "scope_hash",
            name="unique_stage_scope",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    agent: Mapped[str] = mapped_column(String(40))
    analysis_generation: Mapped[int] = mapped_column(Integer)
    scope_type: Mapped[str] = mapped_column(String(40))
    scope_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    scope_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    expected_job_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_job_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("pending_jobs", "available_at", postgresql_where=text("status = 'pending'")),
        Index("lease_jobs", "status", "lease_expires_at"),
        Index("scan_jobs", "scan_id", "status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    stage_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stage_runs.id"))
    analysis_generation: Mapped[int] = mapped_column(Integer)
    agent: Mapped[str] = mapped_column(String(40))
    job_type: Mapped[str] = mapped_column(String(30), default="initial")
    object_type: Mapped[str] = mapped_column(String(40))
    object_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    issue_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    reanalysis_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    scope_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    claimed_by: Mapped[str | None] = mapped_column(String(200))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_type: Mapped[str | None] = mapped_column(String(100))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class JobDependency(Base):
    __tablename__ = "job_dependencies"
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    depends_on_job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    __table_args__ = (CheckConstraint("job_id <> depends_on_job_id"),)


class LLMUsage(Base):
    """One row per provider request: volume for cost control and quality audits."""

    __tablename__ = "llm_usage"
    __table_args__ = (Index("llm_usage_scan", "scan_id", "created_at"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id"))
    stage_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("stage_runs.id"))
    agent: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(160))
    tier: Mapped[str] = mapped_column(String(20), default="smart")
    purpose: Mapped[str] = mapped_column(String(20), default="initial")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    is_estimated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CalculationRun(Base):
    __tablename__ = "calculation_runs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    analysis_generation: Mapped[int] = mapped_column(Integer)
    formula_version: Mapped[str] = mapped_column(String(80), default="formulas_v1")
    config_hash: Mapped[str] = mapped_column(String(64))
    input_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="running")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReanalysisRequest(Base):
    __tablename__ = "reanalysis_requests"
    __table_args__ = (UniqueConstraint("scan_id", "client_request_id"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    client_request_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    issue_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    agent: Mapped[str] = mapped_column(String(40))
    object_type: Mapped[str] = mapped_column(String(40))
    object_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    analysis_generation: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SourceProfile(Base):
    __tablename__ = "source_profiles"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"), index=True)
    calculation_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("calculation_runs.id"))
    analysis_generation: Mapped[int] = mapped_column(Integer)
    methodology_version: Mapped[str] = mapped_column(String(80), default="methodology_v1")
    formula_version: Mapped[str] = mapped_column(String(80), default="formulas_v1")
    config_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Metric(Base):
    __tablename__ = "metrics"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    calculation_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calculation_runs.id"), index=True)
    analysis_generation: Mapped[int] = mapped_column(Integer)
    source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sources.id"))
    code: Mapped[str] = mapped_column(String(80))
    value: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    breakdown: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    formula_version: Mapped[str] = mapped_column(String(80), default="formulas_v1")
    config_hash: Mapped[str] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Capital(Base):
    __tablename__ = "capitals"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    calculation_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calculation_runs.id"))
    code: Mapped[str] = mapped_column(String(80))
    value: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class IndexResult(Base):
    __tablename__ = "indices"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    calculation_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calculation_runs.id"))
    code: Mapped[str] = mapped_column(String(80), default="storyaudit_index")
    value: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Finding(Base):
    __tablename__ = "findings"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id"), index=True)
    metric_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("metrics.id"))
    calculation_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calculation_runs.id"))
    code: Mapped[str] = mapped_column(String(80))
    severity: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(200))
    explanation: Mapped[str] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FindingEvidence(Base):
    __tablename__ = "finding_evidence"
    finding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("findings.id"), primary_key=True)
    proposition_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("propositions.id"), primary_key=True)


class Recommendation(Base):
    __tablename__ = "recommendations"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    finding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("findings.id"), unique=True)
    title: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(Text)
    expected_effect: Mapped[str] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


# Register tables for the evidence-first report (report_v2). Imported for
# metadata registration only; the models themselves live in app/registers.
from app.registers import models as _register_models  # noqa: E402,F401
