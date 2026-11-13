import os

import pytest


@pytest.fixture(scope="module")
def completed_scan():
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
        scan = create_scan(session, ScanCreate(title="Registers fixture"))
        add_source(session, scan.id, SourceCreate(
            name="flow.example",
            documents=[DocumentCreate(
                title="FlowDesk — супервизия для психологов",
                url="https://flow.example/supervision",
                text=(
                    "Индивидуальная супервизия, 60 минут, 18 000 ₽.\n\n"
                    "Мы помогаем психологам увидеть случай целиком и найти следующий шаг.\n\n"
                    "Стоимость профессиональной сессии — 15 000 ₽ за 50 минут.\n\n"
                    "Мы не гарантируем решение клинического случая за одну встречу."
                ),
            )],
        ))
        run_scan(session, scan.id)
        session.commit()
        for _ in range(2000):
            job = claim_next_job(session, "registers-test")
            if job is None:
                break
            execute_job(session, job)
            assert complete_job(session, job)
            schedule_after_success(session, job)
            session.commit()
        yield scan.id


def test_pipeline_builds_registers_and_finishes_scan(completed_scan):
    from sqlalchemy import select

    from app.db import get_session_factory
    from app.models import Scan
    from app.registers.models import (
        EvidenceRecord,
        LanguageCode,
        Product,
        SourceRegistryEntry,
        Statement,
    )

    with get_session_factory()() as session:
        scan = session.get(Scan, completed_scan)
        products = list(session.scalars(select(Product).where(Product.scan_id == completed_scan)))
        evidence = list(session.scalars(select(EvidenceRecord).where(EvidenceRecord.scan_id == completed_scan)))
        statements = list(session.scalars(select(Statement).where(Statement.scan_id == completed_scan)))
        language = list(session.scalars(select(LanguageCode).where(LanguageCode.scan_id == completed_scan)))
        registry = list(session.scalars(select(SourceRegistryEntry).where(SourceRegistryEntry.scan_id == completed_scan)))
        status = scan.status
        phase = scan.audit_phase
        completed_at = scan.completed_at

    assert status == "succeeded"
    assert phase == "narrative_draft"
    assert completed_at is not None
    assert products and products[0].code == "P01"
    assert "18 000 ₽" in (products[0].price or "")
    assert evidence and evidence[0].code == "E01"
    assert statements and statements[0].kind == "F"
    assert language and language[0].code == "L01"
    assert registry and registry[0].code == "S01"


def test_evidence_never_overclaims(completed_scan):
    from sqlalchemy import select

    from app.db import get_session_factory
    from app.registers.models import EvidenceRecord

    with get_session_factory()() as session:
        evidence = list(session.scalars(select(EvidenceRecord).where(EvidenceRecord.scan_id == completed_scan)))

    assert evidence
    for record in evidence:
        assert record.verification == "publication_only"
        assert record.provenance in {"primary", "review"}
        assert record.allowed_conclusion.startswith("Бренд публично заявляет")
        assert "Независимая проверка утверждения не выполнялась" in record.limitations
        assert record.source_codes


def test_language_map_does_not_invent_lived_language(completed_scan):
    from sqlalchemy import select

    from app.db import get_session_factory
    from app.registers.models import LanguageCode

    with get_session_factory()() as session:
        codes = list(session.scalars(select(LanguageCode).where(LanguageCode.scan_id == completed_scan)))

    assert codes
    for code in codes:
        assert code.brand
        assert code.lived is None
        assert code.match_status == "no_data"
        assert code.is_illustration is False


def test_report_sections_use_registers(completed_scan):
    from app.db import get_session_factory
    from app.reports.build_v2 import build_report_v2
    from app.reports.model_v2 import validate_document

    with get_session_factory()() as session:
        document = build_report_v2(session, completed_scan)

    assert validate_document(document) == []
    for section_id in ("3", "5", "8", "11", "12"):
        assert not document.section(section_id).data_gaps, section_id
    # The competitor landscape arrives in phase E.
    assert document.section("4").data_gaps


def test_live_check_updates_registry(monkeypatch):
    from datetime import datetime

    from app.analysis import verify as verify_module
    from app.config import get_settings
    from app.registers.models import SourceRegistryEntry

    settings = get_settings()
    monkeypatch.setattr(settings, "report_live_check", True)
    monkeypatch.setattr(
        verify_module,
        "http_probe",
        lambda url, timeout: verify_module.LiveCheck(status_code=200, final_url=url),
    )
    entry = SourceRegistryEntry(
        scan_id=None, code="S99", url="https://example.com/", role="client",
        limitations=[], entry_metadata={},
    )
    result = verify_module.verify_source(session=None, entry=entry)
    assert result.status_code == 200
    assert isinstance(entry.live_checked_at, datetime)
    assert entry.availability == "full"


def test_live_check_failure_is_a_limitation_not_a_verdict(monkeypatch):
    from app.analysis import verify as verify_module
    from app.config import get_settings
    from app.registers.models import SourceRegistryEntry

    settings = get_settings()
    monkeypatch.setattr(settings, "report_live_check", True)
    monkeypatch.setattr(
        verify_module,
        "http_probe",
        lambda url, timeout: verify_module.LiveCheck(status_code=503),
    )
    entry = SourceRegistryEntry(
        scan_id=None, code="S98", url="https://example.com/", role="client",
        limitations=[], entry_metadata={},
    )
    verify_module.verify_source(session=None, entry=entry)
    assert entry.availability == "partial"
    assert any("HTTP 503" in str(item) for item in entry.limitations)
