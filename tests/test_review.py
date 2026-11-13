import os
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
AUTH = {"Authorization": "Bearer local-test-token"}


@pytest.fixture
def review_scan():
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from app.db import get_session_factory
    from app.registers.codes import allocate_code
    from app.registers.models import (
        AuditHypothesis,
        EvidenceRecord,
        FindingV2,
        JourneyStep,
        LanguageCode,
        Product,
        RecommendationFinding,
        RecommendationV2,
        ScorecardEntry,
        StageGate,
        Statement,
    )
    from app.registers.sources import ensure_source_registry
    from app.reports.build_v2 import build_report_v2
    from app.scans import add_source, create_scan
    from app.schemas import DocumentCreate, ScanCreate, SourceCreate

    with get_session_factory()() as session:
        try:
            session.execute(text("SELECT 1"))
        except OperationalError:
            pytest.skip("PostgreSQL is not available")
        scan = create_scan(session, ScanCreate(title="Phase F review fixture"))
        add_source(session, scan.id, SourceCreate(
            name="review.example",
            documents=[DocumentCreate(
                title="Offer", url="https://review.example/", text="Аудит нарратива для команд.",
            )],
        ))
        registry = ensure_source_registry(session, scan)
        source = registry[0]
        product = Product(
            scan_id=scan.id, code=allocate_code(session, scan.id, "P"),
            name="Нарративный аудит", promise="Ясное обещание", source_codes=[source.code],
        )
        session.add(product)
        session.flush()
        statement = Statement(
            scan_id=scan.id, code=allocate_code(session, scan.id, "F"), kind="F",
            text="На сайте опубликовано обещание", source_codes=[source.code],
        )
        session.add(statement)
        session.flush()
        evidence = EvidenceRecord(
            scan_id=scan.id, code=allocate_code(session, scan.id, "E"),
            statement_id=statement.id, claim="Опубликовано обещание", source_codes=[source.code],
            allowed_conclusion="Бренд публично заявляет: обещание опубликовано",
        )
        finding = FindingV2(
            scan_id=scan.id, code=allocate_code(session, scan.id, "W"), kind="weak_evidence",
            statement="Обещание не подкреплено проверяемым примером", basis_codes=[product.code],
            alternative_explanation="Пример не был опубликован в исследованном корпусе",
        )
        recommendation = RecommendationV2(
            scan_id=scan.id, code=allocate_code(session, scan.id, "R"),
            change="Добавить проверяемый пример", rationale=finding.statement,
        )
        hypothesis = AuditHypothesis(
            scan_id=scan.id, code=allocate_code(session, scan.id, "H"),
            statement="Пример повысит ясность", how_to_verify="Провести интервью",
        )
        scorecard = ScorecardEntry(
            scan_id=scan.id, ordinal=1, dimension="credibility", criterion="Доказательность",
            score=Decimal("4"), confidence="medium", rationale="Есть публикация, но нет примера",
            source_codes=[evidence.code],
        )
        experience_scorecard = ScorecardEntry(
            scan_id=scan.id, ordinal=2, dimension="consistency",
            criterion="Коммуникация ↔ фактический опыт", score=None,
            na_reason="Desk Research: опыт не наблюдался", rationale="Нет интервью",
        )
        gate = StageGate(
            scan_id=scan.id, decision="conditional_ready", rationale="Нужна проверка брифа",
            criteria=[{"criterion": "Цели и границы клиента", "result": "Not established",
                       "required": "Заполнить Client Brief v2.0"}],
            blockers=[], required_validations=["Client Brief v2.0"],
        )
        session.add_all([
            evidence, finding, recommendation, hypothesis, scorecard, experience_scorecard, gate,
        ])
        session.flush()
        session.add(RecommendationFinding(recommendation_id=recommendation.id, finding_id=finding.id))
        session.add_all([
            JourneyStep(
                scan_id=scan.id, ordinal=1, step="trigger", client_question="Что произошло?",
                public_state="Ситуация описана", source_codes=[source.code],
            ),
            LanguageCode(
                scan_id=scan.id, code=allocate_code(session, scan.id, "L"), topic="Аудит",
                brand="Нарративный аудит", match_status="no_data", source_codes=[source.code],
            ),
        ])
        scan.status = "succeeded"
        scan.audit_phase = "narrative_draft"
        session.commit()
        build_report_v2(session, scan.id, persist=True)
        yield {
            "scan": scan.id,
            "source": source.id,
            "product": product.id,
            "statement": statement.id,
            "evidence": evidence.id,
            "finding": finding.id,
            "recommendation": recommendation.id,
            "scorecard": scorecard.id,
            "experience_scorecard": experience_scorecard.id,
            "gate": gate.id,
        }


