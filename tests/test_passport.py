"""Паспорт отчёта: география и категория выводятся из корпуса.

Эвристики проверяются на текстах без БД, поведение с подтверждением аналитика —
на реальной базе (пропускается без TEST_DATABASE_URL).
"""

import os
import uuid

import pytest

from app.analysis.passport import (
    confirm_passport,
    detect_category,
    detect_geography,
    passport_view,
    refresh_passport,
)

PSYCHOLOGY = (
    "Индивидуальная супервизия для психологов: разбираем сложные случаи. "
    "Терапия и консультирование клиентов, эмоциональная поддержка специалиста. "
    "Стоимость сессии 18 000 ₽, работаем в Москве."
)

SHOP = (
    "Интернет-магазин с доставкой по России: корзина, оформление заказа, "
    "пункты выдачи, оплата картой, скидки и акции. Товары для дома."
)

NOISE = "Мы работаем с 2010 года и ценим качество."


def test_geography_by_domain_currency_and_city():
    result = detect_geography(PSYCHOLOGY, ["example.ru"])
    assert result["value"] == "Россия"
    assert result["confidence"] in {"high", "medium"}
    assert any("домен" in item for item in result["evidence"])
    assert any("₽" in item for item in result["evidence"])


def test_geography_of_kazakhstan_site():
    result = detect_geography("Стоимость 45 000 ₸, офис в Алматы.", ["shop.kz"])
    assert result["value"] == "Казахстан"


def test_geography_without_signals_is_empty():
    result = detect_geography(NOISE, ["example.com"])
    assert result["value"] is None
    assert result["confidence"] == "none"
    assert result["evidence"] == []


def test_category_of_psychology_practice():
    result = detect_category(PSYCHOLOGY)
    assert result["value"] == "Психология и терапия"
    assert result["confidence"] == "high"
    assert result["evidence"]


def test_category_of_online_shop():
    assert detect_category(SHOP)["value"] == "Электронная коммерция"


def test_category_without_signal_is_none():
    result = detect_category(NOISE)
    assert result["value"] is None
    assert result["confidence"] == "none"


@pytest.fixture
def session():
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from app.db import get_session_factory

    db = get_session_factory()()
    try:
        db.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip("PostgreSQL is not available")
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _scan_with_corpus(session, text: str, host: str, name: str = "Passport fixture"):
    from app.models import Block, Document, Source
    from app.scans import create_scan
    from app.schemas import ScanCreate

    scan = create_scan(session, ScanCreate(title=name))
    source = Source(scan_id=scan.id, name=host, type="website", external_id=host, source_metadata={})
    session.add(source)
    session.flush()
    document = Document(
        source_id=source.id, title=host, url=f"https://{host}/",
        content_hash=uuid.uuid4().hex, text=text,
    )
    session.add(document)
    session.flush()
    session.add(Block(
        document_id=document.id, ordinal=1, text=text, start_offset=0, end_offset=len(text),
    ))
    session.commit()
    return scan


def test_refresh_sets_derived_passport(session):
    scan = _scan_with_corpus(session, PSYCHOLOGY, "practice.ru")
    assert scan.geography is None and scan.category is None

    refresh_passport(session, scan)
    session.commit()

    assert scan.geography == "Россия"
    assert scan.category == "Психология и терапия"
    assert scan.category_status == "derived"
    assert scan.passport["derived"]["category"]["confidence"] == "high"
    assert scan.passport["derived_at"]

    view = passport_view(session, scan)
    assert view["geography"] == "Россия"
    assert view["status"] == "derived"


def test_confirm_overrides_and_blocks_automation(session):
    scan = _scan_with_corpus(session, PSYCHOLOGY, "practice.ru")
    refresh_passport(session, scan)
    session.commit()

    confirm_passport(
        session, scan, actor="Аналитик", geography="Россия, Москва",
        category="Психология", note="Уточнено по интервью",
    )
    assert scan.category_status == "confirmed"
    assert scan.geography == "Россия, Москва"

    refresh_passport(session, scan)  # повторный анализ
    session.commit()
    assert scan.geography == "Россия, Москва", "автоматика перезаписала подтверждённое"
    assert scan.category == "Психология"

    from sqlalchemy import select

    from app.registers.models import ReviewEvent

    event = session.scalar(
        select(ReviewEvent).where(ReviewEvent.scan_id == scan.id, ReviewEvent.entity_type == "passport")
    )
    assert event is not None and event.actor == "Аналитик"


def test_passport_section_shows_value_and_provenance(session):
    from app.reports.build_v2 import _section_passport

    scan = _scan_with_corpus(session, PSYCHOLOGY, "practice.ru")
    refresh_passport(session, scan)
    session.commit()

    section = _section_passport(scan, registry=[], documents=1, blocks=1)
    flat = {
        item["key"]: item["value"]
        for block in section.blocks if block.type == "kv"
        for item in block.items
    }
    assert flat.get("География") == "Россия · по корпусу"
    assert flat.get("Категория") == "Психология и терапия · по корпусу"


def test_passport_section_falls_back_when_nothing_derived(session):
    from app.reports.build_v2 import _section_passport

    scan = _scan_with_corpus(session, NOISE, "example.com")
    section = _section_passport(scan, registry=[], documents=1, blocks=1)
    flat = {
        item["key"]: item["value"]
        for block in section.blocks if block.type == "kv"
        for item in block.items
    }
    assert flat.get("География") == "не подтверждена"
    assert flat.get("Категория") == "Неизвестна"
