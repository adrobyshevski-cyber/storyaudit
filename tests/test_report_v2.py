import os
import uuid

import pytest


@pytest.fixture(scope="module")
def completed_scan():
    """One succeeded scan built through the real pipeline (PostgreSQL required)."""
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
        scan = create_scan(session, ScanCreate(title="Report v2 fixture"))
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
            job = claim_next_job(session, "report-v2-test")
            if job is None:
                break
            execute_job(session, job)
            assert complete_job(session, job)
            schedule_after_success(session, job)
            session.commit()
        yield scan.id


def test_report_v2_has_full_structure(completed_scan):
    from app.db import get_session_factory
    from app.reports.build_v2 import build_report_v2
    from app.reports.model_v2 import (
        METHODOLOGY_VERSION,
        REPORT_VERSION,
        validate_document,
    )

    with get_session_factory()() as session:
        document = build_report_v2(session, completed_scan)

    assert document.meta.report_version == REPORT_VERSION == "report_v2"
    assert document.meta.methodology_version == METHODOLOGY_VERSION == "socode_v1.1"
    assert document.meta.scan_id == str(completed_scan)
    assert document.meta.audit_mode == "desk"
    assert document.meta.report_status == "internal_draft"
    assert document.meta.disclaimer
    assert [section.id for section in document.sections] == [str(i) for i in range(1, 18)]
    assert [appendix.id for appendix in document.appendices] == ["A", "B", "C", "D"]
    assert validate_document(document) == []
    # Phase B registers are filled from the corpus, not from invented values.
    products = next(block for block in document.section("3").blocks if block.type == "table")
    assert products.rows and products.rows[0][0].startswith("P")
    evidence = next(block for block in document.section("8").blocks if block.type == "table")
    assert evidence.rows and evidence.rows[0][0].startswith("E")
    language = next(block for block in document.section("11").blocks if block.type == "table")
    assert language.rows and language.rows[0][0].startswith("L")
    # The competitor landscape is the only empty section until phase E.
    assert any(gap.section == "4" for section in document.sections for gap in section.data_gaps)


def test_report_v2_source_register_and_stable_hash(completed_scan):
    from sqlalchemy import select

    from app.db import get_session_factory
    from app.registers.models import SourceRegistryEntry
    from app.reports.build_v2 import build_report_v2

    factory = get_session_factory()
    with factory() as session:
        first = build_report_v2(session, completed_scan)
        second = build_report_v2(session, completed_scan)
        entries = list(session.scalars(
            select(SourceRegistryEntry).where(SourceRegistryEntry.scan_id == completed_scan)
        ))
        session.commit()

    assert first.meta.content_hash == second.meta.content_hash
    assert first.meta.report_id == second.meta.report_id
    assert entries and entries[0].code == "S01"
    assert entries[0].source_id is not None
    assert entries[0].currency == "current"
    register_rows = first.section("2").blocks[1].rows
    assert register_rows and register_rows[0][0] == "S01"


def test_report_v2_desk_mode_has_no_scores(completed_scan):
    from app.db import get_session_factory
    from app.reports.build_v2 import build_report_v2

    with get_session_factory()() as session:
        document = build_report_v2(session, completed_scan)

    # Desk mode scores only what the corpus supports; the rest stays N/A.
    assert document.coverage.scored > 0
    assert document.coverage.na > 0
    scorecard = document.section("12")
    table = next(block for block in scorecard.blocks if block.type == "table")
    assert table.rows
    experience = [row for row in table.rows if "фактический опыт" in row[1]]
    assert experience and experience[0][2].startswith("N/A")
    text = " ".join(block.text or "" for block in scorecard.blocks)
    assert "N/A" in text
    assert "индекс" in text.lower()
    # No aggregate and no pseudo-index anywhere in the payload.
    payload = str(document.model_dump(mode="json"))
    for forbidden in ("storyaudit_index", "Индекс нарративного капитала", "score_total"):
        assert forbidden not in payload


