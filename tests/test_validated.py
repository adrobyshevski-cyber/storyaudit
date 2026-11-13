import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.anonymize import anonymize, assert_clean
from app.config import get_settings
from app.db import get_session_factory
from app.errors import AppError
from app.main import app
from app.schemas import ScanCreate
from app.scans import create_scan
from app.validated import (
    add_segment,
    approve_session,
    coverage,
    create_session,
    list_segments,
    record_consent,
    set_audit_mode,
)

client = TestClient(app)


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_settings().app_access_token}"}


def require_db():
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    session = get_session_factory()()
    try:
        session.execute(text("SELECT 1"))
    except OperationalError:
        session.close()
        pytest.skip("PostgreSQL is not available")
    return session


def validated_scan(session) -> uuid.UUID:
    scan = create_scan(session, ScanCreate(title="Validated probe"))
    set_audit_mode(session, scan.id, "validated")
    return scan.id


RAW = (
    "Меня зовут Анна Петрова, я работаю в ООО Ромашка. Пишите на anna@example.com "
    "или +7 (999) 123-45-67, сайт https://example.com/about, профиль @anna_p. "
    "Главное: обещали сопровождение, но после оплаты отвечали через неделю. "
    "Внутренний номер договора 1234567."
)


# --- anonymisation ---------------------------------------------------------


def test_anonymize_removes_contacts_and_numbers():
    result = anonymize(RAW)
    for forbidden in ("anna@example.com", "999", "example.com", "@anna_p", "1234567"):
        assert forbidden not in result.text
    assert "[email]" in result.text
    assert "[телефон]" in result.text
    assert "[ссылка]" in result.text
    assert result.findings["email"] == 1
    assert result.findings["phone"] == 1
    assert result.total_redactions >= 4
    assert_clean(result.text)


def test_anonymize_redacts_supplied_names_and_companies():
    result = anonymize(RAW, extra_terms={"Анна Петрова": "name", "ООО Ромашка": "organization"})
    assert "Анна Петрова" not in result.text
    assert "Ромашка" not in result.text
    assert "[имя]" in result.text
    assert "[организация]" in result.text


def test_assert_clean_rejects_leftovers():
    with pytest.raises(ValueError):
        assert_clean("write to someone@example.com")


# --- service rules ---------------------------------------------------------


def test_interviews_require_validated_mode():
    session = require_db()
    try:
        scan = create_scan(session, ScanCreate(title="Desk probe"))
        with pytest.raises(AppError) as excinfo:
            create_session(session, scan.id, {"anonymous_ref": "P1"})
        assert excinfo.value.status_code == 409
        assert "validated" in excinfo.value.message
    finally:
        session.close()


def test_segment_requires_consent_and_stores_only_redacted_text():
    session = require_db()
    try:
        scan_id = validated_scan(session)
        record = create_session(session, scan_id, {
            "anonymous_ref": "P1: маркетолог, 8 лет опыта",
            "layer": "experienced",
            "sample_note": "3 клиента малого бизнеса",
        })
        with pytest.raises(AppError) as excinfo:
            add_segment(session, scan_id, record.id, text=RAW)
        assert excinfo.value.status_code == 409
        assert "согласия" in excinfo.value.message

        record_consent(session, scan_id, record.id, status="granted",
                       scope="цитаты без имени и контактов", reference="consent-2026-11")
        segment = add_segment(session, scan_id, record.id, text=RAW,
                              extra_terms={"Анна Петрова": "name"})
        assert "anna@example.com" not in segment.text
        assert "[email]" in segment.text
        assert segment.redactions["email"] == 1
    finally:
        session.close()


def test_dry_run_previews_without_storing():
    session = require_db()
    try:
        scan_id = validated_scan(session)
        record = create_session(session, scan_id, {"anonymous_ref": "P2", "sample_note": "n=1"})
        record_consent(session, scan_id, record.id, status="granted", scope="quotes")
        preview = add_segment(session, scan_id, record.id, text=RAW, dry_run=True)
        assert isinstance(preview, dict)
        assert "[email]" in preview["text"]
        assert list_segments(session, scan_id, record.id) == []
    finally:
        session.close()


def test_approval_needs_consent_sample_note_and_segments():
    session = require_db()
    try:
        scan_id = validated_scan(session)
        record = create_session(session, scan_id, {"anonymous_ref": "P3"})
        record_consent(session, scan_id, record.id, status="granted", scope="quotes")
        with pytest.raises(AppError) as missing_sample:
            approve_session(session, scan_id, record.id, actor="Ирина")
        assert missing_sample.value.status_code == 422

        record.sample_note = "2 участника, B2B-услуги"
        session.commit()
        with pytest.raises(AppError) as missing_segments:
            approve_session(session, scan_id, record.id, actor="Ирина")
        assert missing_segments.value.status_code == 409

        add_segment(session, scan_id, record.id, text="Обещали сопровождение, отвечали неделю.")
        approved = approve_session(session, scan_id, record.id, actor="Ирина")
        assert approved.status == "approved"
        assert approved.approved_by == "Ирина"
    finally:
        session.close()


