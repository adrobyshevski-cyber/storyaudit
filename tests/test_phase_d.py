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
        scan = create_scan(session, ScanCreate(title="Phase D fixture"))
        add_source(session, scan.id, SourceCreate(
            name="audit.example",
            documents=[DocumentCreate(
                title="Аудит коммуникации",
                url="https://audit.example/narrative",
                text=(
                    "Коммуникация теряет смысл между стратегией и текстом на сайте.\n\n"
                    "Аудит нарратива, 60 минут, 25 000 ₽.\n\n"
                    "Мы показываем, где обещание расходится с доказательствами."
                ),
            )],
        ))
        run_scan(session, scan.id)
        session.commit()
        for _ in range(3000):
            job = claim_next_job(session, "phase-d-test")
            if job is None:
                break
            execute_job(session, job)
            assert complete_job(session, job)
            schedule_after_success(session, job)
            session.commit()
        yield scan.id


def test_scorecard_scores_only_supported_criteria(completed_scan):
    from sqlalchemy import select

    from app.db import get_session_factory
    from app.registers.models import ScorecardEntry

    with get_session_factory()() as session:
        entries = list(session.scalars(
            select(ScorecardEntry).where(ScorecardEntry.scan_id == completed_scan)
            .order_by(ScorecardEntry.ordinal)
        ))

    assert len(entries) >= 12
    for entry in entries:
        assert entry.dimension in {"clarity", "consistency", "credibility", "resonance"}
        if entry.score is None:
            assert entry.na_reason
            assert entry.confidence is None
        else:
            assert 0 <= float(entry.score) <= 10
            assert entry.confidence in {"high", "medium", "low"}
        assert entry.rationale
    assert all(
        entry.confidence is not None for entry in entries if entry.score is not None
    )
    experience = [entry for entry in entries if "фактический опыт" in entry.criterion]
    assert experience and experience[0].score is None


def test_recommendations_kpi_and_gate(completed_scan):
    from sqlalchemy import select

    from app.db import get_session_factory
    from app.registers.models import (
        AuditHypothesis,
        FindingV2,
        Kpi,
        RecommendationFinding,
        RecommendationV2,
        RoadmapPhase,
        StageGate,
    )

    with get_session_factory()() as session:
        findings = list(session.scalars(select(FindingV2).where(FindingV2.scan_id == completed_scan)))
        recommendations = list(session.scalars(
            select(RecommendationV2).where(RecommendationV2.scan_id == completed_scan)
        ))
        links = list(session.scalars(select(RecommendationFinding)))
        kpis = list(session.scalars(select(Kpi).where(Kpi.scan_id == completed_scan)))
        phases = list(session.scalars(select(RoadmapPhase).where(RoadmapPhase.scan_id == completed_scan)))
        gate = session.scalar(select(StageGate).where(StageGate.scan_id == completed_scan))
        hypotheses = list(session.scalars(
            select(AuditHypothesis).where(AuditHypothesis.scan_id == completed_scan)
        ))

    assert findings and recommendations
    assert len(recommendations) >= len(findings)
    assert links, "every recommendation must keep its finding link"
    for recommendation in recommendations:
        assert recommendation.expected_effect_status == "H"
        assert recommendation.priority in {"P1", "P2", "P3", "blocker"}
        assert recommendation.decision_class in {"keep", "fix", "test", "stop"}
        assert recommendation.kpi_codes
        assert recommendation.impact in {"high", "medium", "low"}
    assert kpis and all(kpi.baseline == "unknown" for kpi in kpis)
    assert len(phases) == 5
    assert gate is not None
    assert gate.decision in {"ready", "conditional_ready", "not_ready"}
    assert gate.criteria
    assert gate.required_validations
    assert hypotheses


def test_narrative_is_stored_and_references_registers(completed_scan):
    from sqlalchemy import select

    from app.db import get_session_factory
    from app.registers.models import ReportNarrative

    with get_session_factory()() as session:
        rows = list(session.scalars(
            select(ReportNarrative).where(ReportNarrative.scan_id == completed_scan)
        ))

    assert {row.section_id for row in rows} == {"1", "17"}
    for row in rows:
        assert row.status in {"validated", "fallback"}
        for paragraph in row.paragraphs:
            if paragraph.get("status") in {"F", "I", "H", "R"}:
                assert paragraph.get("refs")


def test_report_sections_d_are_filled(completed_scan):
    from app.db import get_session_factory
    from app.reports.build_v2 import build_report_v2
    from app.reports.model_v2 import validate_document
    from app.reports.render_v2 import render_html

    with get_session_factory()() as session:
        document = build_report_v2(session, completed_scan)

    assert validate_document(document) == []
    for section_id in ("1", "12", "13", "14", "15", "16", "17"):
        assert not document.section(section_id).data_gaps, section_id
    assert document.section("4").data_gaps
    assert document.coverage.scored > 0
    assert document.coverage.na > 0
    # No aggregate score anywhere.
    payload = str(document.model_dump(mode="json"))
    for forbidden in ("storyaudit_index", "score_total", "Narrative Capital"):
        assert forbidden not in payload

    html = render_html(document)
    for needle in ("Narrative Scorecard", "Strategic Recommendations", "Stage 02 Readiness Gate"):
        assert needle in html, needle


def test_narrative_validator_rejects_unsafe_text():
    from app.analysis.narrative import validate_narrative

    refs = {"P01", "M01"}
    numbers = {"18000"}
    good = {"1": [{"text": "Ключевая находка: границы не описаны.", "status": "I", "refs": ["M01"]}],
            "17": [{"text": "Изменений не требуется.", "status": "U", "refs": []}]}
    assert validate_narrative(good, refs=refs, numbers=numbers) == []

    bad = {
        "1": [
            {"text": "Это гарантирует результат.", "status": "I", "refs": ["M01"]},
            {"text": "Цена 99 000 ₽ обоснована.", "status": "I", "refs": ["P01"]},
            {"text": "См. X99.", "status": "I", "refs": ["X99"]},
            {"text": "Без ссылок.", "status": "I", "refs": []},
        ],
        "17": [],
    }
    problems = validate_narrative(bad, refs=refs, numbers=numbers)
    joined = " | ".join(problems)
    assert "обещание гарантированного результата" in joined
    assert "99000" in joined
    assert "unknown references" in joined
    assert "without references" in joined
    assert "missing narrative sections" in joined
