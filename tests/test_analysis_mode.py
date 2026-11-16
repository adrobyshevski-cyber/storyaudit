"""ANALYSIS_MODE: drafts, dedup and the provider cache must not change the report.

The invariant these tests protect: switching mode changes *who wrote the text*,
never which registers, findings, scores or references the report contains. The
report is checked by the same validators in every mode, so a broken draft must
fail loudly instead of producing a quietly empty report.
"""

import os
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.agents import execute_job
from app.config import Settings, get_settings
from app.db import get_session_factory
from app.models import Annotation, Proposition
from app.pipeline import schedule_after_success
from app.queue import claim_next_job, complete_job
from app.scans import add_source, create_scan, run_scan
from app.schemas import DocumentCreate, ScanCreate, SourceCreate


def _require_db():
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    from sqlalchemy import text

    with get_session_factory()() as session:
        try:
            session.execute(text("SELECT 1"))
        except OperationalError:
            pytest.skip("PostgreSQL is not available")


def _drain(session, worker_id: str = "analysis-mode-test") -> None:
    for _ in range(5000):
        job = claim_next_job(session, worker_id)
        if job is None:
            return
        execute_job(session, job)
        assert complete_job(session, job)
        schedule_after_success(session, job)
        session.commit()
    raise AssertionError("pipeline did not drain")


def _scan(session, text: str, *, model: str | None = None, title: str = "mode fixture"):
    scan = create_scan(session, ScanCreate(title=f"{title} {uuid.uuid4()}"))
    add_source(session, scan.id, SourceCreate(
        name=f"mode-{uuid.uuid4().hex[:8]}.example",
        documents=[DocumentCreate(title="Страница", url="https://mode.example/", text=text)],
    ))
    run_scan(session, scan.id)
    session.commit()
    _drain(session)
    session.refresh(scan)
    return scan


def _stub_provider(monkeypatch, *, fail: bool = False):
    """Replace the transport-facing ``generate`` and record every call."""
    from app.llm import LLMClient

    calls: list[tuple[str, dict]] = []

    def fake_generate(self, *, agent, schema, context, mock, validate=None):
        calls.append((agent, context))
        if fail:
            raise AssertionError(f"provider must not be called for {agent}")
        payload = schema.model_validate(mock).model_dump(mode="json")
        if validate is not None:
            validate(payload)
        return payload

    monkeypatch.setattr(LLMClient, "generate", fake_generate)
    return calls


def _configure(monkeypatch, **values):
    settings = get_settings()
    base = {"rate_limit_enabled": False, "llm_provider": "openai_compatible"}
    base.update(values)
    for key, value in base.items():
        monkeypatch.setattr(settings, key, value)
    return settings


# -- unit -------------------------------------------------------------------


def test_uses_llm_matrix():
    assert Settings(analysis_mode="llm", analysis_llm_agents="NARRATIVE").uses_llm("PRODUCT")
    hybrid = Settings(analysis_mode="hybrid", analysis_llm_agents="NARRATIVE,A1")
    assert hybrid.uses_llm("NARRATIVE") and hybrid.uses_llm("A1")
    assert not hybrid.uses_llm("PRODUCT") and not hybrid.uses_llm("CRITIC")
    deterministic = Settings(analysis_mode="deterministic", analysis_llm_agents="NARRATIVE")
    assert not deterministic.uses_llm("NARRATIVE")


# -- hybrid / deterministic -------------------------------------------------


def test_hybrid_scan_makes_no_provider_calls(monkeypatch):
    _require_db()
    _configure(monkeypatch, analysis_mode="hybrid", analysis_llm_agents="")
    calls = _stub_provider(monkeypatch, fail=True)

    with get_session_factory()() as session:
        scan = _scan(
            session,
            "Команда теряет обратную связь.\n\nАудит нарратива, 60 минут, 25 000 ₽.",
        )
        assert scan.status == "succeeded"
        annotations = list(session.scalars(
            select(Annotation).where(Annotation.scan_id == scan.id)
        ))
        report_status = session.execute(
            select(Annotation.payload).where(
                Annotation.scan_id == scan.id, Annotation.agent == "NARRATIVE"
            )
        ).scalar_one()["status"]

    assert calls == [], "hybrid mode with no LLM agents must not reach the provider"
    assert annotations
    # Honest provenance: templated payloads are not attributed to a model.
    templated = [item for item in annotations if item.agent in {"PRODUCT", "NARRATIVE"}]
    assert templated and all(item.model_version.startswith("code:") for item in templated)
    assert report_status == "draft"


