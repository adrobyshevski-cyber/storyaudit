import http.server
import os
import threading

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.errors import AppError
from app.schemas import ScanCreate, WebsiteSourceCreate
from app.scans import add_website_source, create_scan, run_scan
from app.website import crawl_site

PAGES = {
    "/robots.txt": b"User-agent: *\nAllow: /\nDisallow: /private\n",
    "/client": b"""<html><head><title>Client practice</title></head><body>
        <p>\xd0\x9c\xd1\x8b \xd0\xbf\xd0\xbe\xd0\xbc\xd0\xbe\xd0\xb3\xd0\xb0\xd0\xb5\xd0\xbc \xd0\xbf\xd1\x81\xd0\xb8\xd1\x85\xd0\xbe\xd0\xbb\xd0\xbe\xd0\xb3\xd0\xb0\xd0\xbc \xd1\x83\xd0\xb2\xd0\xb8\xd0\xb4\xd0\xb5\xd1\x82\xd1\x8c \xd1\x81\xd0\xbb\xd1\x83\xd1\x87\xd0\xb0\xd0\xb9 \xd1\x86\xd0\xb5\xd0\xbb\xd0\xb8\xd0\xba\xd0\xbe\xd0\xbc \xd0\xb8 \xd0\xbd\xd0\xb0\xd0\xb9\xd1\x82\xd0\xb8 \xd1\x81\xd0\xbb\xd0\xb5\xd0\xb4\xd1\x83\xd1\x8e\xd1\x89\xd0\xb8\xd0\xb9 \xd1\x88\xd0\xb0\xd0\xb3.</p>
        <p>\xd0\x98\xd0\xbd\xd0\xb4\xd0\xb8\xd0\xb2\xd0\xb8\xd0\xb4\xd1\x83\xd0\xb0\xd0\xbb\xd1\x8c\xd0\xbd\xd0\xb0\xd1\x8f \xd1\x81\xd1\x83\xd0\xbf\xd0\xb5\xd1\x80\xd0\xb2\xd0\xb8\xd0\xb7\xd0\xb8\xd1\x8f, 60 \xd0\xbc\xd0\xb8\xd0\xbd\xd1\x83\xd1\x82, 18 000 \xe2\x82\xbd.</p>
        </body></html>""",
    "/comp1": b"""<html><head><title>Competitor one</title></head><body>
        <p>\xd0\x9f\xd0\xbe\xd0\xbc\xd0\xbe\xd0\xb3\xd0\xb0\xd0\xb5\xd0\xbc \xd1\x83\xd0\xb2\xd0\xb8\xd0\xb4\xd0\xb5\xd1\x82\xd1\x8c \xd1\x81\xd0\xbb\xd1\x83\xd1\x87\xd0\xb0\xd0\xb9 \xd1\x86\xd0\xb5\xd0\xbb\xd0\xb8\xd0\xba\xd0\xbe\xd0\xbc: \xd1\x81\xd1\x83\xd0\xbf\xd0\xb5\xd1\x80\xd0\xb2\xd0\xb8\xd0\xb7\xd0\xb8\xd1\x8f \xd0\xb4\xd0\xbb\xd1\x8f \xd0\xbf\xd1\x81\xd0\xb8\xd1\x85\xd0\xbe\xd0\xbb\xd0\xbe\xd0\xb3\xd0\xbe\xd0\xb2.</p>
        <p>\xd0\xa1\xd1\x82\xd0\xbe\xd0\xb8\xd0\xbc\xd0\xbe\xd1\x81\xd1\x82\xd1\x8c 5 000 \xe2\x82\xbd \xd0\xb7\xd0\xb0 60 \xd0\xbc\xd0\xb8\xd0\xbd\xd1\x83\xd1\x82.</p>
        </body></html>""",
    "/comp2": b"""<html><head><title>Competitor two</title></head><body>
        <p>\xd0\xa0\xd0\xb0\xd0\xb7\xd0\xb1\xd0\xbe\xd1\x80 \xd1\x81\xd0\xbb\xd0\xbe\xd0\xb6\xd0\xbd\xd1\x8b\xd1\x85 \xd1\x81\xd0\xbb\xd1\x83\xd1\x87\xd0\xb0\xd0\xb5\xd0\xb2 \xd0\xb8 \xd0\xbf\xd0\xbe\xd0\xb4\xd0\xb4\xd0\xb5\xd1\x80\xd0\xb6\xd0\xba\xd0\xb0 \xd0\xbf\xd1\x81\xd0\xb8\xd1\x85\xd0\xbe\xd0\xbb\xd0\xbe\xd0\xb3\xd0\xbe\xd0\xb2.</p>
        <p>\xd0\x9a\xd0\xb5\xd0\xb9\xd1\x81\xd1\x8b \xd0\xb8 \xd0\xbe\xd1\x82\xd0\xb7\xd1\x8b\xd0\xb2\xd1\x8b, \xd1\x81\xd1\x82\xd0\xb0\xd0\xb6 12 \xd0\xbb\xd0\xb5\xd1\x82.</p>
        </body></html>""",
    "/private": b"<html><body><p>Private page must not be fetched</p></body></html>",
}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = PAGES.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        content_type = "text/plain" if self.path == "/robots.txt" else "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


