import http.server
import threading

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.errors import AppError
from app.scans import add_website_source, create_scan, run_scan
from app.schemas import ScanCreate, WebsiteSourceCreate
from app.website import crawl_site, normalize_site_url, parse_html

PAGES = {
    "/": b"""<html><head><title>Acme</title><style>.x{color:red}</style></head><body>
        <nav>Home About Contacts</nav>
        <h1>\xd0\x9c\xd1\x8b \xd0\xb4\xd0\xb5\xd0\xbb\xd0\xb0\xd0\xb5\xd0\xbc \xd0\xb0\xd1\x83\xd0\xb4\xd0\xb8\xd1\x82</h1>
        <p>Small teams miss feedback from customers.</p>
        <p>StoryAudit shows narrative gaps.</p>
        <a href="/about">About</a>
        <a href="https://other.example/x">External</a>
        <script>var secret = 'noise';</script>
        </body></html>""",
    "/about": b"""<html><head><title>About</title></head><body>
        <h2>Team</h2><p>We analyse how a site tells its story.</p></body></html>""",
}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = PAGES.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


@pytest.fixture(scope="module")
def site_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


@pytest.fixture(autouse=True)
def allow_local_sites(monkeypatch):
    monkeypatch.setenv("WEBSITE_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_normalize_site_url_adds_scheme_and_strips_query():
    assert normalize_site_url("example.com") == "https://example.com/"
    assert normalize_site_url(" http://example.com/a?b=1#c ") == "http://example.com/a"


@pytest.mark.parametrize("raw", ["", "ftp://example.com", "https://", "https://user:pass@example.com"])
def test_normalize_site_url_rejects_unsupported(raw):
    with pytest.raises(AppError):
        normalize_site_url(raw)


def test_parse_html_drops_noise_and_keeps_paragraphs():
    title, paragraphs, links = parse_html(PAGES["/"].decode())
    assert title == "Acme"
    assert "Small teams miss feedback from customers." in paragraphs
    assert not any("secret" in item or "color:red" in item or "Home About" in item for item in paragraphs)
    assert "/about" in links


def test_private_hosts_blocked_by_default(monkeypatch):
    monkeypatch.setenv("WEBSITE_ALLOW_PRIVATE_HOSTS", "false")
    get_settings.cache_clear()
    with pytest.raises(AppError):
        crawl_site("http://127.0.0.1:9/")


def test_crawl_follows_only_same_origin(site_url):
    pages = crawl_site(site_url, max_pages=5)
    urls = {page.url for page in pages}
    assert len(pages) == 2, urls
    assert all("other.example" not in url for url in urls)
    assert any("We analyse" in page.text for page in pages)


def test_crawl_reports_unreachable_site(monkeypatch):
    with pytest.raises(AppError) as exc:
        crawl_site("http://127.0.0.1:1/", max_pages=1)
    assert exc.value.status_code == 422


def test_add_website_source_builds_corpus(site_url):
    import os

    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    from app.db import get_session_factory
    from app.models import Block, Document

    factory = get_session_factory()
    with factory() as session:
        try:
            session.execute(text("SELECT 1"))
        except OperationalError:
            pytest.skip("PostgreSQL is not available")
        scan = create_scan(session, ScanCreate(title="Website ingestion"))
        source = add_website_source(session, scan.id, WebsiteSourceCreate(url=site_url, max_pages=3))
        assert source.type == "website"
        assert source.source_metadata["pages"] == 2
        documents = session.query(Document).filter(Document.source_id == source.id).all()
        assert len(documents) == 2
        assert all(doc.url for doc in documents)
        blocks = session.query(Block).filter(Block.document_id.in_([doc.id for doc in documents])).all()
        assert len(blocks) == sum(len(doc.text.split("\n\n")) for doc in documents)
        assert run_scan(session, scan.id).status == "queued"