def _url(ids, suffix=""):
    return f"/scans/{ids['scan']}{suffix}"


def test_register_patch_records_history_and_invalidates_snapshot(review_scan):
    from app.db import get_session_factory
    from app.models import Document, Scan, Source
    from app.registers.models import ReportSnapshot, ReviewEvent
    from sqlalchemy import select

    with get_session_factory()() as session:
        original_source = session.scalar(select(Source.source_metadata).join(
            Document, Document.source_id == Source.id
        ).where(Source.scan_id == review_scan["scan"]))
        original_text = session.scalar(select(Document.text).where(
            Document.source_id == Source.id, Source.scan_id == review_scan["scan"]
        ))

    response = client.patch(
        _url(review_scan, f"/registers/products/{review_scan['product']}"),
        headers=AUTH,
        json={"actor": "Ирина", "note": "Уточнено по брифу",
              "changes": {"promise": "Проверяемое обещание"}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["promise"] == "Проверяемое обещание"

    bad = client.patch(
        _url(review_scan, f"/registers/products/{review_scan['product']}"),
        headers=AUTH, json={"actor": "Ирина", "changes": {"code": "P99"}},
    )
    assert bad.status_code == 422

    with get_session_factory()() as session:
        scan = session.get(Scan, review_scan["scan"])
        events = list(session.scalars(select(ReviewEvent).where(ReviewEvent.scan_id == scan.id)))
        snapshots = list(session.scalars(select(ReportSnapshot).where(ReportSnapshot.scan_id == scan.id)))
        unchanged_source = session.scalar(select(Source.source_metadata).where(
            Source.scan_id == review_scan["scan"]
        ))
        unchanged_text = session.scalar(select(Document.text).join(
            Source, Document.source_id == Source.id
        ).where(Source.scan_id == review_scan["scan"]))

    assert scan.audit_phase == "analyst_review"
    assert len(events) == 1
    assert events[0].actor == "Ирина"
    assert events[0].before["promise"] == "Ясное обещание"
    assert events[0].after["promise"] == "Проверяемое обещание"
    assert {snapshot.status for snapshot in snapshots} == {"needs_rebuild"}
    assert unchanged_source == original_source
    assert unchanged_text == original_text


def test_disputed_source_marks_dependent_registers_for_review(review_scan):
    response = client.patch(
        _url(review_scan, f"/registers/sources/{review_scan['source']}"),
        headers=AUTH,
        json={"actor": "Аналитик", "changes": {"currency": "disputed"}},
    )
    assert response.status_code == 200, response.text

    from sqlalchemy import select

    from app.db import get_session_factory
    from app.registers.models import (
        EvidenceRecord,
        FindingV2,
        RecommendationV2,
        ScorecardEntry,
        Statement,
    )

    with get_session_factory()() as session:
        evidence = session.get(EvidenceRecord, review_scan["evidence"])
        finding = session.get(FindingV2, review_scan["finding"])
        recommendation = session.get(RecommendationV2, review_scan["recommendation"])
        statement = session.scalar(select(Statement).where(
            Statement.scan_id == review_scan["scan"]
        ))
        scorecard = session.get(ScorecardEntry, review_scan["scorecard"])

    assert statement.needs_review and statement.review_status == "needs_review"
    assert evidence.needs_review
    assert finding.needs_review and finding.review_status == "needs_review"
    assert recommendation.review_status == "needs_review"
    assert scorecard.entry_metadata["needs_review"] is True


def test_client_brief_open_questions_and_report_appendix(review_scan):
    response = client.put(
        _url(review_scan, "/client-brief"), headers=AUTH,
        json={"actor": "Аналитик", "brief": {
            "business_goal": "Согласовать предложение", "primary_audience": "Владельцы студий",
        }},
    )
    assert response.status_code == 200, response.text
    created = client.post(
        _url(review_scan, "/open-questions"), headers=AUTH,
        json={"actor": "Аналитик", "question": "Кто утверждает обещание?", "target": "client"},
    )
    assert created.status_code == 201, created.text
    question_id = created.json()["id"]
    answered = client.patch(
        _url(review_scan, f"/open-questions/{question_id}"), headers=AUTH,
        json={"actor": "Аналитик", "status": "answered", "note": "Утверждает основатель"},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["status"] == "answered"

    from app.db import get_session_factory
    from app.registers.models import StageGate
    from app.reports.build_v2 import build_report_v2

    with get_session_factory()() as session:
        gate = session.get(StageGate, review_scan["gate"])
        criteria = gate.criteria
        validations = gate.required_validations
        document = build_report_v2(session, review_scan["scan"])

    assert criteria[0]["result"] == "Ready"
    assert "Client Brief v2.0" not in validations
    appendix = document.section("B") if "B" in [item.id for item in document.sections] else next(
        item for item in document.appendices if item.id == "B"
    )
    assert any("Кто утверждает обещание?" in str(row) for block in appendix.blocks for row in block.rows)
    assert any("answered" in str(row) for block in appendix.blocks for row in block.rows)


def test_stage_gate_decisions_and_reopen_after_edit(review_scan):
    blocked = client.post(
        _url(review_scan, "/stage-gate"), headers=AUTH,
        json={"actor": "Руководитель", "decision": "ready", "rationale": "Пробуем утвердить",
              "blockers": ["Нет согласования"], "required_validations": []},
    )
    assert blocked.status_code == 409

    conditional = client.post(
        _url(review_scan, "/stage-gate"), headers=AUTH,
        json={"actor": "Руководитель", "decision": "conditional_ready",
              "rationale": "Нужны интервью", "blockers": [], "required_validations": ["Интервью"]},
    )
    assert conditional.status_code == 200, conditional.text
    assert conditional.json()["audit_phase"] == "stage_gate_decided"
    assert conditional.json()["report_status"] == "internal_draft"

    ready = client.post(
        _url(review_scan, "/stage-gate"), headers=AUTH,
        json={"actor": "Руководитель", "decision": "ready", "rationale": "Проверено аналитиком",
              "blockers": [], "required_validations": []},
    )
    assert ready.status_code == 200, ready.text
    assert ready.json()["audit_phase"] == "client_ready"
    assert ready.json()["report_status"] == "client_ready"

    report = client.get(_url(review_scan, "/report.json"), headers=AUTH)
    assert report.status_code == 200
    assert report.json()["meta"]["report_status"] == "client_ready"

    edited = client.patch(
        _url(review_scan, f"/registers/findings/{review_scan['finding']}"), headers=AUTH,
        json={"actor": "Аналитик", "changes": {"verification": "Запросить два интервью"}},
    )
    assert edited.status_code == 200, edited.text

    from sqlalchemy import select

    from app.db import get_session_factory
    from app.registers.models import ReportSnapshot, StageGate
    from app.models import Scan

    with get_session_factory()() as session:
        scan = session.get(Scan, review_scan["scan"])
        gate = session.get(StageGate, review_scan["gate"])
        snapshots = list(session.scalars(select(ReportSnapshot).where(
            ReportSnapshot.scan_id == review_scan["scan"]
        )))

    assert scan.audit_phase == "analyst_review"
    assert gate.approved_by is None
    assert any(snapshot.status == "approved" for snapshot in snapshots) is False
    assert all(snapshot.status in {"needs_rebuild", "superseded"} for snapshot in snapshots)


def test_scorecard_na_and_desk_mode_guards(review_scan):
    na = client.patch(
        _url(review_scan, f"/registers/scorecard/{review_scan['scorecard']}"), headers=AUTH,
        json={"actor": "Аналитик", "changes": {"score": None, "na_reason": "Нет интервью",
                                               "confidence": "high"}},
    )
    assert na.status_code == 200, na.text
    assert na.json()["score"] is None
    assert na.json()["confidence"] is None

    from sqlalchemy import select

    from app.db import get_session_factory
    from app.registers.models import JourneyStep

    with get_session_factory()() as session:
        journey_id = session.scalar(select(JourneyStep.id).where(
            JourneyStep.scan_id == review_scan["scan"]
        ))
    observed = client.patch(
        _url(review_scan, f"/registers/journey/{journey_id}"), headers=AUTH,
        json={"actor": "Аналитик", "changes": {"is_observed": True}},
    )
    assert observed.status_code == 422
    experience_score = client.patch(
        _url(review_scan, f"/registers/scorecard/{review_scan['experience_scorecard']}"),
        headers=AUTH,
        json={"actor": "Аналитик", "changes": {"score": 5, "confidence": "high"}},
    )
    assert experience_score.status_code == 422


def test_pdf_is_cached_by_content_hash(review_scan, monkeypatch):
    calls = 0

    def fake_render(document):
        nonlocal calls
        calls += 1
        return b"%PDF-1.7\ncached-report"

    monkeypatch.setattr("app.reports.artifacts.render_pdf", fake_render)
    first = client.get(_url(review_scan, "/report.pdf"), headers=AUTH)
    second = client.get(_url(review_scan, "/report.pdf"), headers=AUTH)
    assert first.status_code == second.status_code == 200
    assert first.headers["x-report-cache"] == "MISS"
    assert second.headers["x-report-cache"] == "HIT"
    assert first.content == second.content == b"%PDF-1.7\ncached-report"
    assert first.headers["etag"] == second.headers["etag"]
    assert calls == 1

    from sqlalchemy import select

    from app.db import get_session_factory
    from app.registers.models import ReportArtifact

    with get_session_factory()() as session:
        artifacts = list(session.scalars(select(ReportArtifact).where(
            ReportArtifact.scan_id == review_scan["scan"]
        )))
    assert len(artifacts) == 1


def test_review_ui_and_register_listing(review_scan):
    listing = client.get(_url(review_scan, "/registers/findings"), headers=AUTH)
    assert listing.status_code == 200
    assert listing.json()[0]["id"] == str(review_scan["finding"])

    page = client.get(f"/ui/scans/{review_scan['scan']}/review?register=findings", headers=AUTH)
    assert page.status_code == 200, page.text
    for needle in ("Client Brief v2.0", "Stage 02 Readiness Gate", "История ревью",
                   "field__statement", "G/M/W"):
        assert needle in page.text

    transition = client.post(
        f"/ui/scans/{review_scan['scan']}/review/action", headers=AUTH,
        data={"actor": "UI аналитик", "action": "start", "note": "Начато из UI"},
        follow_redirects=False,
    )
    assert transition.status_code == 303
    events = client.get(_url(review_scan, "/review-events"), headers=AUTH)
    assert events.status_code == 200
    assert events.json()[0]["actor"] == "UI аналитик"
    assert events.json()[0]["action"] == "reopen"