@pytest.fixture(scope="module")
def site_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(autouse=True)
def local_settings(monkeypatch):
    monkeypatch.setenv("WEBSITE_ALLOW_PRIVATE_HOSTS", "true")
    monkeypatch.setenv("LANDSCAPE_FETCH_DELAY_SECONDS", "0")
    monkeypatch.setenv("REPORT_LIVE_CHECK", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_robots_txt_is_respected(site_url):
    pages = crawl_site(f"{site_url}/client", 5)
    fetched = {page.url for page in pages}
    assert any("/client" in url for url in fetched)
    assert not any("/private" in url for url in fetched)


def test_competitor_requires_reason_and_limit():
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    from app.db import get_session_factory
    from app.models import Source

    factory = get_session_factory()
    with factory() as session:
        scan = create_scan(session, ScanCreate(title="Landscape limit fixture"))
        with pytest.raises(AppError) as exc:
            add_website_source(session, scan.id, WebsiteSourceCreate(
                url="https://example.com/", role="competitor",
            ))
        assert exc.value.status_code == 422
        for index in range(5):
            session.add(Source(
                scan_id=scan.id, name=f"competitor-{index}", type="website",
                external_id=f"c{index}.example",
                source_metadata={"role": "competitor", "selection_reason": "limit fixture"},
            ))
        session.commit()
        with pytest.raises(AppError) as exc:
            add_website_source(session, scan.id, WebsiteSourceCreate(
                url="https://example.com/", role="competitor",
                selection_reason="Прямой конкурент",
            ))
        assert exc.value.status_code == 422
        assert "5" in exc.value.message


def test_landscape_pipeline_and_report(site_url):
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    from app.agents import execute_job
    from app.db import get_session_factory
    from app.models import Scan
    from app.pipeline import schedule_after_success
    from app.queue import claim_next_job, complete_job
    from app.registers.models import (
        LandscapeEntry,
        ScorecardEntry,
        FindingV2,
        RecommendationFinding,
        RecommendationV2,
        SourceRegistryEntry,
    )
    from app.reports.build_v2 import build_report_v2
    from app.reports.model_v2 import validate_document

    factory = get_session_factory()
    with factory() as session:
        try:
            session.execute(text("SELECT 1"))
        except OperationalError:
            pytest.skip("PostgreSQL is not available")
        scan = create_scan(session, ScanCreate(title="Landscape fixture"))
        add_website_source(session, scan.id, WebsiteSourceCreate(
            url=f"{site_url}/client", max_pages=1,
        ))
        add_website_source(session, scan.id, WebsiteSourceCreate(
            url=f"{site_url}/comp1", max_pages=1, role="competitor",
            selection_reason="Прямой конкурент в той же категории",
        ))
        add_website_source(session, scan.id, WebsiteSourceCreate(
            url=f"{site_url}/comp2", max_pages=1, role="competitor",
            selection_reason="Альтернатива с другим фокусом",
        ))
        run_scan(session, scan.id)
        session.commit()
        for _ in range(4000):
            job = claim_next_job(session, "landscape-test")
            if job is None:
                break
            execute_job(session, job)
            assert complete_job(session, job)
            schedule_after_success(session, job)
            session.commit()
        scan = session.get(Scan, scan.id)
        assert scan.status == "succeeded"

        landscape = list(session.scalars(
            select(LandscapeEntry).where(LandscapeEntry.scan_id == scan.id)
            .order_by(LandscapeEntry.code)
        ))
        assert [entry.code for entry in landscape] == ["C01", "C02"]
        assert all(entry.inclusion_reason for entry in landscape)
        assert all(entry.source_code for entry in landscape)
        assert any(entry.price for entry in landscape)

        registry = list(session.scalars(
            select(SourceRegistryEntry).where(SourceRegistryEntry.scan_id == scan.id)
            .order_by(SourceRegistryEntry.code)
        ))
        roles = sorted(entry.role for entry in registry)
        assert roles == ["client", "competitor", "competitor"]

        scorecard = list(session.scalars(
            select(ScorecardEntry).where(ScorecardEntry.scan_id == scan.id)
        ))
        uniqueness = next(entry for entry in scorecard if "уникальность" in entry.criterion)
        assert uniqueness.score is not None
        assert any(str(code).startswith("C") for code in uniqueness.source_codes)

        findings = list(session.scalars(
            select(FindingV2).where(FindingV2.scan_id == scan.id)
        ))
        overlap = next(
            (item for item in findings if "пересекается" in item.statement), None
        )
        assert overlap is not None
        assert any(str(code).startswith("C") for code in overlap.basis_codes)
        assert overlap.kind == "weak_evidence"

        recommendations = list(session.scalars(
            select(RecommendationV2).where(RecommendationV2.scan_id == scan.id)
        ))
        assert any(item.decision_class in {"fix", "test"} for item in recommendations)
        links = list(session.scalars(select(RecommendationFinding)))
        assert links, "recommendations must be built after findings"

        document = build_report_v2(session, scan.id)
        assert validate_document(document) == []
        section = document.section("4")
        assert section.data_gaps == []
        table = next(block for block in section.blocks if block.type == "table")
        assert len(table.rows) == 2
        assert table.rows[0][0] == "C01"
