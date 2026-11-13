import os
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from app.agents import _check_batch_answer, execute_job
from app.config import get_settings
from app.db import get_session_factory
from app.models import Annotation, Block, Document, Job, Proposition, Scan, Source
from app.pipeline import create_reanalysis, schedule_after_success
from app.queue import claim_next_job, complete_job
from app.schemas import A2BatchResult, DocumentCreate, ReanalysisCreate, ScanCreate, SourceCreate
from app.scans import add_source, create_scan, run_scan

PARAGRAPHS = 5


def drain(session) -> None:
    for _ in range(4000):
        job = claim_next_job(session, f"test-{uuid.uuid4()}")
        if job is None:
            return
        execute_job(session, job)
        assert complete_job(session, job)
        schedule_after_success(session, job)
        session.commit()
    raise AssertionError("Job drain limit exceeded")


def run_corpus(session) -> Scan:
    scan = create_scan(session, ScanCreate(title="Batching"))
    body = "\n\n".join(f"Statement number {index} about the product." for index in range(PARAGRAPHS))
    add_source(session, scan.id, SourceCreate(
        name="Batch source",
        documents=[DocumentCreate(title="Doc", text=body)],
    ))
    run_scan(session, scan.id)
    drain(session)
    return session.get(Scan, scan.id)


def proposition_count(session, scan_id: uuid.UUID) -> int:
    return session.scalar(
        select(func.count(Proposition.id))
        .join(Block, Proposition.block_id == Block.id)
        .join(Document, Block.document_id == Document.id)
        .join(Source, Document.source_id == Source.id)
        .where(Source.scan_id == scan_id)
    )


def require_db():
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    factory = get_session_factory()
    session = factory()
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


def test_chunked_fanout_calls_provider_once_per_chunk(monkeypatch):
    session = require_db()
    monkeypatch.setenv("ANNOTATION_BATCH_SIZE", "2")
    get_settings.cache_clear()
    try:
        scan = run_corpus(session)
        assert scan.status == "succeeded"

        total = proposition_count(session, scan.id)
        assert total == PARAGRAPHS
        expected_per_agent = -(-total // 2)

        batch_jobs = list(session.scalars(select(Job).where(
            Job.scan_id == scan.id,
            Job.agent.in_(["A2", "A3", "A4"]),
            Job.job_type == "initial",
        )))
        assert len(batch_jobs) == expected_per_agent * 3
        assert all(job.object_type == "proposition_batch" for job in batch_jobs)
        for agent in ("A2", "A3", "A4"):
            assert len([job for job in batch_jobs if job.agent == agent]) == expected_per_agent

        annotations = list(session.scalars(select(Annotation).where(
            Annotation.scan_id == scan.id,
            Annotation.agent.in_(["A2", "A3", "A4"]),
            Annotation.object_type == "proposition",
            Annotation.is_current.is_(True),
        )))
        # Still one annotation per proposition, not one per batch.
        assert len(annotations) == PARAGRAPHS * 3
        assert all("proposition_id" not in item.payload for item in annotations)
        a2_payload = next(item.payload for item in annotations if item.agent == "A2")
        assert set(a2_payload) == {"customer", "problem", "object", "promise"}
    finally:
        session.close()


def test_batch_size_one_keeps_single_proposition_jobs(monkeypatch):
    session = require_db()
    monkeypatch.setenv("ANNOTATION_BATCH_SIZE", "1")
    get_settings.cache_clear()
    try:
        scan = run_corpus(session)
        assert scan.status == "succeeded"
        jobs = list(session.scalars(select(Job).where(
            Job.scan_id == scan.id,
            Job.agent.in_(["A2", "A3", "A4"]),
            Job.job_type == "initial",
        )))
        assert all(job.object_type == "proposition" for job in jobs)
        assert len(jobs) == PARAGRAPHS * 3
    finally:
        session.close()


def test_reanalysis_stays_single_proposition(monkeypatch):
    session = require_db()
    monkeypatch.setenv("ANNOTATION_BATCH_SIZE", "3")
    get_settings.cache_clear()
    try:
        scan = run_corpus(session)
        proposition_id = session.scalar(select(Annotation.object_id).where(
            Annotation.scan_id == scan.id,
            Annotation.agent == "A2",
            Annotation.object_type == "proposition",
        ))
        result = create_reanalysis(session, scan.id, ReanalysisCreate(
            agent="A3", object_type="proposition", object_id=proposition_id,
            issue_id=uuid.uuid4(), client_request_id=uuid.uuid4(),
        ))
        drain(session)
        reanalysis_job = session.get(Job, uuid.UUID(result["job_ids"][0]))
        assert reanalysis_job.object_type == "proposition"
        assert reanalysis_job.job_type == "reanalysis"
        history = list(session.scalars(select(Annotation).where(
            Annotation.scan_id == scan.id,
            Annotation.agent == "A3",
            Annotation.object_id == proposition_id,
        )))
        assert len(history) == 2
    finally:
        session.close()


def test_batch_answer_guard_rejects_missing_foreign_and_duplicates():
    requested = [str(uuid.uuid4()) for _ in range(3)]
    label = {"label": "customer", "confidence": 0.9}

    def answer(ids):
        return {"items": [
            {
                "proposition_id": item,
                "customer": label, "problem": label, "object": label, "promise": label,
            }
            for item in ids
        ]}

    _check_batch_answer(requested, answer(requested))
    with pytest.raises(ValueError, match="missing"):
        _check_batch_answer(requested, answer(requested[:2]))
    with pytest.raises(ValueError, match="foreign"):
        _check_batch_answer(requested, answer(requested + [str(uuid.uuid4())]))
    with pytest.raises(ValueError, match="repeats"):
        _check_batch_answer(requested, answer(requested[:2] + requested[:1]))


def test_batch_schema_requires_proposition_id():
    with pytest.raises(Exception):
        A2BatchResult.model_validate({"items": [{
            "customer": {"label": "a", "confidence": 0.5},
            "problem": {"label": "b", "confidence": 0.5},
            "object": {"label": "c", "confidence": 0.5},
            "promise": {"label": "d", "confidence": 0.5},
        }]})