def test_consent_withdrawal_blocks_further_material():
    session = require_db()
    try:
        scan_id = validated_scan(session)
        record = create_session(session, scan_id, {"anonymous_ref": "P4", "sample_note": "n=1"})
        record_consent(session, scan_id, record.id, status="granted", scope="quotes")
        add_segment(session, scan_id, record.id, text="Фрагмент без персональных данных.")
        approve_session(session, scan_id, record.id, actor="Ирина")
        record_consent(session, scan_id, record.id, status="withdrawn", scope=None, reference=None)
        with pytest.raises(AppError) as excinfo:
            add_segment(session, scan_id, record.id, text="Ещё фрагмент.")
        assert excinfo.value.status_code == 409
        assert record.status == "draft"
        assert record.approved_by is None
    finally:
        session.close()


def test_coverage_is_honest_about_representativeness():
    session = require_db()
    try:
        scan_id = validated_scan(session)
        experienced = create_session(session, scan_id, {
            "anonymous_ref": "P5", "layer": "experienced", "sample_note": "n=2",
        })
        record_consent(session, scan_id, experienced.id, status="granted", scope="quotes")
        add_segment(session, scan_id, experienced.id, text="Опыт взаимодействия: долгий онбординг.")
        approve_session(session, scan_id, experienced.id, actor="Ирина")

        summary = coverage(session, scan_id)
        assert summary["audit_mode"] == "validated"
        assert summary["sessions_approved"] == 1
        assert summary["segments_by_layer"]["experienced"] == 1
        assert summary["experienced_available"] is True
        assert summary["perceived_available"] is False
        assert summary["representative"] is False
        assert "не является статистически репрезентативной" in summary["note"]
    finally:
        session.close()


def test_desk_mode_cannot_be_restored_with_approved_interviews():
    session = require_db()
    try:
        scan_id = validated_scan(session)
        record = create_session(session, scan_id, {"anonymous_ref": "P6", "sample_note": "n=1"})
        record_consent(session, scan_id, record.id, status="granted", scope="quotes")
        add_segment(session, scan_id, record.id, text="Фрагмент.")
        approve_session(session, scan_id, record.id, actor="Ирина")
        with pytest.raises(AppError) as excinfo:
            set_audit_mode(session, scan_id, "desk")
        assert excinfo.value.status_code == 409
    finally:
        session.close()


# --- API surface -----------------------------------------------------------


def test_api_validated_flow():
    session = require_db()
    try:
        scan = create_scan(session, ScanCreate(title="API validated"))
        scan_id = scan.id
    finally:
        session.close()
    headers = auth()

    mode = client.patch(f"/scans/{scan_id}/audit-mode", json={"mode": "validated"}, headers=headers)
    assert mode.status_code == 200, mode.text
    assert mode.json()["audit_mode"] == "validated"

    created = client.post(f"/scans/{scan_id}/interviews", headers=headers, json={
        "anonymous_ref": "P7: владелец студии",
        "layer": "perceived",
        "sample_note": "1 участник, пилот",
    })
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    consent = client.patch(
        f"/scans/{scan_id}/interviews/{session_id}/consent",
        headers=headers,
        json={"status": "granted", "scope": "цитаты без контактов", "reference": "consent-1"},
    )
    assert consent.status_code == 200, consent.text

    segment = client.post(
        f"/scans/{scan_id}/interviews/{session_id}/segments",
        headers=headers,
        json={"text": "Сказали, что бренд понятен. Пишите на pro@example.com.", "theme": "Experience"},
    )
    assert segment.status_code == 201, segment.text
    assert "pro@example.com" not in segment.json()["text"]

    approved = client.post(
        f"/scans/{scan_id}/interviews/{session_id}/approve",
        headers=headers, json={"actor": "Ирина"},
    )
    assert approved.status_code == 200, approved.text

    summary = client.get(f"/scans/{scan_id}/validated", headers=headers)
    assert summary.status_code == 200
    body = summary.json()
    assert body["perceived_available"] is True
    assert body["sessions_approved"] == 1

    listing = client.get(f"/scans/{scan_id}/interviews", headers=headers)
    assert listing.status_code == 200
    assert listing.json()[0]["code"] == "IV01"


def test_ui_interviews_page_renders_and_accepts_a_segment():
    session = require_db()
    try:
        scan_id = validated_scan(session)
        record = create_session(session, scan_id, {
            "anonymous_ref": "P8: основатель практики",
            "layer": "perceived",
            "sample_note": "1 участник",
        })
        record_consent(session, scan_id, record.id, status="granted", scope="quotes")
        session_id = record.id
    finally:
        session.close()

    page = client.get(f"/ui/scans/{scan_id}/interviews",
                      auth=("audit", get_settings().app_access_token))
    assert page.status_code == 200, page.text
    assert "Validated mode" in page.text
    assert "P8: основатель практики" in page.text

    posted = client.post(
        f"/ui/scans/{scan_id}/interviews/{session_id}/actions/segment",
        data={"text": "Бренд понятен, но звоните +7 999 111-22-33.", "theme": "Experience"},
        auth=("audit", get_settings().app_access_token),
        follow_redirects=False,
    )
    assert posted.status_code == 303

    check = require_db()
    try:
        stored = list_segments(check, scan_id, session_id)
        assert len(stored) == 1
        assert "999" not in stored[0]["text"]
        assert "[телефон]" in stored[0]["text"]
    finally:
        check.close()
