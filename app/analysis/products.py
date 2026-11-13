"""Product map: turn the corpus into `products` rows (Current Proposition Map).

Deterministic in mock mode: one document with readable offers becomes one
product draft, prices and durations are taken verbatim from the page text.
No product is invented when the corpus has no propositions.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.common import propositions_by_source, registry_map
from app.models import Document, Proposition, Scan, Source

PRICE_RE = re.compile(r"(\d[\d\s\u00a0]{2,9})\s*(?:₽|руб(?:\.|лей|ля)?|р\.)", re.IGNORECASE)
DURATION_RE = re.compile(r"(\d{1,3})\s*(мин(?:ут)?[аы]?)", re.IGNORECASE)
BOUNDARY_RE = re.compile(r"[^.]*\bне\s+(?:гарантир|заменя|является|даёт)[^.]*\.", re.IGNORECASE)
PROOF_RE = re.compile(r"[^.]*(?:\bлет\b|стаж|сертифик|образовани|аккредит|практик)[^.]*\.", re.IGNORECASE)
AUDIENCE_MARKERS = (
    (re.compile("психолог[а-яa-z]*", re.IGNORECASE), "психологи"),
    (re.compile("коуч[а-яa-z]*", re.IGNORECASE), "коучи"),
    (re.compile("команд[а-яa-z]*", re.IGNORECASE), "команды"),
    (re.compile("компани[а-яa-z]*", re.IGNORECASE), "компании"),
    (re.compile("клиент[а-яa-z]*", re.IGNORECASE), "клиенты"),
    (re.compile("студент[а-яa-z]*", re.IGNORECASE), "студенты"),
)


@dataclass
class ProductDraft:
    name: str
    audience: str = "не определена"
    trigger_situation: str | None = None
    promise: str | None = None
    format: str | None = None
    price: str | None = None
    procedure: str | None = None
    proof: str | None = None
    boundary: str | None = None
    status: str = "current"
    is_primary: bool = False
    source_codes: list[str] = field(default_factory=list)

    def as_payload(self) -> dict:
        return {
            "name": self.name,
            "audience": self.audience,
            "trigger_situation": self.trigger_situation,
            "promise": self.promise,
            "format": self.format,
            "price": self.price,
            "procedure": self.procedure,
            "proof": self.proof,
            "boundary": self.boundary,
            "status": self.status,
            "is_primary": self.is_primary,
            "source_codes": self.source_codes,
        }


def _first_match(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if not match:
        return None
    return " ".join(match.group(0).split())


def _audience(text: str) -> str:
    for pattern, label in AUDIENCE_MARKERS:
        if pattern.search(text):
            return label
    return "не определена"


def _document_text(propositions: list[Proposition]) -> str:
    return "\n".join(item.text for item in propositions)


def extract_products(session: Session, scan: Scan, limit: int = 10) -> list[ProductDraft]:
    registry = registry_map(session, scan)
    grouped = propositions_by_source(session, scan.id)
    documents = list(session.scalars(
        select(Document)
        .join(Source, Document.source_id == Source.id)
        .where(Source.scan_id == scan.id)
        .order_by(Document.created_at, Document.id)
    ))
    drafts: list[ProductDraft] = []
    counts: list[tuple[uuid.UUID, int]] = []
    for document in documents:
        counts.append((document.id, len(grouped.get(document.source_id, []))))
    primary_id = max(counts, key=lambda item: item[1])[0] if counts else None

    for document in documents:
        if len(drafts) >= limit:
            break
        entry = registry.get(document.source_id)
        propositions = grouped.get(document.source_id, [])
        if not propositions or entry is None:
            continue
        text = _document_text(propositions)
        first = " ".join(propositions[0].text.split())
        drafts.append(ProductDraft(
            name=" ".join(document.title.split())[:300],
            audience=_audience(text),
            trigger_situation=first[:280] if len(first) > 40 else None,
            promise=first[:280],
            format=_first_match(DURATION_RE, text),
            price=_first_match(PRICE_RE, text),
            procedure=None,
            proof=_first_match(PROOF_RE, text),
            boundary=_first_match(BOUNDARY_RE, text),
            status="historical" if entry.currency == "historical" else "current",
            is_primary=document.id == primary_id,
            source_codes=[entry.code],
        ))
    return drafts