def test_deterministic_mode_still_builds_all_registers(monkeypatch):
    _require_db()
    _configure(monkeypatch, analysis_mode="deterministic")
    _stub_provider(monkeypatch, fail=True)

    with get_session_factory()() as session:
        scan = _scan(
            session,
            "Команда теряет обратную связь.\n\nАудит нарратива, 60 минут, 25 000 ₽.\n\n"
            "Мы показываем, где обещание расходится с доказательствами.",
        )
        from app.reports.build_v2 import build_report_v2

        document = build_report_v2(session, scan.id)
        narrative = session.execute(
            select(Annotation.payload).where(
                Annotation.scan_id == scan.id, Annotation.agent == "NARRATIVE"
            )
        ).scalar_one()

    assert scan.status == "succeeded"
    assert len(document.sections) == 17
    assert narrative["status"] == "draft"
    # The narrative draft must still satisfy the deterministic guard.
    assert narrative["problems"] == []


# -- dedup and cache --------------------------------------------------------


def test_repeated_text_is_sent_to_the_provider_once(monkeypatch):
    _require_db()
    model = f"dedup-{uuid.uuid4().hex[:8]}"
    _configure(
        monkeypatch,
        analysis_mode="llm",
        llm_model=model,
        llm_model_fast=model,
        llm_model_smart=model,
    )
    calls = _stub_provider(monkeypatch)

    with get_session_factory()() as session:
        scan = _scan(
            session,
            "Одинаковое обещание клиенту.\n\n"
            "Одинаковое обещание клиенту.\n\n"
            "Уникальное обещание клиенту.",
        )
        a2_calls = [context for agent, context in calls if agent == "A2"]
        annotations = list(session.scalars(
            select(Annotation).where(
                Annotation.scan_id == scan.id,
                Annotation.agent == "A2",
                Annotation.is_current.is_(True),
            )
        ))
        duplicated = list(session.scalars(
            select(Proposition).where(Proposition.text == "Одинаковое обещание клиенту.")
        ))

    assert scan.status == "succeeded"
    # One chunk, but only two unique texts go to the model.
    assert len(a2_calls) == 1 and len(a2_calls[0]["propositions"]) == 2
    assert len(duplicated) >= 2
    # Every proposition still gets its own annotation.
    assert len(annotations) == 3


def test_second_scan_reuses_the_annotation_cache(monkeypatch):
    _require_db()
    model = f"cache-{uuid.uuid4().hex[:8]}"
    _configure(
        monkeypatch,
        analysis_mode="llm",
        llm_model=model,
        llm_model_fast=model,
        llm_model_smart=model,
    )
    calls = _stub_provider(monkeypatch)
    text = "Обещаем сопровождение после внедрения."

    with get_session_factory()() as session:
        first = _scan(session, text, title="cache first")
        first_a2 = [agent for agent, _ in calls].count("A2")
        calls.clear()
        second = _scan(session, text, title="cache second")
        second_a2 = [agent for agent, _ in calls].count("A2")
        annotations = list(session.scalars(
            select(Annotation).where(
                Annotation.scan_id == second.id,
                Annotation.agent == "A2",
                Annotation.is_current.is_(True),
            )
        ))
        statuses = (first.status, second.status)
        annotated_model = annotations[0].model_version if annotations else None

    assert statuses == ("succeeded", "succeeded")
    assert first_a2 >= 1, "the first scan must populate the cache"
    assert second_a2 == 0, "the second scan must be served from the cache"
    assert len(annotations) == 1
    assert annotated_model == model


def test_reanalysis_calls_the_provider_even_in_hybrid(monkeypatch):
    """An analyst who reopens an issue must get a fresh answer, not the draft."""
    _require_db()
    model = f"reanalysis-{uuid.uuid4().hex[:8]}"
    _configure(
        monkeypatch,
        analysis_mode="hybrid",
        analysis_llm_agents="",
        llm_model=model,
        llm_model_fast=model,
        llm_model_smart=model,
    )
    calls = _stub_provider(monkeypatch)

    with get_session_factory()() as session:
        scan = _scan(session, "Обещаем сопровождение после внедрения.", title="reanalysis")
        assert [agent for agent, _ in calls] == [], "the initial scan is fully deterministic"
        proposition = session.scalar(
            select(Proposition)
            .join(Annotation, Annotation.object_id == Proposition.id)
            .where(Annotation.scan_id == scan.id, Annotation.agent == "A2")
            .limit(1)
        )
        from app.pipeline import create_reanalysis
        from app.schemas import ReanalysisCreate

        create_reanalysis(session, scan.id, ReanalysisCreate(
            agent="A3",
            object_type="proposition",
            object_id=proposition.id,
            issue_id=uuid.uuid4(),
            client_request_id=uuid.uuid4(),
        ))
        _drain(session)
        session.refresh(scan)

    assert scan.status == "succeeded"
    # Only the re-analyzed stage goes to the provider; A5/PROFILE/A6/A7/A8* are a
    # deterministic recomputation and must not be re-billed.
    assert [agent for agent, _ in calls] == ["A3"]


