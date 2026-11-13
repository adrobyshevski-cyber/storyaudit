import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.db import get_session_factory
from app.models import Annotation, LLMUsage, Proposition
from app.registers.models import ReportArtifact
from app.retention import (
    prune_llm_usage,
    prune_report_artifacts,
    prune_superseded_annotations,
    run_retention,
    table_sizes,
)
from app.schemas import DocumentCreate, ScanCreate, SourceCreate
from app.scans import add_source, create_scan

PAYLOAD = {"customer": {"label": "customer", "confidence": 0.9}}


def require_db():
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    session = get_session_factory()()
    try:
        session.execute(text("SELECT 1"))
    except OperationalError:
        session.close()
        pytest.skip("PostgreSQL is not available")
    return session


@pytest.fixture(autouse=True)
def reset_settings():
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def clean_tables():
    """Retention is global, so leftover rows from another test would skew counts."""
    if not os.environ.get("TEST_DATABASE_URL"):
        yield
        return
    session = get_session_factory()()
    try:
        session.execute(text("DELETE FROM report_artifacts"))
        session.execute(text("DELETE FROM llm_usage"))
        session.commit()
    finally:
        session.close()
    yield


def make_scan(session) -> uuid.UUID:
    scan = create_scan(session, ScanCreate(title="Retention probe"))
    add_source(session, scan.id, SourceCreate(
        name="Retention source",
        documents=[DocumentCreate(title="Doc", text="One statement.\n\nAnother statement.")],
    ))
    return scan.id


