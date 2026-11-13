import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.db import get_session_factory
from app.errors import AppError
from app.ratelimit import counters, guard_report_pdf, hit
from app.main import app
from app.schemas import DocumentCreate, ScanCreate, SourceCreate
from app.scans import add_source, add_website_source, create_scan, run_scan
from app.schemas import WebsiteSourceCreate

client = TestClient(app)


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
def clean_counters():
    if not os.environ.get("TEST_DATABASE_URL"):
        yield
        return
    session = get_session_factory()()
    try:
        session.execute(text("DELETE FROM rate_limit_counters"))
        session.commit()
    finally:
        session.close()
    yield


@pytest.fixture(autouse=True)
def reset_settings():
    yield
    get_settings.cache_clear()


def test_hit_allows_up_to_the_limit_then_raises_429():
    require_db().close()
    bucket = f"probe:{uuid.uuid4()}"
    for _ in range(3):
        hit(bucket, limit=3, window_seconds=3600)
    with pytest.raises(AppError) as excinfo:
        hit(bucket, limit=3, window_seconds=3600)
    error = excinfo.value
    assert error.status_code == 429
    assert error.code == "rate_limited"
    retry_after = int(error.headers["Retry-After"])
    assert 1 <= retry_after <= 3600
    assert "Повторите через" in error.message


def test_buckets_are_independent():
    require_db().close()
    first, second = f"a:{uuid.uuid4()}", f"b:{uuid.uuid4()}"
    hit(first, limit=1, window_seconds=3600)
    with pytest.raises(AppError):
        hit(first, limit=1, window_seconds=3600)
    hit(second, limit=1, window_seconds=3600)  # separate budget, no error
    hit(f"{first}:other", limit=5, window_seconds=3600)


def test_counters_are_visible_across_sessions():
    session = require_db()
    try:
        bucket = f"shared:{uuid.uuid4()}"
        hit(bucket, limit=10, window_seconds=3600)
        rows = [row for row in counters(session) if row["bucket"] == bucket]
        assert len(rows) == 1
        assert rows[0]["count"] == 1
    finally:
        session.close()


def test_limits_can_be_disabled(monkeypatch):
    require_db().close()
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    get_settings.cache_clear()
    bucket = f"disabled:{uuid.uuid4()}"
    for _ in range(5):
        hit(bucket, limit=1, window_seconds=3600)


def test_scan_creation_is_limited(monkeypatch):
    session = require_db()
    monkeypatch.setenv("RATE_LIMIT_SCANS_PER_DAY", "1")
    get_settings.cache_clear()
    try:
        create_scan(session, ScanCreate(title="First allowed"))
        with pytest.raises(AppError) as excinfo:
            create_scan(session, ScanCreate(title="Second blocked"))
        assert excinfo.value.status_code == 429
        assert excinfo.value.code == "rate_limited"
    finally:
        session.close()


def test_source_guard_is_per_scan(monkeypatch):
    """The guarded path is the network fetch, so crawl_site is stubbed out."""
    from app import scans as scans_module

    session = require_db()
    monkeypatch.setenv("RATE_LIMIT_SOURCES_PER_HOUR", "1")
    get_settings.cache_clear()

    class Page:
        url = "https://example.com/"
        title = "Example"
        text = "Statement about the product."
        paragraphs = ["Statement about the product."]

    monkeypatch.setattr(scans_module, "crawl_site", lambda *args, **kwargs: [Page()])
    try:
        first = create_scan(session, ScanCreate(title="Limits A"))
        second = create_scan(session, ScanCreate(title="Limits B"))
        add_website_source(session, first.id, WebsiteSourceCreate(url="https://example.com"))
        with pytest.raises(AppError) as excinfo:
            add_website_source(session, first.id, WebsiteSourceCreate(url="https://example.com/2"))
        assert excinfo.value.status_code == 429
        # A different scan has its own budget.
        add_website_source(session, second.id, WebsiteSourceCreate(url="https://example.com"))
    finally:
        session.close()


def test_run_guard_fires_before_the_state_check(monkeypatch):
    session = require_db()
    monkeypatch.setenv("RATE_LIMIT_RUNS_PER_HOUR", "1")
    get_settings.cache_clear()
    try:
        scan = create_scan(session, ScanCreate(title="Run limits"))
        add_source(session, scan.id, SourceCreate(
            name="Site", documents=[DocumentCreate(title="Doc", text="Statement.")],
        ))
        run_scan(session, scan.id)
        with pytest.raises(AppError) as excinfo:
            run_scan(session, scan.id)
        # Guard runs first, so the answer is 429, not the 409 "already started".
        assert excinfo.value.status_code == 429
    finally:
        session.close()


def test_api_returns_429_with_retry_after(monkeypatch):
    require_db().close()
    monkeypatch.setenv("RATE_LIMIT_SCANS_PER_DAY", "1")
    get_settings.cache_clear()
    headers = {"Authorization": f"Bearer {get_settings().app_access_token}"}
    first = client.post("/scans", json={"title": "API allowed"}, headers=headers)
    assert first.status_code == 201, first.text
    second = client.post("/scans", json={"title": "API blocked"}, headers=headers)
    assert second.status_code == 429, second.text
    body = second.json()["error"]
    assert body["code"] == "rate_limited"
    assert int(second.headers["Retry-After"]) >= 1


def test_report_pdf_guard_is_wired(monkeypatch):
    require_db().close()
    monkeypatch.setenv("RATE_LIMIT_REPORT_PDFS_PER_HOUR", "1")
    get_settings.cache_clear()
    scan_id = uuid.uuid4()
    guard_report_pdf(scan_id)
    with pytest.raises(AppError) as excinfo:
        guard_report_pdf(scan_id)
    assert excinfo.value.status_code == 429
    # Another scan is unaffected.
    guard_report_pdf(uuid.uuid4())


def test_guard_does_not_fail_when_counters_table_is_unavailable(monkeypatch):
    """A broken counter must never block the product."""
    require_db().close()
    from app import ratelimit as module

    class Boom:
        def __enter__(self):
            raise RuntimeError("database is gone")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(module, "get_session_factory", lambda: lambda: Boom())
    hit(f"degraded:{uuid.uuid4()}", limit=1, window_seconds=60)  # no exception
