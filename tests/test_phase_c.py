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
        scan = create_scan(session, ScanCreate(title="Phase C fixture"))
        add_source(session, scan.id, SourceCreate(
            name="practice.example",
            documents=[DocumentCreate(
                title="Супервизия для психологов",
                url="https://practice.example/supervision",
                text=(
                    "Психологи часто застревают: терапия идёт, а изменений нет.\n\n"
                    "Индивидуальная супервизия, 60 минут, 18 000 ₽.\n\n"
                    "Мы помогаем увидеть случай целиком и найти следующий шаг."
                ),
            )],
        ))
        run_scan(session, scan.id)
        session.commit()
        for _ in range(3000):
            job = claim_next_job(session, "phase-c-test")
            if job is None:
                break
            execute_job(session, job)
            assert complete_job(session, job)
            schedule_after_success(session, job)
            session.commit()
        yield scan.id


def test_actant_map_has_six_roles_and_three_object_levels(completed_scan):
    from sqlalchemy import select

    from app.db import get_session_factory
    from app.registers.models import ActantMap, ActantRole

    with get_session_factory()() as session:
        maps = list(session.scalars(select(ActantMap).where(ActantMap.scan_id == completed_scan)))
        roles = list(session.scalars(
            select(ActantRole).join(ActantMap, ActantRole.map_id == ActantMap.id)
            .where(ActantMap.scan_id == completed_scan)
        ))

    assert maps
    names = {role.role for role in roles}
    assert names == {"subject", "object", "sender", "receiver", "helper", "opponent"}
    levels = {role.object_level for role in roles if role.role == "object"}
    assert levels == {"functional", "emotional", "transformational"}
    for role in roles:
        assert role.status in {"F", "I", "H", "U"}
        if role.status == "F":
            assert role.source_codes


def test_layers_keep_experience_and_perception_unobserved(completed_scan):
    from sqlalchemy import select

    from app.db import get_session_factory
    from app.registers.models import LayerClaim

    with get_session_factory()() as session:
        claims = list(session.scalars(select(LayerClaim).where(LayerClaim.scan_id == completed_scan)))

    assert claims
    for claim in claims:
        assert claim.declared.get("text")
        assert claim.experienced.get("status") == "unavailable"
        assert claim.perceived.get("status") == "unavailable"
        assert claim.match_status == "no_data"
        assert claim.unknown_note


def test_journey_is_modelled_not_observed(completed_scan):
    from sqlalchemy import select

    from app.db import get_session_factory
    from app.registers.models import JourneyStep

    with get_session_factory()() as session:
        steps = list(session.scalars(
            select(JourneyStep).where(JourneyStep.scan_id == completed_scan)
            .order_by(JourneyStep.ordinal)
        ))

    assert [step.step for step in steps] == [
        "trigger", "interpretation", "search", "comparison",
        "trust", "decision", "experience", "referral",
    ]
    assert all(step.is_observed is False for step in steps)
    by_step = {step.step: step for step in steps}
    assert by_step["experience"].public_state == "нет данных"
    assert by_step["referral"].public_state == "нет данных"
    assert by_step["trigger"].client_question


def test_consistency_matrix_is_complete_and_honest(completed_scan):
    from sqlalchemy import select

    from app.db import get_session_factory
    from app.registers.models import ConsistencyCell

    with get_session_factory()() as session:
        cells = list(session.scalars(
            select(ConsistencyCell).where(ConsistencyCell.scan_id == completed_scan)
        ))

    assert len(cells) == 16
    for cell in cells:
        assert cell.status in {"confirmed", "partial", "conflict", "no_data"}
    experience = [cell for cell in cells if cell.dimension == "experience"]
    assert experience and all(cell.status == "no_data" for cell in experience)
    assert all(
        cell.counterexample for cell in cells if cell.status == "conflict"
    )


def test_findings_are_classified_and_reviewed(completed_scan):
    from sqlalchemy import select

    from app.db import get_session_factory
    from app.registers.models import FindingV2

    with get_session_factory()() as session:
        findings = list(session.scalars(select(FindingV2).where(FindingV2.scan_id == completed_scan)))

    assert findings
    for finding in findings:
        prefix = {"gap": "G", "missing_link": "M", "weak_evidence": "W"}[finding.kind]
        assert finding.code.startswith(prefix)
        assert finding.basis_codes
        assert finding.alternative_explanation
        if finding.kind == "gap":
            assert len(set(finding.basis_codes)) >= 2
        if finding.kind == "weak_evidence":
            assert finding.needs_review is True


def test_empty_findings_are_a_result_not_a_gap():
    from app.reports.build_v2 import _section_findings

    section = _section_findings([], True)
    assert section.data_gaps == []
    assert any(block.title == "Находок не обнаружено" for block in section.blocks)


def test_report_sections_c_are_filled(completed_scan):
    from app.db import get_session_factory
    from app.reports.build_v2 import build_report_v2
    from app.reports.model_v2 import validate_document
    from app.reports.render_v2 import render_html

    with get_session_factory()() as session:
        document = build_report_v2(session, completed_scan)

    assert validate_document(document) == []
    for section_id in ("5", "6", "7", "9", "10", "12", "13", "15", "16"):
        assert not document.section(section_id).data_gaps, section_id
    # The competitor landscape is the only remaining empty section.
    assert document.section("4").data_gaps

    html = render_html(document)
    for needle in ("Greimas Actant Reconstruction", "Narrative Consistency Matrix",
                   "Findings Register", "Граница режима Desk"):
        assert needle in html, needle
