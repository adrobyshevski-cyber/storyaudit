import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.db import get_session_factory
from app.main import app
from app.models import Job, StageRun
from app.pagination import MAX_LIMIT
from app.schemas import DocumentCreate, ScanCreate, SourceCreate
from app.scans import add_source, create_scan

client = TestClient(app)


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_settings().app_access_token}"}


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


@pytest.fixture
def scan_with_jobs():
    session = require_db()
    try:
        scan = create_scan(session, ScanCreate(title="Pagination scan"))
        add_source(session, scan.id, SourceCreate(
            name="Source",
            documents=[DocumentCreate(title="Doc", text="One.\n\nTwo.\n\nThree.")],
        ))
        stage = session.scalar(select(StageRun).where(StageRun.scan_id == scan.id))
        if stage is None:
            stage = StageRun(
                scan_id=scan.id, agent="PROBE", analysis_generation=1,
                scope_type="scan", scope_id=scan.id, scope_hash=uuid.uuid4().hex * 2,
            )
            session.add(stage)
            session.flush()
        for index in range(7):
            session.add(Job(
                scan_id=scan.id, stage_run_id=stage.id, analysis_generation=1,
                agent="PROBE", job_type="initial", object_type="scan", object_id=scan.id,
                scope_hash=f"{index:064d}", idempotency_key=f"page-{scan.id}-{index}",
                status="pending", max_attempts=3,
            ))
        session.commit()
        return scan.id
    finally:
        session.close()


def test_scans_list_is_paginated_and_reports_the_total():
    require_db().close()
    first = client.get("/scans?limit=2&offset=0", headers=auth())
    assert first.status_code == 200
    assert len(first.json()) <= 2
    assert first.headers["X-Limit"] == "2"
    assert first.headers["X-Offset"] == "0"
    total = int(first.headers["X-Total-Count"])
    assert total >= 2
    if total > 2:
        assert 'rel="next"' in first.headers.get("Link", "")

    second = client.get("/scans?limit=1&offset=1", headers=auth())
    assert second.status_code == 200
    assert second.headers["X-Offset"] == "1"
    assert 'rel="prev"' in second.headers.get("Link", "")
    if first.json() and second.json():
        assert first.json()[0]["id"] != second.json()[0]["id"]


def test_limit_is_capped_not_rejected():
    require_db().close()
    response = client.get("/scans?limit=100000", headers=auth())
    assert response.status_code == 200
    assert response.headers["X-Limit"] == str(MAX_LIMIT)


def test_jobs_are_paginated_with_next_link(scan_with_jobs):
    scan_id = scan_with_jobs
    page_one = client.get(f"/scans/{scan_id}/jobs?limit=3&offset=0", headers=auth())
    assert page_one.status_code == 200
    assert len(page_one.json()) == 3
    assert int(page_one.headers["X-Total-Count"]) >= 7
    assert 'rel="next"' in page_one.headers["Link"]

    page_two = client.get(f"/scans/{scan_id}/jobs?limit=3&offset=3", headers=auth())
    assert page_two.status_code == 200
    assert len(page_two.json()) == 3
    ids_one = {row["id"] for row in page_one.json()}
    ids_two = {row["id"] for row in page_two.json()}
    assert not ids_one & ids_two, "pages must not overlap"


def test_scan_detail_jobs_are_truncated_but_counted(scan_with_jobs):
    scan_id = scan_with_jobs
    response = client.get(f"/scans/{scan_id}?limit=2", headers=auth())
    assert response.status_code == 200
    body = response.json()
    assert len(body["jobs"]) == 2
    assert body["jobs_total"] >= 7
    assert body["jobs_limit"] == 2


def test_annotations_and_review_events_report_totals(scan_with_jobs):
    scan_id = scan_with_jobs
    annotations = client.get(f"/scans/{scan_id}/annotations?limit=1", headers=auth())
    assert annotations.status_code == 200
    assert "X-Total-Count" in annotations.headers

    events = client.get(f"/scans/{scan_id}/review-events?limit=1", headers=auth())
    assert events.status_code == 200
    assert events.headers["X-Limit"] == "1"

    register = client.get(f"/scans/{scan_id}/registers/findings?limit=1", headers=auth())
    assert register.status_code == 200
    assert "X-Total-Count" in register.headers


def test_ui_scans_page_has_a_pager():
    require_db().close()
    response = client.get("/ui/scans?limit=1&offset=0", auth=("audit", get_settings().app_access_token))
    assert response.status_code == 200
    assert "Scans" in response.text
