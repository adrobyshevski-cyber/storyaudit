"""API паспорта: PATCH подтверждает значения, GET отдаёт их вместе с эвристикой."""

import os

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app

client = TestClient(app)


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_settings().app_access_token}"}


@pytest.fixture
def scan_with_corpus():
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    import uuid

    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from app.analysis.passport import refresh_passport
    from app.db import get_session_factory
    from app.models import Block, Document, Source
    from app.scans import create_scan
    from app.schemas import ScanCreate

    db = get_session_factory()()
    try:
        db.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip("PostgreSQL is not available")

    corpus = (
        "Индивидуальная супервизия для психологов: разбираем сложные случаи. "
        "Терапия и консультирование клиентов. Стоимость 18 000 ₽, Москва."
    )
    scan = create_scan(db, ScanCreate(title="Passport API fixture"))
    source = Source(scan_id=scan.id, name="practice.ru", type="website", external_id="practice.ru", source_metadata={})
    db.add(source)
    db.flush()
    document = Document(
        source_id=source.id, title="practice.ru", url="https://practice.ru/",
        content_hash=uuid.uuid4().hex, text=corpus,
    )
    db.add(document)
    db.flush()
    db.add(Block(document_id=document.id, ordinal=1, text=corpus, start_offset=0, end_offset=len(corpus)))
    db.commit()
    refresh_passport(db, scan)
    db.commit()
    scan_id = scan.id
    db.close()
    return scan_id


def test_passport_get_and_patch(scan_with_corpus):
    scan_id = scan_with_corpus

    unauthorised = client.get(f"/scans/{scan_id}/passport")
    assert unauthorised.status_code == 401

    current = client.get(f"/scans/{scan_id}/passport", headers=auth())
    assert current.status_code == 200
    body = current.json()
    assert body["geography"] == "Россия"
    assert body["category"] == "Психология и терапия"
    assert body["status"] == "derived"
    assert body["derived"]["category"]["value"] == "Психология и терапия"

    patched = client.patch(
        f"/scans/{scan_id}/passport",
        headers=auth(),
        json={
            "actor": "Аналитик",
            "note": "Уточнено по интервью",
            "geography": "Россия, Москва",
            "category": "Психология и супервизия",
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["category_status"] == "confirmed"
    assert patched.json()["geography"] == "Россия, Москва"

    after = client.get(f"/scans/{scan_id}/passport", headers=auth()).json()
    assert after["status"] == "confirmed"
    assert after["category"] == "Психология и супервизия"