def test_evidence_output_is_capped_by_the_statement_budget(monkeypatch):
    """A model answer cannot blow past REPORT_MAX_STATEMENTS.

    The llm-mode A/B returned 32 claims for a single page while the configured
    budget was 25 and the per-source draft limit was 8. Findings then cited claims
    that the budget never allowed.
    """
    _require_db()
    model = f"cap-{uuid.uuid4().hex[:8]}"
    settings = _configure(
        monkeypatch,
        analysis_mode="hybrid",
        analysis_llm_agents="EVIDENCE",
        llm_model=model,
        llm_model_fast=model,
        llm_model_smart=model,
    )
    from app.llm import LLMClient
    from app.registers.models import EvidenceRecord

    def fake_generate(self, *, agent, schema, context, mock, validate=None):
        if agent == "EVIDENCE":
            payload = {
                "claims": [
                    {
                        "statement": f"Утверждение номер {index}",
                        "relevance": "medium",
                        "confidence": "medium",
                        "source_codes": ["S01"],
                    }
                    for index in range(20)
                ]
            }
        else:
            payload = mock
        result = schema.model_validate(payload).model_dump(mode="json")
        if validate is not None:
            validate(result)
        return result

    monkeypatch.setattr(LLMClient, "generate", fake_generate)

    with get_session_factory()() as session:
        scan = _scan(session, "Обещаем сопровождение после внедрения.", title="evidence cap")
        records = session.scalar(
            select(func.count(EvidenceRecord.id)).where(EvidenceRecord.scan_id == scan.id)
        )

    assert scan.status == "succeeded"
    expected = min(settings.report_max_statements_per_source, settings.report_max_statements)
    assert records == expected, "the model answer must be cut to the per-source budget"


def test_deterministic_findings_and_plan_fit_the_provider_schema():
    """A draft must satisfy the same max_length as a provider answer.

    An 11-finding site produced 13 recommendations in the deterministic plan; the
    draft failed PlanResult validation and the whole scan failed (seen in the
    capacity run). Caps keep the most severe items and stay inside the schema.
    """
    from app.analysis.findings import MAX_FINDINGS, FindingDraft, cap_by_priority
    from app.analysis.recommendations import (
        MAX_RECOMMENDATIONS,
        KpiDraft,
        RecommendationDraft,
        cap_recommendations,
    )
    from app.schemas import FindingsResult, PlanResult

    findings = [
        FindingDraft(kind="weak_evidence", statement=f"W{index}", basis_codes=["E01"], priority="P3")
        for index in range(15)
    ]
    findings.append(FindingDraft(kind="missing_link", statement="critical", basis_codes=["P01"], priority="P1"))
    capped = cap_by_priority(findings)
    assert len(capped) == MAX_FINDINGS
    assert any(item.priority == "P1" for item in capped)
    FindingsResult.model_validate({"findings": [item.as_payload() for item in capped]})

    kpi = KpiDraft("d", "x", "unknown", "p", "s", "o", "t", "l")
    recommendations = [
        RecommendationDraft(
            change=f"change {index}", rationale="because", finding_codes=[],
            kpi=kpi, priority="P3",
        )
        for index in range(14)
    ]
    recommendations.append(
        RecommendationDraft(
            change="blocker", rationale="because", finding_codes=[], kpi=kpi, priority="blocker",
        )
    )
    plan = cap_recommendations(recommendations)
    assert len(plan) == MAX_RECOMMENDATIONS
    assert any(item.priority == "blocker" for item in plan)
    PlanResult.model_validate({
        "recommendations": [
            {
                "change": item.change,
                "rationale": item.rationale,
                "expected_effect": item.expected_effect,
                "impact": item.impact,
                "evidence_confidence": item.evidence_confidence,
                "urgency": item.urgency,
                "effort": item.effort,
                "priority": item.priority,
                "decision_class": item.decision_class,
                "owner": item.owner,
                "finding_codes": item.finding_codes,
                "kpi": {
                    "name": item.kpi.name, "definition": item.kpi.definition,
                    "baseline": item.kpi.baseline, "period": item.kpi.period,
                    "data_source": item.kpi.data_source, "owner": item.kpi.owner,
                    "target": item.kpi.target, "limitations": item.kpi.limitations,
                },
            }
            for item in plan
        ],
        "roadmap": [],
        "gate": {"decision": "conditional_ready", "rationale": "x", "criteria": [],
                 "blockers": [], "required_validations": []},
        "hypotheses": [],
    })
