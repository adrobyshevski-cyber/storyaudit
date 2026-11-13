import os

import pytest

from app.reports.render import render_html


@pytest.fixture(scope="module")
def completed_scan():
    """Build one succeeded scan through the real pipeline (PostgreSQL required)."""
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from app.agents import execute_job
    from app.db import get_session_factory
    from app.pipeline import schedule_after_success
    from app.queue import claim_next_job, complete_job
    from app.scans import add_source, create_scan, run_scan
    from app.schemas import DocumentCreate, ScanCreate, SourceCreate

    factory = get_session_factory()
    with factory() as session:
        try:
            session.execute(text("SELECT 1"))
        except OperationalError:
            pytest.skip("PostgreSQL is not available")
        scan = create_scan(session, ScanCreate(title="Report fixture"))
        add_source(session, scan.id, SourceCreate(
            name="example.com",
            documents=[DocumentCreate(
                title="Home",
                url="https://example.com/",
                text=(
                    "Мы создали CRM, которая помогает командам продаж.\n\n"
                    "Клиенты теряют заявки между этапами и не видят статус сделок.\n\n"
                    "FlowDesk показывает воронку и напоминает о следующем шаге."
                ),
            )],
        ))
        run_scan(session, scan.id)
        session.commit()
        for _ in range(2000):
            job = claim_next_job(session, "report-test")
            if job is None:
                break
            execute_job(session, job)
            assert complete_job(session, job)
            schedule_after_success(session, job)
            session.commit()
        yield scan.id


def test_free_report_document(completed_scan):
    from app.db import get_session_factory
    from app.reports.build import build_report

    with get_session_factory()() as session:
        document = build_report(session, completed_scan, "free")

    assert document.meta.report_type == "free"
    assert document.meta.scan_id == str(completed_scan)
    assert document.meta.report_id.startswith("free-")
    assert document.summary.indicators, "index and metrics must be reported"
    assert document.meta.data_completeness == 0.55
    assert any(row.code == "storyaudit_index" for row in document.summary.indicators)
    assert len(document.observations) <= 3
    sections = {gap.section for gap in document.data_gaps}
    assert "Narrative Capital Index" in sections
    assert "Межканальная матрица" in sections
    assert document.appendix["report_version"] == "report_v1"
    assert document.appendix["content_hash"]


def test_full_report_document(completed_scan):
    from app.db import get_session_factory
    from app.reports.build import build_report

    with get_session_factory()() as session:
        document = build_report(session, completed_scan, "full")

    assert document.meta.report_type == "full"
    assert document.corpus.rows, "corpus must list the fetched source"
    assert document.corpus.total_blocks > 0
    assert document.quality
    assert document.action_plan
    assert document.measurement
    assert document.contradictions == []
    ratios = {row.code: row for row in document.quantitative}
    assert ratios["hero_ratio"].available is False
    assert ratios["hero_ratio"].display == "н/д"


def test_report_rejects_unknown_type(completed_scan):
    from app.db import get_session_factory
    from app.errors import AppError
    from app.reports.build import build_report

    with get_session_factory()() as session:
        with pytest.raises(AppError) as exc:
            build_report(session, completed_scan, "bogus")
    assert exc.value.status_code == 422


def test_report_requires_finished_scan():
    import uuid

    from app.db import get_session_factory
    from app.errors import AppError
    from app.reports.build import build_report

    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    created = {"id": None}
    from app.scans import create_scan
    from app.schemas import ScanCreate

    with get_session_factory()() as session:
        scan = create_scan(session, ScanCreate(title="Draft scan"))
        created["id"] = scan.id
        with pytest.raises(AppError) as exc:
            build_report(session, scan.id, "free")
    assert exc.value.status_code == 409
    assert created["id"] != uuid.UUID(int=0)


def test_html_renders_all_sections(completed_scan):
    from app.db import get_session_factory
    from app.reports.build import build_report

    with get_session_factory()() as session:
        full = render_html(build_report(session, completed_scan, "full"))
        free = render_html(build_report(session, completed_scan, "free"))

    for needle in ("Executive Summary", "8. Предлагаемая новая нарративная архитектура",
                   "9. Rewriting Engine", "Ограничения данных", "Приложение"):
        assert needle in full, needle
    for needle in ("нарратив за 30 секунд", "Карта нарративных ролей", "Что делать дальше"):
        assert needle in free, needle
    assert "Ограничения данных" in free
    assert "Приложение" not in free


def test_pdf_rendering(completed_scan):
    from app.db import get_session_factory
    from app.reports.build import build_report
    from app.reports.render import render_pdf

    with get_session_factory()() as session:
        document = build_report(session, completed_scan, "free")
    pdf = render_pdf(document)
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 20_000
