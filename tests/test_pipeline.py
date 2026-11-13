import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.agents import execute_job
from app.api import metric_view
from app.db import get_session_factory
from app.models import Annotation, Job, Metric, Scan
from app.pipeline import create_reanalysis, schedule_after_success
from app.queue import claim_next_job, complete_job
from app.scans import add_source, create_scan, run_scan
from app.schemas import DocumentCreate, ReanalysisCreate, ScanCreate, SourceCreate


def drain(session) -> None:
    for _ in range(2000):
        job = claim_next_job(session, f"test-{uuid.uuid4()}")
        if job is None:
            return
        execute_job(session, job)
        assert complete_job(session, job)
        schedule_after_success(session, job)
        session.commit()
    raise AssertionError("Job drain limit exceeded")


def test_full_pipeline_and_reanalysis():
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    factory = get_session_factory()
    with factory() as session:
        try:
            session.execute(text("SELECT 1"))
        except OperationalError:
            pytest.skip("PostgreSQL is not available")
        scan = create_scan(session, ScanCreate(title="Integration pipeline"))
        add_source(session, scan.id, SourceCreate(
            name="Integration source",
            documents=[DocumentCreate(title="Doc", text="Teams miss customer feedback.\n\nStoryAudit clarifies the narrative.")],
        ))
        run_scan(session, scan.id)
        drain(session)
        scan = session.get(Scan, scan.id)
        assert scan.status == "succeeded"
        metrics = list(session.scalars(select(Metric).where(Metric.scan_id == scan.id, Metric.is_current.is_(True))))
        assert len(metrics) == 4
        detail = metric_view(scan.id, metrics[0].id, session)
        assert detail["breakdown"]["sources"]
        proposition_id = session.scalar(select(Annotation.object_id).where(
            Annotation.scan_id == scan.id,
            Annotation.agent == "A2",
            Annotation.object_type == "proposition",
        ))
        result = create_reanalysis(session, scan.id, ReanalysisCreate(
            agent="A3", object_type="proposition", object_id=proposition_id,
            issue_id=uuid.uuid4(), client_request_id=uuid.uuid4(),
        ))
        assert result["analysis_generation"] == 2
        drain(session)
        assert session.get(Scan, scan.id).status == "succeeded"
        history = list(session.scalars(select(Annotation).where(
            Annotation.scan_id == scan.id,
            Annotation.agent == "A3",
            Annotation.object_id == proposition_id,
        )))
        assert len(history) == 2
        assert sum(item.is_current for item in history) == 1
        assert not session.scalar(select(Job).where(Job.scan_id == scan.id, Job.status == "failed"))
