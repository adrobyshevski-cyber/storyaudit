"""Narrative landscape: a small, explainable selection of alternatives.

Ingestion is limited to public pages, respects robots.txt and rate limits, and
records the selection reason for every alternative. Extraction in mock mode is
deterministic and quote-based; nothing is inferred beyond the page text.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.common import propositions_by_source, registry_map
from app.analysis.products import PRICE_RE, _audience
from app.models import Proposition, Scan, Source
from app.registers.codes import allocate_code
from app.registers.models import LandscapeEntry

PROMISE_MARKERS = ("помога", "позволя", "чтобы", "результат", "увидеть", "получ", "даёт", "дает")
PROBLEM_MARKERS = ("теря", "не видит", "не понима", "тупик", "сложн", "застрева", "проблем")
PROOF_MARKERS = ("лет", "стаж", "сертифик", "образовани", "кейс", "отзыв")
HELPER_MARKERS = ("эксперт", "консульт", "супервиз", "коуч", "наставн", "практик")
STOP_TERMS = {
    "который", "которая", "которые", "чтобы", "также", "более", "может", "можно",
    "нужно", "будет", "через", "после", "перед", "между", "этого", "этому", "своих",
    "своей", "очень", "просто", "вместе", "каждый", "любой", "другой", "другая",
    "their", "there", "these", "those", "which", "while", "about", "after",
}
TERM_RE = re.compile(r"[а-яёa-z]{5,}", re.IGNORECASE)


@dataclass
class LandscapeDraft:
    name: str
    url: str
    source_code: str
    inclusion_reason: str
    audience: str | None = None
    opponent: str | None = None
    object: str | None = None
    helper_role: str | None = None
    promise: str | None = None
    proof: str | None = None
    price: str | None = None
    geographic_scope: str | None = None
    territory_notes: str | None = None

    def as_payload(self) -> dict:
        return asdict(self)


def competitor_sources(session: Session, scan_id: uuid.UUID) -> list[Source]:
    sources = session.scalars(select(Source).where(Source.scan_id == scan_id).order_by(Source.created_at))
    return [
        source for source in sources
        if (source.source_metadata or {}).get("role") == "competitor"
    ]


def _pick(propositions: list[Proposition], markers: tuple[str, ...], limit: int = 500) -> str | None:
    for proposition in propositions:
        text = " ".join(proposition.text.split())
        if any(marker in text.lower() for marker in markers):
            return text[:limit]
    return None


def _first(propositions: list[Proposition], limit: int = 500) -> str | None:
    for proposition in propositions:
        text = " ".join(proposition.text.split())
        if len(text) >= 20:
            return text[:limit]
    return None


def build_landscape_draft(session: Session, scan: Scan) -> list[LandscapeDraft]:
    registry = registry_map(session, scan)
    grouped = propositions_by_source(session, scan.id)
    drafts: list[LandscapeDraft] = []
    for source in competitor_sources(session, scan.id):
        entry = registry.get(source.id)
        if entry is None:
            continue
        propositions = grouped.get(source.id, [])
        if not propositions:
            continue
        text = "\n".join(item.text for item in propositions)
        price_match = PRICE_RE.search(text)
        metadata = source.source_metadata or {}
        reason = (metadata.get("selection_reason") or "").strip()
        drafts.append(LandscapeDraft(
            name=" ".join(source.name.split())[:300],
            url=entry.url or metadata.get("entry_url") or "",
            source_code=entry.code,
            inclusion_reason=reason or "Критерий включения не указан",
            audience=_audience(text),
            opponent=_pick(propositions, PROBLEM_MARKERS),
            object=_pick(propositions, PROMISE_MARKERS),
            helper_role=_pick(propositions, HELPER_MARKERS),
            promise=_pick(propositions, PROMISE_MARKERS) or _first(propositions),
            proof=_pick(propositions, PROOF_MARKERS),
            price=" ".join(price_match.group(0).split()) if price_match else None,
            geographic_scope=None,
            territory_notes=None,
        ))
    return drafts


def persist_landscape(session: Session, scan: Scan, drafts: list[LandscapeDraft]) -> list[str]:
    existing = {
        row.url for row in session.scalars(
            select(LandscapeEntry).where(LandscapeEntry.scan_id == scan.id)
        )
    }
    codes: list[str] = []
    for draft in drafts:
        if draft.url in existing:
            continue
        existing.add(draft.url)
        code = allocate_code(session, scan.id, "C")
        session.add(LandscapeEntry(
            scan_id=scan.id,
            code=code,
            name=draft.name,
            url=draft.url,
            source_code=draft.source_code,
            audience=draft.audience,
            opponent=draft.opponent,
            object=draft.object,
            helper_role=draft.helper_role,
            promise=draft.promise,
            proof=draft.proof,
            price=draft.price,
            inclusion_reason=draft.inclusion_reason,
            geographic_scope=draft.geographic_scope,
            territory_notes=draft.territory_notes,
            fetched_at=datetime.now(timezone.utc),
        ))
        codes.append(code)
    session.flush()
    return codes


def key_terms(text: str | None) -> set[str]:
    if not text:
        return set()
    terms = {
        match.group(0).lower()
        for match in TERM_RE.finditer(text)
    }
    return {term for term in terms if term not in STOP_TERMS}


def overlap_with_competitors(
    promise: str | None,
    entries: list[LandscapeEntry],
) -> tuple[float, list[str], LandscapeEntry | None]:
    """Share of the brand's key promise terms that also appear in alternatives."""
    brand_terms = key_terms(promise)
    if len(brand_terms) < 3:
        return 0.0, [], None
    best_ratio = 0.0
    best_shared: list[str] = []
    best_entry: LandscapeEntry | None = None
    for entry in entries:
        competitor_terms = key_terms(entry.promise) | key_terms(entry.object)
        shared = sorted(brand_terms & competitor_terms)
        ratio = len(shared) / len(brand_terms)
        if ratio > best_ratio:
            best_ratio = ratio
            best_shared = shared
            best_entry = entry
    if best_entry is None and entries:
        best_entry = entries[0]
    return best_ratio, best_shared, best_entry
