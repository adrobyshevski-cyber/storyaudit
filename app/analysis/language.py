"""Language map: brand vocabulary observed in the corpus.

Without interviews the lived language of the client is unknown, so it stays
empty and `match_status` is `no_data` — the map must not fill that gap with an
analyst's invented phrasing.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Block, Document, Proposition, Scan, Source
from app.registers.codes import allocate_code
from app.registers.models import LanguageCode, SourceRegistryEntry

BRAND_MARKERS = ("мы ", "наш", "наша", "наши", "помога", "созда", "работа", "подход", "метод")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


@dataclass
class LanguageDraft:
    topic: str
    brand: str
    professional: str | None
    lived: str | None
    match_status: str
    is_illustration: bool
    source_codes: list[str]

    def as_payload(self) -> dict:
        return {
            "topic": self.topic,
            "brand": self.brand,
            "professional": self.professional,
            "lived": self.lived,
            "match_status": self.match_status,
            "is_illustration": self.is_illustration,
            "source_codes": self.source_codes,
        }


def _topic(sentence: str) -> str:
    words = sentence.split()
    return " ".join(words[:8])[:300]


def extract_language(
    session: Session,
    scan: Scan,
    registry: dict[uuid.UUID, SourceRegistryEntry],
    limit: int = 6,
) -> list[LanguageDraft]:
    rows = session.execute(
        select(Source.id, Proposition.text)
        .join(Document, Document.source_id == Source.id)
        .join(Block, Block.document_id == Document.id)
        .join(Proposition, Proposition.block_id == Block.id)
        .where(Source.scan_id == scan.id)
        .order_by(Proposition.id)
    ).all()
    drafts: list[LanguageDraft] = []
    seen: set[str] = set()
    for source_id, text in rows:
        if len(drafts) >= limit:
            break
        lowered = text.lower()
        if not any(marker in lowered for marker in BRAND_MARKERS):
            continue
        sentence = " ".join(SENTENCE_SPLIT.split(text)[0].split())
        if len(sentence) < 30:
            continue
        entry = registry.get(source_id)
        if entry is None:
            continue
        topic = _topic(sentence)
        if topic.lower() in seen:
            continue
        seen.add(topic.lower())
        drafts.append(LanguageDraft(
            topic=topic,
            brand=sentence[:1000],
            professional=None,
            lived=None,
            match_status="no_data",
            is_illustration=False,
            source_codes=[entry.code],
        ))
    return drafts


def persist_language(session: Session, scan: Scan, drafts: list[LanguageDraft]) -> list[str]:
    if session.scalar(select(LanguageCode.id).where(LanguageCode.scan_id == scan.id).limit(1)):
        return []
    codes: list[str] = []
    for draft in drafts:
        record = LanguageCode(
            scan_id=scan.id,
            code=allocate_code(session, scan.id, "L"),
            topic=draft.topic,
            brand=draft.brand,
            professional=draft.professional,
            lived=draft.lived,
            match_status=draft.match_status,
            is_illustration=draft.is_illustration,
            source_codes=draft.source_codes,
        )
        session.add(record)
        codes.append(record.code)
    session.flush()
    return codes
