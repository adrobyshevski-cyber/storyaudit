"""Passport of the report: geography and category derived from the corpus.

The passport used to be filled by hand: ``scan.geography`` stayed empty and
``scan.category_status`` only said "Неизвестна", so the client saw a report that
did not name the market it was about. Detection here is deterministic and
explainable: every value comes with the evidence that produced it, and the
analyst can confirm or override it in the review UI (then the status becomes
``confirmed`` and automatic detection stops touching the field).
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Block, Document, Scan, Source
from app.utils import canonical_host

# --- география ---------------------------------------------------------------

GEO_BY_TLD = {
    "ru": "Россия",
    "рф": "Россия",
    "su": "Россия",
    "kz": "Казахстан",
    "by": "Беларусь",
    "ua": "Украина",
    "ge": "Грузия",
    "am": "Армения",
    "uz": "Узбекистан",
    "kg": "Кыргызстан",
}

GEO_BY_MARKER = {
    "₽": "Россия",
    "руб": "Россия",
    "₸": "Казахстан",
    "тенге": "Казахстан",
    "₴": "Украина",
    "грн": "Украина",
    "₾": "Грузия",
    "лари": "Грузия",
    "֏": "Армения",
    "драм": "Армения",
}

GEO_BY_CITY = {
    "москва": "Россия",
    "московск": "Россия",
    "санкт-петербург": "Россия",
    "петербург": "Россия",
    "новосибирск": "Россия",
    "екатеринбург": "Россия",
    "казань": "Россия",
    "нижний новгород": "Россия",
    "самара": "Россия",
    "краснодар": "Россия",
    "ростов-на-дону": "Россия",
    "алматы": "Казахстан",
    "астана": "Казахстан",
    "минск": "Беларусь",
    "киев": "Украина",
    "тбилиси": "Грузия",
    "ереван": "Армения",
    "ташкент": "Узбекистан",
}

# --- категория ---------------------------------------------------------------

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Психология и терапия": (
        "психолог", "психотерап", "супервизи", "терапи", "консультирован",
        "эмоциональн", "тревог", "клиентск", "сесси",
    ),
    "Образование и курсы": (
        "курс", "обучени", "студент", "преподават", "вебинар", "лекци",
        "диплом", "школ", "программа обучени", "ментор",
    ),
    "Маркетинг и реклама": (
        "маркетинг", "реклам", "бренд", "продвижени", "smm", "кампани",
        "лидогенерац", "медийн", "контент-маркетинг", "таргетирован",
    ),
    "SaaS и ИТ": (
        "платформ", "интеграц", "api", "crm", "saas", "интерфейс",
        "дашборд", "автоматизац", "софт", "сервер",
    ),
    "Электронная коммерция": (
        "доставк", "интернет-магазин", "корзин", "оформлени заказа",
        "пункт выдач", "оплат", "маркетплейс", "товар", "скидк",
    ),
    "Финансы и инвестиции": (
        "банк", "кредит", "инвест", "капитал", "процентная ставк",
        "платеж", "финансов", "брокер", "портфел", "депозит",
    ),
    "Медицина и клиники": (
        "клиник", "врач", "медицин", "пациент", "диагност", "лечени",
        "приём специалиста", "анализ крови",
    ),
    "Юридические услуги": (
        "юрист", "адвокат", "юридическ", "арбитраж", "договор", "судебн",
        "правов", "нотариус",
    ),
    "HR и подбор": (
        "ваканси", "резюме", "найм", "рекрут", "персонал", "hr",
        "кандидат", "подбор персонал",
    ),
    "Недвижимость": (
        "квартир", "недвижимост", "аренд", "застройщик", "ипотек",
        "жилой комплекс", "жк ",
    ),
    "Консалтинг и B2B-услуги": (
        "консалтинг", "аудит", "стратег", "исследован", "b2b",
        "тендер", "интегратор", "внедрени",
    ),
}

MAX_CHARS = 400_000
MIN_CATEGORY_SCORE = 3


def _corpus_text(session: Session, scan_id) -> str:
    """Текст корпуса для эвристик: без блоков-дублей, с ограничением длины."""
    rows = session.execute(
        select(Block.text)
        .join(Document, Block.document_id == Document.id)
        .join(Source, Document.source_id == Source.id)
        .where(Source.scan_id == scan_id)
        .order_by(Block.document_id, Block.ordinal)
    )
    parts: list[str] = []
    size = 0
    for (text,) in rows:
        if not text:
            continue
        parts.append(text)
        size += len(text)
        if size >= MAX_CHARS:
            break
    return "\n".join(parts)


def _corpus_hosts(session: Session, scan_id) -> list[str]:
    return [
        canonical_host(source.external_id or source.name)
        for source in session.scalars(select(Source).where(Source.scan_id == scan_id))
    ]


def detect_geography(text: str, hosts: list[str]) -> dict[str, Any]:
    """Страна рынка по домену, валюте и городам — с указанием оснований."""
    votes: Counter[str] = Counter()
    evidence: dict[str, list[str]] = {}

    def vote(country: str, weight: int, reason: str) -> None:
        votes[country] += weight
        evidence.setdefault(country, [])
        if reason not in evidence[country]:
            evidence[country].append(reason)

    for host in hosts:
        if not host:
            continue
        tld = host.rsplit(":", 1)[0].rsplit(".", 1)[-1]
        country = GEO_BY_TLD.get(tld)
        if country:
            vote(country, 3, f"домен .{tld}")

    lowered = text.lower()
    for marker, country in GEO_BY_MARKER.items():
        count = lowered.count(marker)
        if count:
            vote(country, min(3, 1 + count // 5), f"«{marker}» — {count} раз")

    for city, country in GEO_BY_CITY.items():
        if city in lowered:
            vote(country, 2, f"город «{city}»")

    if not votes:
        return {"value": None, "confidence": "none", "evidence": []}

    country, score = votes.most_common(1)[0]
    runner_up = votes.most_common(2)[1][1] if len(votes) > 1 else 0
    confidence = "high" if score >= 3 and score > runner_up else "medium" if score >= 2 else "low"
    return {"value": country, "confidence": confidence, "evidence": evidence[country][:4]}


def detect_category(text: str) -> dict[str, Any]:
    """Отрасль по словарю: считаем различные совпадения, а не частоту слов."""
    lowered = text.lower()
    scores: dict[str, list[str]] = {}
    for category, keywords in CATEGORY_KEYWORDS.items():
        hits = [keyword for keyword in keywords if keyword in lowered]
        if hits:
            scores[category] = hits

    if not scores:
        return {"value": None, "confidence": "none", "evidence": []}

    ranked = sorted(scores.items(), key=lambda item: (-len(item[1]), item[0]))
    category, hits = ranked[0]
    runner_up = len(ranked[1][1]) if len(ranked) > 1 else 0
    if len(hits) < MIN_CATEGORY_SCORE:
        confidence = "low"
    elif len(hits) >= 2 * max(runner_up, 1):
        confidence = "high"
    else:
        confidence = "medium"
    return {"value": category, "confidence": confidence, "evidence": hits[:5]}


def derive_passport(session: Session, scan: Scan) -> dict[str, Any]:
    """Посчитать паспорт, ничего не записывая."""
    text = _corpus_text(session, scan.id)
    hosts = _corpus_hosts(session, scan.id)
    return {
        "geography": detect_geography(text, hosts),
        "category": detect_category(text),
    }


def refresh_passport(session: Session, scan: Scan, *, force: bool = False) -> dict[str, Any]:
    """Проставить геометрию и категорию из корпуса.

    Подтверждённые аналитиком значения не перезаписываются: `category_status ==
    "confirmed"` — это стоп-сигнал для автоматики. `force=True` пересчитывает
    производные значения после повторного анализа.
    """
    derived = derive_passport(session, scan)
    meta = dict(scan.passport or {})
    meta["derived"] = derived
    meta["derived_at"] = datetime.now(timezone.utc).isoformat()
    scan.passport = meta

    if scan.category_status != "confirmed" or force:
        geography = derived["geography"]["value"]
        category = derived["category"]["value"]
        changed = False
        if geography and (force or not scan.geography):
            scan.geography = geography
            changed = True
        if category and (force or not scan.category):
            scan.category = category
            scan.category_status = "derived" if scan.category_status != "declared" else "declared"
            changed = True
        if changed and scan.category_status != "declared":
            scan.category_status = "derived"
    return derived


def confirm_passport(
    session: Session,
    scan: Scan,
    *,
    actor: str,
    geography: str | None = None,
    category: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Аналитик подтверждает или правит паспорт — автоматика больше не вмешивается."""
    before = {"geography": scan.geography, "category": scan.category, "category_status": scan.category_status}
    if geography is not None:
        scan.geography = geography.strip() or None
    if category is not None:
        scan.category = category.strip() or None
    scan.category_status = "confirmed"
    after = {"geography": scan.geography, "category": scan.category, "category_status": scan.category_status}

    from app.review import record_passport_change

    record_passport_change(session, scan, actor=actor, note=note, before=before, after=after)
    session.commit()
    return after


def passport_view(session: Session, scan: Scan) -> dict[str, Any]:
    meta = dict(scan.passport or {})
    return {
        "geography": scan.geography,
        "category": scan.category,
        "status": scan.category_status,
        "derived": meta.get("derived") or derive_passport(session, scan),
        "derived_at": meta.get("derived_at"),
    }
