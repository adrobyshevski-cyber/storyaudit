"""Дубли источников: один домен — одна запись S-реестра.

Проверяем три вещи: канонический хост, отказ на повторное добавление того же
домена и схлопывание уже существующих дублей с переносом ссылок в регистрах.
"""

import os
import uuid

import pytest

from app.utils import canonical_host


def test_canonical_host_collapses_url_variants():
    for value in (
        "example.com",
        "https://example.com/",
        "https://example.com./",
        "https://example.com:443/pricing",
        "  WWW.example.com  ",
    ):
        assert canonical_host(value) == "example.com", value
    assert canonical_host("") == ""
    assert canonical_host(None) == ""
    assert canonical_host("www.shop.example.org/path") == "shop.example.org"
    # нестандартный порт — другой сайт
    assert canonical_host("http://www.Example.com:8080/pricing") == "example.com:8080"
    assert canonical_host("http://127.0.0.1:41001/") == "127.0.0.1:41001"


def test_public_competitors_drop_www_and_repeats():
    from app.audit_requests import _public_competitors

    result = _public_competitors(
        [
            "https://www.example.org/pricing",
            "https://example.org",
            "https://example.net",
            "https://www.example.net/",
        ],
        client_host="example.com",
    )
    assert result == ["https://www.example.org/pricing", "https://example.net/"]


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


@pytest.fixture
def scan(session):
    from app.scans import create_scan
    from app.schemas import ScanCreate

    created = create_scan(session, ScanCreate(title="Duplicate sources"))
    session.commit()
    return created


def _page(url: str):
    from types import SimpleNamespace

    return SimpleNamespace(url=url, title="Главная", text="Текст страницы для корпуса." * 20)


def test_second_add_of_same_domain_is_rejected(session, scan, monkeypatch):
    import app.scans as scans_module

    from app.errors import AppError
    from app.schemas import WebsiteSourceCreate

    monkeypatch.setattr(scans_module, "crawl_site", lambda url, *a, **k: [_page("https://example.com/")])
    monkeypatch.setattr(scans_module, "guard_source_add", lambda *a, **k: None)

    scans_module.add_website_source(session, scan.id, WebsiteSourceCreate(url="https://example.com/"))

    with pytest.raises(AppError) as exc:
        scans_module.add_website_source(session, scan.id, WebsiteSourceCreate(url="https://www.example.com/"))
    assert exc.value.status_code == 409
    assert exc.value.code == "duplicate_source"


def test_worker_reuses_existing_source_instead_of_adding_a_twin(session, scan, monkeypatch):
    import app.scans as scans_module

    from app.schemas import WebsiteSourceCreate

    monkeypatch.setattr(scans_module, "crawl_site", lambda url, *a, **k: [_page("https://example.com/")])
    monkeypatch.setattr(scans_module, "guard_source_add", lambda *a, **k: None)

    first = scans_module.add_website_source(session, scan.id, WebsiteSourceCreate(url="https://example.com/"))
    again = scans_module.add_website_source(
        session, scan.id, WebsiteSourceCreate(url="http://www.example.com/"), reuse_existing=True
    )
    assert again.id == first.id

    from sqlalchemy import func, select

    from app.models import Source

    total = session.scalar(select(func.count(Source.id)).where(Source.scan_id == scan.id))
    assert total == 1


def test_merge_collapses_registry_duplicates_and_repoints_codes(session, scan):
    from sqlalchemy import select

    from app.models import Source
    from app.registers.models import EvidenceRecord, Product, SourceRegistryEntry
    from app.registers.sources import merge_duplicate_sources, registry_duplicates

    first = Source(
        scan_id=scan.id, name="example.com", type="website", external_id="example.com",
        source_metadata={"entry_url": "https://example.com/", "role": "client"},
    )
    twin = Source(
        scan_id=scan.id, name="www.example.com", type="website", external_id="www.example.com",
        source_metadata={"entry_url": "https://www.example.com/", "role": "client"},
    )
    other = Source(
        scan_id=scan.id, name="other.example", type="website", external_id="other.example",
        source_metadata={"entry_url": "https://other.example/", "role": "client"},
    )
    session.add_all([first, twin, other])
    session.flush()

    session.add_all([
        SourceRegistryEntry(
            scan_id=scan.id, code="S01", source_id=first.id, url="https://example.com/",
            confirms="страницы example.com", limitations=["a"], entry_metadata={"source_type": "website"},
        ),
        SourceRegistryEntry(
            scan_id=scan.id, code="S02", source_id=twin.id, url="https://www.example.com/",
            confirms="страницы www.example.com", limitations=["b"], entry_metadata={"source_type": "website"},
            content_hash="hash-2",
        ),
        SourceRegistryEntry(
            scan_id=scan.id, code="S03", source_id=other.id, url="https://other.example/",
            confirms="страницы other.example", limitations=[], entry_metadata={},
        ),
    ])
    product = Product(scan_id=scan.id, code="P01", name="Продукт", source_codes=["S02", "S03"])
    evidence = EvidenceRecord(
        scan_id=scan.id, code="E01", claim="Утверждение", source_codes=["S01", "S02"],
        allowed_conclusion="Вывод только по публичной странице",
    )
    session.add_all([product, evidence])
    session.flush()

    assert set(registry_duplicates(session, scan.id)) == {"example.com"}

    preview = merge_duplicate_sources(session, scan.id)
    assert preview == [{"host": "example.com", "kept": "S01", "merged": ["S02"], "repointed": {"Product": 1, "EvidenceRecord": 1}}]
    assert len(list(session.scalars(select(SourceRegistryEntry).where(SourceRegistryEntry.scan_id == scan.id)))) == 3, "dry-run не должен удалять строки"

    merge_duplicate_sources(session, scan.id, dry_run=False)

    rows = list(session.scalars(select(SourceRegistryEntry).where(SourceRegistryEntry.scan_id == scan.id).order_by(SourceRegistryEntry.code)))
    assert [row.code for row in rows] == ["S01", "S03"]
    assert rows[0].limitations == ["a", "b"]
    assert rows[0].content_hash == "hash-2"
    assert rows[0].entry_metadata["merged_codes"] == ["S02"]
    assert "https://www.example.com/" in rows[0].entry_metadata["merged_urls"]

    assert product.source_codes == ["S01", "S03"]
    assert evidence.source_codes == ["S01"]

    # корпус не тронут: обе исходные страницы остаются в источниках
    assert session.scalar(select(Source).where(Source.id == twin.id)) is not None