def artifact(session, scan_id, days_ago: int, size: int = 1024) -> ReportArtifact:
    row = ReportArtifact(
        scan_id=scan_id,
        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        media_type="application/pdf",
        content=b"%PDF" + b"0" * size,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    session.add(row)
    return row


def test_report_artifacts_keep_latest_per_scan_and_drop_old():
    session = require_db()
    try:
        scan_id = make_scan(session)
        for days in (0, 1, 2, 40, 60):
            artifact(session, scan_id, days)
        session.commit()

        report = prune_report_artifacts(
            session, older_than_days=30, keep_per_scan=3, dry_run=True
        )
        # The 40- and 60-day snapshots are older than the cutoff; the newest three
        # stay, because keep_per_scan is a floor for recent versions.
        assert report.deleted == 2
        assert report.freed_bytes > 0
        assert session.scalar(
            select(ReportArtifact.id).where(ReportArtifact.scan_id == scan_id).limit(1)
        )
        assert len(list(session.scalars(
            select(ReportArtifact).where(ReportArtifact.scan_id == scan_id)
        ))) == 5, "dry run must not delete"

        applied = prune_report_artifacts(
            session, older_than_days=30, keep_per_scan=3, dry_run=False
        )
        session.commit()
        assert applied.deleted == 2
        left = list(session.scalars(
            select(ReportArtifact).where(ReportArtifact.scan_id == scan_id)
        ))
        assert len(left) == 3
        # Nothing older than the cutoff survives when it is not the newest snapshot.
        assert min(row.created_at for row in left) > datetime.now(timezone.utc) - timedelta(days=30)
    finally:
        session.close()


def test_newest_artifact_survives_even_if_everything_is_old():
    session = require_db()
    try:
        scan_id = make_scan(session)
        artifact(session, scan_id, 400)
        artifact(session, scan_id, 300)
        session.commit()
        report = prune_report_artifacts(
            session, older_than_days=90, keep_per_scan=1, dry_run=False
        )
        session.commit()
        assert report.deleted == 1
        assert len(list(session.scalars(
            select(ReportArtifact).where(ReportArtifact.scan_id == scan_id)
        ))) == 1
    finally:
        session.close()


def test_llm_usage_pruned_by_age():
    session = require_db()
    try:
        scan_id = make_scan(session)
        old = LLMUsage(
            scan_id=scan_id, agent="A2", model="m", tier="fast",
            prompt_tokens=10, completion_tokens=5, total_tokens=15,
            created_at=datetime.now(timezone.utc) - timedelta(days=400),
        )
        fresh = LLMUsage(
            scan_id=scan_id, agent="A2", model="m", tier="fast",
            prompt_tokens=10, completion_tokens=5, total_tokens=15,
        )
        session.add_all([old, fresh])
        session.commit()
        assert prune_llm_usage(session, older_than_days=365, dry_run=True).deleted >= 1
        report = prune_llm_usage(session, older_than_days=365, dry_run=False)
        session.commit()
        assert report.deleted >= 1
        remaining = list(session.scalars(select(LLMUsage).where(LLMUsage.scan_id == scan_id)))
        assert len(remaining) == 1
        assert remaining[0].created_at > datetime.now(timezone.utc) - timedelta(days=1)
    finally:
        session.close()


def test_superseded_annotations_pruned_and_link_cleared():
    session = require_db()
    try:
        scan_id = make_scan(session)
        block_id = session.scalar(text(
            "SELECT b.id FROM blocks b JOIN documents d ON b.document_id = d.id "
            "JOIN sources s ON d.source_id = s.id WHERE s.scan_id = :scan LIMIT 1"
        ), {"scan": scan_id})
        proposition = Proposition(block_id=block_id, ordinal=0, text="Statement.", confidence=1)
        session.add(proposition)
        session.flush()
        proposition_id = proposition.id
        old = Annotation(
            scan_id=scan_id, agent="A2", object_type="proposition", object_id=proposition_id,
            analysis_generation=1, scope_hash="a" * 64, payload=PAYLOAD,
            methodology_version="methodology_v1", schema_version="schema_v1",
            prompt_version="prompt_v1", model_version="mock-v1",
            is_current=False, created_at=datetime.now(timezone.utc) - timedelta(days=200),
        )
        session.add(old)
        session.flush()
        current = Annotation(
            scan_id=scan_id, agent="A2", object_type="proposition", object_id=proposition_id,
            analysis_generation=2, scope_hash="b" * 64, payload=PAYLOAD,
            methodology_version="methodology_v1", schema_version="schema_v1",
            prompt_version="prompt_v1", model_version="mock-v1",
            is_current=True, supersedes_annotation_id=old.id,
        )
        session.add(current)
        session.commit()
        # Remember the ids: after the prune, refreshing a deleted instance raises.
        current_id, old_id = current.id, old.id

        report = prune_superseded_annotations(session, older_than_days=90, dry_run=False)
        session.commit()
        assert report.deleted == 1
        # The update ran as SQL, so drop the identity-map copy before re-reading.
        session.expire_all()
        survivor = session.get(Annotation, current_id)
        assert survivor is not None
        assert survivor.supersedes_annotation_id is None, "dangling reference must be cleared"
        # session.get() raises for an identity-mapped deleted object, so count rows.
        assert session.scalar(
            text("SELECT count(*) FROM annotations WHERE id = :id"), {"id": old_id}
        ) == 0
    finally:
        session.close()


def test_run_retention_reports_tables_and_sizes(monkeypatch):
    session = require_db()
    monkeypatch.setenv("RETENTION_LLM_USAGE_DAYS", "30")
    monkeypatch.setenv("RETENTION_REPORT_ARTIFACTS_KEEP", "1")
    monkeypatch.setenv("RETENTION_ANNOTATIONS_DAYS", "0")
    get_settings.cache_clear()
    try:
        scan_id = make_scan(session)
        artifact(session, scan_id, 200)
        artifact(session, scan_id, 100)
        session.commit()

        dry = run_retention(session, dry_run=True)
        assert dry["dry_run"] is True
        assert [row["table"] for row in dry["tables"]] == ["report_artifacts", "llm_usage"]
        assert dry["deleted"] >= 1 and dry["freed_bytes"] > 0
        assert "report_artifacts" in dry["sizes"]
        assert len(list(session.scalars(
            select(ReportArtifact).where(ReportArtifact.scan_id == scan_id)
        ))) == 2, "dry run must roll back"

        applied = run_retention(session, dry_run=False)
        assert applied["deleted"] >= 1
        assert len(list(session.scalars(
            select(ReportArtifact).where(ReportArtifact.scan_id == scan_id)
        ))) == 1
        assert table_sizes(session)
    finally:
        session.close()
