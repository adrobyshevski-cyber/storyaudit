import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sqlalchemy import select

from app.main import app

client = TestClient(app)
ORIGIN = {"origin": "http://testserver"}


@pytest.fixture
def db_session():
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from app.db import get_session_factory

    session = get_session_factory()()
    try:
        session.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip("PostgreSQL is not available")
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _submit(website: str = "https://example.com/") -> str:
    response = client.post(
        "/audit",
        data={"website": website, "consent": "on"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response.headers["location"].rsplit("/", 1)[-1]


def _latest_request(session):
    from sqlalchemy import select

    from app.models import AuditRequest

    request = session.scalar(select(AuditRequest).order_by(AuditRequest.created_at.desc()).limit(1))
    assert request is not None
    return request


def _retire_public_jobs(session):
    """Cancel jobs of public-test scans.

    Other integration tests drain every pending job in the database; leaving an
    INGEST or A0 job behind would make them crawl a real URL or process a scan
    that belongs to this module.
    """
    from sqlalchemy import select, update

    from app.models import AuditRequest, Job

    scan_ids = select(AuditRequest.scan_id).where(AuditRequest.scan_id.is_not(None))
    session.execute(
        update(Job)
        .where(Job.scan_id.in_(scan_ids), Job.status.in_(["pending", "running"]))
        .values(status="cancelled")
    )
    session.commit()


def test_public_pages_do_not_require_auth():
    for path in ("/", "/sample-report", "/privacy", "/robots.txt", "/sitemap.xml"):
        assert client.get(path).status_code == 200, path
    landing = client.get("/")
    assert "audit-form" in landing.text
    assert "NIA" in landing.text
    # The sample opens in a same-origin iframe, so DENY would break the landing.
    assert landing.headers["x-frame-options"] == "SAMEORIGIN"


def test_internal_ui_is_still_protected():
    assert client.get("/ui/scans").status_code == 401
    assert client.get("/scans").status_code == 401


def test_origin_check_is_scheme_agnostic_but_blocks_foreign_hosts(db_session):
    # Render's proxy may leave request.base_url on http:// while the browser
    # sends https://; the host comparison must still accept the form.
    response = client.post(
        "/audit",
        data={"website": "https://example.com/", "consent": "on"},
        headers={"origin": "https://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    _retire_public_jobs(db_session)

    foreign = client.post(
        "/audit",
        data={"website": "https://example.com/", "consent": "on"},
        headers={"origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert foreign.status_code == 403


def test_unknown_token_shows_friendly_404():
    response = client.get("/audit/definitely-not-a-token")
    assert response.status_code == 404
    assert "недействительна" in response.text
    assert response.headers["x-robots-tag"] == "noindex, nofollow"


def test_form_rejects_missing_consent_and_bad_input():
    no_consent = client.post("/audit", data={"website": "https://example.com"}, headers=ORIGIN)
    assert no_consent.status_code == 422
    assert "согласие" in no_consent.text.lower()

    bad_url = client.post(
        "/audit", data={"website": "javascript:alert(1)", "consent": "on"}, headers=ORIGIN
    )
    assert bad_url.status_code == 422

    bad_email = client.post(
        "/audit",
        data={"website": "https://example.com", "email": "not-an-email", "consent": "on"},
        headers=ORIGIN,
    )
    assert bad_email.status_code == 422

    honeypot = client.post(
        "/audit",
        data={"website": "https://example.com", "consent": "on", "company_website": "spam"},
        headers=ORIGIN,
    )
    assert honeypot.status_code == 400


def test_form_creates_request_scan_and_ingest_job(db_session):
    from sqlalchemy import select

    from app.models import AuditRequest, Job, Scan

    token = _submit("example.com")
    request = db_session.scalar(
        select(AuditRequest).order_by(AuditRequest.created_at.desc()).limit(1)
    )
    assert request.website_url == "https://example.com/"
    assert request.ip_hash and "testclient" not in request.ip_hash
    assert request.status == "ingesting"

    scan = db_session.get(Scan, request.scan_id)
    assert scan.status == "draft"
    assert scan.audit_phase == "ingesting"
    assert scan.analysis_generation == 1

    ingest = db_session.scalar(select(Job).where(Job.scan_id == scan.id, Job.agent == "INGEST"))
    assert ingest is not None and ingest.job_type == "ingest"

    page = client.get(f"/audit/{token}")
    assert page.status_code == 200
    assert "готовится" in page.text
    state = client.get(f"/audit/{token}/state").json()
    assert state["state"] == "ingesting"
    assert state["report_url"] is None
    _retire_public_jobs(db_session)


def test_ingest_job_crawls_and_queues_the_pipeline(db_session, monkeypatch):
    from sqlalchemy import select

    from app.agents import execute_job
    from app.models import Job, Scan
    from app.website import Page

    token = _submit()
    request = _latest_request(db_session)
    scan = db_session.get(Scan, request.scan_id)
    job = db_session.scalar(select(Job).where(Job.scan_id == scan.id, Job.agent == "INGEST"))

    monkeypatch.setattr(
        "app.scans.crawl_site",
        lambda *args, **kwargs: [
            Page(
                url="https://example.com/",
                title="Public example",
                paragraphs=[
                    "Мы делаем аудит нарратива для команд.",
                    "Аудит занимает 60 минут и стоит 25 000 рублей.",
                ],
            )
        ],
    )
    execute_job(db_session, job)
    db_session.commit()
    # A lost lease can retry the job after run_scan committed; ingestion must be
    # idempotent and must not fail on the now non-draft scan.
    execute_job(db_session, job)
    db_session.commit()

    from sqlalchemy import func

    from app.models import Source

    sources = db_session.scalar(
        select(func.count(Source.id)).where(Source.scan_id == scan.id)
    )
    assert sources == 1

    scan = db_session.get(Scan, scan.id)
    assert scan.status == "queued"
    assert scan.audit_phase == "analyzing"
    a0 = db_session.scalar(select(Job).where(Job.scan_id == scan.id, Job.agent == "A0"))
    assert a0 is not None and a0.job_type == "initial"

    # The public page keeps following the scan without exposing internal jobs.
    state = client.get(f"/audit/{token}/state").json()
    assert state["state"] == "analyzing"
    _retire_public_jobs(db_session)


def test_internal_scans_page_lists_public_review_queue(db_session):
    from app.config import get_settings
    from app.models import Scan

    _submit("https://example.com/")
    request = _latest_request(db_session)
    scan = db_session.get(Scan, request.scan_id)
    scan.status = "succeeded"
    scan.audit_phase = "registers_ready"
    db_session.commit()

    response = client.get(
        "/ui/scans",
        headers={"Authorization": f"Bearer {get_settings().app_access_token}"},
    )
    assert response.status_code == 200
    assert "Публичные заявки на ревью" in response.text
    assert request.website_url in response.text
    _retire_public_jobs(db_session)


def test_report_stays_closed_until_the_gate_is_approved(db_session):
    from app.models import Scan
    from app.registers.models import StageGate

    token = _submit("https://example.com/")
    request = _latest_request(db_session)
    scan = db_session.get(Scan, request.scan_id)
    scan.status = "succeeded"
    scan.audit_phase = "registers_ready"
    db_session.commit()

    page = client.get(f"/audit/{token}")
    assert page.status_code == 200
    assert "проверяет аналитик" in page.text
    closed = client.get(f"/audit/{token}/report", follow_redirects=False)
    assert closed.status_code == 303
    assert "notready" in closed.headers["location"]

    db_session.add(StageGate(
        scan_id=scan.id,
        decision="ready",
        rationale="Проверено аналитиком",
        approved_by="analyst",
        criteria=[],
        blockers=[],
        required_validations=[],
    ))
    scan.audit_phase = "client_ready"
    db_session.commit()

    page = client.get(f"/audit/{token}")
    assert "Ваш отчёт готов" in page.text
    assert f"/audit/{token}/report" in page.text
    _retire_public_jobs(db_session)


def test_public_report_renders_after_gate(db_session, monkeypatch):
    from app.models import Scan
    from app.registers.models import StageGate

    token = _submit("https://example.com/")
    request = _latest_request(db_session)
    scan = db_session.get(Scan, request.scan_id)
    scan.status = "succeeded"
    scan.audit_phase = "client_ready"
    db_session.add(StageGate(
        scan_id=scan.id, decision="ready", rationale="ok", approved_by="analyst",
        criteria=[], blockers=[], required_validations=[],
    ))
    db_session.commit()

    document = SimpleNamespace(
        meta=SimpleNamespace(report_status="client_ready", content_hash="hash"),
    )
    monkeypatch.setattr("app.public.build_report_v2", lambda *a, **k: document)
    monkeypatch.setattr(
        "app.public.render_html_v2",
        lambda doc, download_url=None, inline_css=False, brief=False: f"<html>{download_url}</html>",
    )
    response = client.get(f"/audit/{token}/report")
    assert response.status_code == 200
    assert f"/audit/{token}/report.pdf" in response.text
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    _retire_public_jobs(db_session)


def test_publish_endpoint_requires_the_internal_token(db_session):
    from app.models import Scan

    token = _submit("https://example.com/")
    record = _latest_request(db_session)
    scan = db_session.get(Scan, record.scan_id)
    scan.status = "succeeded"
    scan.audit_phase = "registers_ready"
    db_session.commit()

    denied = client.post(f"/audit/{token}/publish")
    assert denied.status_code == 403

    wrong = client.post(f"/audit/{token}/publish", headers={"x-internal-token": "not-the-token"})
    assert wrong.status_code == 403
    _retire_public_jobs(db_session)


def test_publish_rejects_audits_outside_review(db_session, monkeypatch):
    from app.models import Scan

    token = _submit("https://example.com/")
    record = _latest_request(db_session)
    scan = db_session.get(Scan, record.scan_id)
    scan.status = "running"
    db_session.commit()

    monkeypatch.setattr(
        "app.public.get_settings",
        lambda: SimpleNamespace(public_internal_token="test-internal", public_base_url="http://testserver"),
    )
    response = client.post(f"/audit/{token}/publish", headers={"x-internal-token": "test-internal"})
    assert response.status_code == 409
    assert response.json()["state"] != "ready"
    _retire_public_jobs(db_session)


def test_publish_approves_reviewed_audit_for_the_site(db_session, monkeypatch):
    from app.models import Scan
    from app.schemas import StageGateDecision

    token = _submit("https://example.com/")
    record = _latest_request(db_session)
    scan = db_session.get(Scan, record.scan_id)
    scan.status = "succeeded"
    scan.audit_phase = "registers_ready"
    db_session.commit()

    monkeypatch.setattr(
        "app.public.get_settings",
        lambda: SimpleNamespace(public_internal_token="test-internal", public_base_url="http://testserver"),
    )

    calls = []

    def fake_gate(session, scan_id, data: StageGateDecision):
        # пишем через сессию приложения: у TestClient своя, изменения db_session не видны
        from app.models import Scan as ScanModel
        from app.registers.models import StageGate

        calls.append((scan_id, data))
        session.add(StageGate(
            scan_id=scan_id, decision="ready", rationale=data.rationale,
            approved_by=data.actor, criteria=[], blockers=[], required_validations=[],
        ))
        session.get(ScanModel, scan_id).audit_phase = "client_ready"
        session.commit()
        return {}

    monkeypatch.setattr("app.public.decide_stage_gate", fake_gate)

    response = client.post(f"/audit/{token}/publish", headers={"x-internal-token": "test-internal"})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "ready"
    assert len(calls) == 1
    scan_id, decision = calls[0]
    assert decision.decision == "ready"
    assert decision.actor == "auto:nia-site"
    assert decision.blockers == []

    again = client.post(f"/audit/{token}/publish", headers={"x-internal-token": "test-internal"})
    assert again.status_code == 200
    assert again.json()["state"] == "ready"
    assert len(calls) == 1, "повторный вызов не должен подтверждать гейт заново"
    _retire_public_jobs(db_session)


def test_landing_offers_two_competitor_fields():
    landing = client.get("/").text
    assert 'name="competitor_1"' in landing
    assert 'name="competitor_2"' in landing


def test_request_normalises_and_caps_competitors(db_session):
    from app.audit_requests import create_audit_request

    request, _ = create_audit_request(
        db_session,
        website_url="https://example.com/",
        email=None,
        company=None,
        ip="10.0.0.1",
        user_agent="pytest",
        competitors=[
            "https://example.org/",
            "https://example.com/",          # same host as the client: dropped
            "javascript:alert(1)",           # invalid: dropped, must not fail the form
            "https://example.net/",
            "https://iana.org/",             # over the public cap of 2
        ],
    )
    from app.models import Scan

    scan = db_session.get(Scan, request.scan_id)
    assert scan.config_snapshot["origin"] == "public"
    assert scan.config_snapshot["landscape_max_competitors"] == 2
    from app.models import Job

    ingest = db_session.scalar(
        select(Job).where(Job.scan_id == scan.id, Job.agent == "INGEST")
    )
    assert ingest.input_snapshot["competitors"] == [
        "https://example.org/", "https://example.net/"
    ]
    _retire_public_jobs(db_session)


def test_ingest_adds_public_competitors_once(db_session, monkeypatch):
    from sqlalchemy import func

    from app.agents import execute_job
    from app.models import Job, Scan, Source
    from app.website import Page

    token = _submit()
    assert token
    request = _latest_request(db_session)
    scan = db_session.get(Scan, request.scan_id)
    scan.config_snapshot = {
        **scan.config_snapshot,
        "origin": "public",
        "landscape_max_competitors": 2,
    }
    job = db_session.scalar(select(Job).where(Job.scan_id == scan.id, Job.agent == "INGEST"))
    job.input_snapshot = {
        **job.input_snapshot,
        "competitors": ["https://example.org/", "https://example.net/"],
    }
    db_session.commit()

    crawled: list[str] = []

    def fake_crawl(url, max_pages, **kwargs):
        crawled.append(url)
        return [Page(
            url=url,
            title="site",
            paragraphs=[
                "Мы делаем аудит нарратива для команд.",
                "Аудит занимает 60 минут и стоит 25 000 рублей.",
            ],
        )]

    monkeypatch.setattr("app.scans.crawl_site", fake_crawl)
    execute_job(db_session, job)
    db_session.commit()
    execute_job(db_session, job)  # lost-lease retry: must not duplicate sources
    db_session.commit()

    sources = list(db_session.scalars(select(Source).where(Source.scan_id == scan.id)))
    assert len(sources) == 3
    competitors = [item for item in sources if (item.source_metadata or {}).get("role") == "competitor"]
    assert len(competitors) == 2
    assert all(
        item.source_metadata["selection_reason"].startswith("Указана заявителем")
        for item in competitors
    )
    # One page per alternative, the client site keeps its own budget.
    assert len(crawled) == 3
    assert db_session.scalar(
        select(func.count(Source.id)).where(Source.scan_id == scan.id)
    ) == 3


def test_public_scan_competitor_cap_is_two(db_session, monkeypatch):
    from app.audit_requests import create_audit_request
    from app.errors import AppError
    from app.scans import add_website_source
    from app.schemas import WebsiteSourceCreate
    from app.website import Page

    request, _ = create_audit_request(
        db_session,
        website_url="https://example.com/",
        email=None,
        company=None,
        ip="10.0.0.2",
        user_agent="pytest",
    )
    monkeypatch.setattr(
        "app.scans.crawl_site",
        lambda url, max_pages, **kwargs: [Page(
            url=url, title="site",
            paragraphs=["Мы делаем аудит нарратива для команд.", "Стоимость 25 000 рублей."],
        )],
    )
    for index in (1, 2):
        add_website_source(db_session, request.scan_id, WebsiteSourceCreate(
            url=("https://example.org/" if index == 1 else "https://example.net/"), max_pages=1,
            role="competitor", selection_reason=f"альтернатива {index}",
        ))
    with pytest.raises(AppError) as excinfo:
        add_website_source(db_session, request.scan_id, WebsiteSourceCreate(
            url="https://iana.org/", max_pages=1,
            role="competitor", selection_reason="третья альтернатива",
        ))
    assert excinfo.value.status_code == 422
    assert "2" in excinfo.value.message
    _retire_public_jobs(db_session)


def _api_client(monkeypatch, *, token="capability-token", trusted=None):
    """DB-free TestClient for the JSON/trusted-caller surface.

    The endpoint's database work is stubbed out, so these tests stay fast and run
    without PostgreSQL (the full flow is covered by the db_session tests).
    """
    from app.db import get_db

    created: dict = {}

    def fake_create(session, **kwargs):
        created.update(kwargs)
        from types import SimpleNamespace
        return SimpleNamespace(id="req-id", scan_id="scan-id"), token

    app.dependency_overrides[get_db] = lambda: None
    monkeypatch.setattr("app.public.create_audit_request", fake_create)
    monkeypatch.setattr("app.public.guard_public_request", lambda ip_hash, *, trusted=False: created.update(
        guard_trusted=trusted
    ))
    return created


@pytest.fixture(autouse=True)
def _reset_overrides():
    yield
    app.dependency_overrides.clear()


def test_submit_returns_json_for_api_clients(monkeypatch):
    created = _api_client(monkeypatch)
    response = client.post(
        "/audit",
        data={"website": "https://example.com/", "consent": "on"},
        headers={**ORIGIN, "accept": "application/json"},
        follow_redirects=False,
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["token"] == "capability-token"
    assert payload["state_url"] == "/audit/capability-token/state"
    assert payload["report_url"] == "/audit/capability-token/report"
    assert payload["pdf_url"] == "/audit/capability-token/report.pdf"
    assert "location" not in {key.lower() for key in response.headers}
    assert created["website_url"] == "https://example.com/"


def test_html_clients_still_get_a_redirect(monkeypatch):
    _api_client(monkeypatch)
    response = client.post(
        "/audit",
        data={"website": "https://example.com/", "consent": "on"},
        headers={**ORIGIN, "accept": "text/html"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/audit/capability-token"


def test_internal_token_marks_the_call_trusted(monkeypatch):
    from app.config import get_settings

    created = _api_client(monkeypatch)
    monkeypatch.setenv("PUBLIC_INTERNAL_TOKEN", "trusted-secret")
    get_settings.cache_clear()
    try:
        client.post(
            "/audit",
            data={"website": "https://example.com/", "consent": "on"},
            headers={**ORIGIN, "accept": "application/json", "x-internal-token": "trusted-secret"},
            follow_redirects=False,
        )
        assert created["guard_trusted"] is True

        client.post(
            "/audit",
            data={"website": "https://example.org/", "consent": "on"},
            headers={**ORIGIN, "accept": "application/json", "x-internal-token": "wrong"},
            follow_redirects=False,
        )
        assert created["guard_trusted"] is False
    finally:
        get_settings.cache_clear()


def test_internal_token_is_ignored_when_not_configured(monkeypatch):
    from app.config import get_settings

    created = _api_client(monkeypatch)
    monkeypatch.delenv("PUBLIC_INTERNAL_TOKEN", raising=False)
    get_settings.cache_clear()
    try:
        client.post(
            "/audit",
            data={"website": "https://example.com/", "consent": "on"},
            headers={**ORIGIN, "accept": "application/json", "x-internal-token": ""},
            follow_redirects=False,
        )
        assert created["guard_trusted"] is False
    finally:
        get_settings.cache_clear()


def test_report_inline_flag_asks_for_embedded_css(monkeypatch):
    from types import SimpleNamespace

    from app.db import get_db

    captured: dict = {}
    app.dependency_overrides[get_db] = lambda: None
    monkeypatch.setattr("app.public.get_request_by_token", lambda session, token: SimpleNamespace(scan_id="scan-id"))
    monkeypatch.setattr("app.public.public_state", lambda session, record: "ready")
    monkeypatch.setattr("app.public.guard_report_build", lambda scan_id: None)
    monkeypatch.setattr(
        "app.public.build_report_v2",
        lambda session, scan_id, brief=False: captured.update(brief=brief)
        or SimpleNamespace(meta=SimpleNamespace(report_status="client_ready")),
    )
    monkeypatch.setattr(
        "app.public.render_html_v2",
        lambda doc, download_url=None, inline_css=False, brief=False: captured.update(inline_css=inline_css)
        or "<html>ok</html>",
    )

    assert client.get("/audit/capability-token/report").status_code == 200
    assert captured["inline_css"] is False

    captured.clear()
    response = client.get("/audit/capability-token/report?inline=1")
    assert response.status_code == 200
    assert captured["inline_css"] is True


def test_trusted_caller_passes_the_real_client_ip(monkeypatch):
    """Behind an edge X-Forwarded-For is the worker's own address, so the real
    client travels in x-client-ip - but only for a trusted caller."""
    from app.config import get_settings

    created = _api_client(monkeypatch)
    monkeypatch.setenv("PUBLIC_INTERNAL_TOKEN", "trusted-secret")
    get_settings.cache_clear()
    try:
        client.post(
            "/audit",
            data={"website": "https://example.com/", "consent": "on"},
            headers={
                **ORIGIN,
                "accept": "application/json",
                "x-internal-token": "trusted-secret",
                "x-forwarded-for": "10.0.0.1",
                "x-client-ip": "203.0.113.7",
            },
            follow_redirects=False,
        )
        assert created["ip"] == "203.0.113.7", created

        client.post(
            "/audit",
            data={"website": "https://example.org/", "consent": "on"},
            headers={
                **ORIGIN,
                "accept": "application/json",
                "x-internal-token": "wrong",
                "x-forwarded-for": "10.0.0.1",
                "x-client-ip": "203.0.113.7",
            },
            follow_redirects=False,
        )
        assert created["ip"] == "10.0.0.1", created
    finally:
        get_settings.cache_clear()


def test_guard_public_request_trusted_skips_only_the_global_counter(monkeypatch):
    from app.ratelimit import guard_public_request

    calls: list[str] = []
    monkeypatch.setattr("app.ratelimit.hit", lambda bucket, limit, window: calls.append(bucket))

    guard_public_request("abc", trusted=True)
    assert calls == ["public_request_ip:abc"], calls

    calls.clear()
    guard_public_request("abc")
    assert calls == ["public_request_ip:abc", "public_request_total"], calls


def test_report_brief_flag_asks_for_the_short_variant(monkeypatch):
    from types import SimpleNamespace

    from app.db import get_db

    captured: dict = {}
    app.dependency_overrides[get_db] = lambda: None
    monkeypatch.setattr("app.public.get_request_by_token", lambda session, token: SimpleNamespace(scan_id="scan-id"))
    monkeypatch.setattr("app.public.public_state", lambda session, record: "ready")
    monkeypatch.setattr("app.public.guard_report_build", lambda scan_id: None)
    monkeypatch.setattr(
        "app.public.build_report_v2",
        lambda session, scan_id, brief=False: captured.update(brief=brief)
        or SimpleNamespace(meta=SimpleNamespace(report_status="client_ready")),
    )
    monkeypatch.setattr(
        "app.public.render_html_v2",
        lambda doc, download_url=None, inline_css=False, brief=False: "<html>ok</html>",
    )

    assert client.get("/audit/capability-token/report").status_code == 200
    assert captured["brief"] is False

    captured.clear()
    assert client.get("/audit/capability-token/report?brief=1&inline=1").status_code == 200
    assert captured["brief"] is True