def test_report_v2_html_format(completed_scan):
    from app.db import get_session_factory
    from app.reports.build_v2 import build_report_v2
    from app.reports.render_v2 import render_html

    with get_session_factory()() as session:
        html = render_html(build_report_v2(session, completed_scan))

    for needle in (
        "НАРРАТИВНЫЙ АУДИТ",
        "Методологическая оговорка",
        "S-register",
        "Current Business",
        "Proposition Map",
        "Findings Register",
        "Narrative Scorecard",
        "Stage 02 Readiness Gate",
        "Ограничения данных",
        "Приложение A",
        "Приложение D",
    ):
        assert needle in html, needle
    for legacy in ("Rewriting Engine", "Narrative Capital", "Межканальная матрица"):
        assert legacy not in html


def test_report_v2_requires_succeeded_scan():
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    from app.db import get_session_factory
    from app.errors import AppError
    from app.reports.build_v2 import build_report_v2
    from app.scans import create_scan
    from app.schemas import ScanCreate

    with get_session_factory()() as session:
        scan = create_scan(session, ScanCreate(title="Draft for report v2"))
        with pytest.raises(AppError) as exc:
            build_report_v2(session, scan.id)
    assert exc.value.status_code == 409


def test_report_v2_persists_snapshot(completed_scan):
    from sqlalchemy import select

    from app.db import get_session_factory
    from app.registers.models import ReportSnapshot
    from app.reports.build_v2 import build_report_v2

    factory = get_session_factory()
    with factory() as session:
        document = build_report_v2(session, completed_scan, persist=True)
        snapshots = list(session.scalars(
            select(ReportSnapshot).where(ReportSnapshot.scan_id == completed_scan)
        ))
    assert snapshots
    fresh = [snapshot for snapshot in snapshots if snapshot.status == "fresh"]
    assert len(fresh) == 1
    assert fresh[0].content_hash == document.meta.content_hash
    assert fresh[0].document["meta"]["brand"]


def test_code_allocation_is_stable_and_formatted():
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    from app.db import get_session_factory
    from app.registers.codes import allocate_code
    from app.scans import create_scan
    from app.schemas import ScanCreate

    with get_session_factory()() as session:
        scan = create_scan(session, ScanCreate(title="Codes fixture"))
        first = allocate_code(session, scan.id, "E")
        second = allocate_code(session, scan.id, "E")
        kpi = allocate_code(session, scan.id, "KPI")
        session.commit()

    assert (first, second, kpi) == ("E01", "E02", "KPI01")


def test_toc_page_numbers_come_from_actual_section_pages(completed_scan, monkeypatch):
    from app.db import get_session_factory
    from app.reports.build_v2 import build_report_v2
    from app.reports.render_v2 import _find_section_pages, render_html

    with get_session_factory()() as session:
        document = build_report_v2(session, completed_scan)

    class FakePage:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

    toc_text = " ".join(f"{item.id} {item.title}" for item in document.sections)
    pages = [
        FakePage("Cover"),
        FakePage(toc_text),
        FakePage(f"{document.section('1').id} {document.section('1').title}"),
        FakePage(f"{document.section('2').id} {document.section('2').title}"),
        FakePage(f"{document.appendices[0].id} {document.appendices[0].title}"),
    ]
    monkeypatch.setattr("app.reports.render_v2.PdfReader", lambda stream: type(
        "FakeReader", (), {"pages": pages}
    )())
    pages_by_anchor = _find_section_pages(b"%PDF fake", document)
    assert pages_by_anchor[document.section("1").anchor] == 3
    assert pages_by_anchor[document.section("2").anchor] == 4
    assert pages_by_anchor[document.appendices[0].anchor] == 5
    assert 2 not in pages_by_anchor.values()

    html = render_html(document, page_numbers=pages_by_anchor)
    assert 'class="toc-page">3</span>' in html
    assert f'href="#{document.section("1").anchor}"' in html


def test_report_v2_rejects_unknown_objects():
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    from app.db import get_session_factory
    from app.errors import AppError
    from app.reports.build_v2 import build_report_v2

    with get_session_factory()() as session:
        with pytest.raises(AppError) as exc:
            build_report_v2(session, uuid.uuid4())
    assert exc.value.status_code == 404
