"""Evidence Auditor: claim selection and E-register persistence.

An evidence record never claims more than the publication itself proves:
`verification` is always `publication_only`, and `allowed_conclusion` is phrased
as "the brand publicly states ...".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Proposition, Scan
from app.registers.codes import allocate_code
from app.registers.models import EvidenceRecord, SourceRegistryEntry, Statement, TraceLink

CLAIM_MARKERS = (
    "₽", "руб", "лет", "стаж", "гарант", "сертифик", "%", "клиент",
    "помога", "созда", "является", "метод", "подход", "практик", "опыт",
    "консульт", "терапи", "обучен", "супервиз",
)
MAX_CLAIM_LENGTH = 1000


@dataclass
class ClaimDraft:
    statement: str
    relevance: str = "medium"
    confidence: str = "medium"
    source_codes: list[str] = field(default_factory=list)
    proposition_ids: list[uuid.UUID] = field(default_factory=list)

    def as_payload(self) -> dict:
        return {
            "statement": self.statement,
            "relevance": self.relevance,
            "confidence": self.confidence,
            "source_codes": self.source_codes,
        }


def _score(text: str) -> float:
    lowered = text.lower()
    hits = sum(1 for marker in CLAIM_MARKERS if marker in lowered)
    return hits + min(len(text), 400) / 400


def select_claims(
    propositions: list[Proposition],
    *,
    limit: int,
    source_code: str,
) -> list[ClaimDraft]:
    ranked = sorted(propositions, key=lambda item: (-_score(item.text), item.text))
    claims: list[ClaimDraft] = []
    seen: set[str] = set()
    for proposition in ranked:
        if len(claims) >= limit:
            break
        text = " ".join(proposition.text.split())
        if len(text) < 20:
            continue
        normalized = text.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        hits = sum(1 for marker in CLAIM_MARKERS if marker in normalized)
        claims.append(ClaimDraft(
            statement=text[:MAX_CLAIM_LENGTH],
            relevance="high" if hits >= 2 else "medium",
            confidence="medium",
            source_codes=[source_code],
            proposition_ids=[proposition.id],
        ))
    return claims


def existing_claim_texts(session: Session, scan_id: uuid.UUID) -> set[str]:
    return {
        " ".join(item.lower().split())
        for item in session.scalars(select(Statement.text).where(Statement.scan_id == scan_id))
    }


def persist_claims(
    session: Session,
    scan: Scan,
    entry: SourceRegistryEntry,
    claims: list[ClaimDraft],
) -> dict[str, list[str]]:
    existing = existing_claim_texts(session, scan.id)
    statement_codes: list[str] = []
    evidence_codes: list[str] = []
    for claim in claims:
        normalized = " ".join(claim.statement.lower().split())
        if normalized in existing:
            continue
        existing.add(normalized)
        statement = Statement(
            scan_id=scan.id,
            code=allocate_code(session, scan.id, "F"),
            kind="F",
            sub_status="current" if entry.currency in ("current", "unknown") else entry.currency,
            layer="declared",
            text=claim.statement,
            source_codes=claim.source_codes,
            confidence=claim.confidence,
        )
        session.add(statement)
        session.flush()
        evidence = EvidenceRecord(
            scan_id=scan.id,
            code=allocate_code(session, scan.id, "E"),
            statement_id=statement.id,
            claim=claim.statement,
            source_codes=claim.source_codes,
            provenance="review" if entry.role == "review" else "primary",
            verification="publication_only",
            relevance=claim.relevance,
            freshness="current" if entry.currency in ("current", "unknown") else entry.currency,
            limitations=[
                *(str(item) for item in (entry.limitations or [])),
                "Независимая проверка утверждения не выполнялась",
            ],
            allowed_conclusion=f"Бренд публично заявляет: «{claim.statement[:300]}»",
            confidence=claim.confidence,
        )
        session.add(evidence)
        session.flush()
        session.add(TraceLink(
            scan_id=scan.id, from_type="evidence", from_id=evidence.id,
            to_type="statement", to_id=statement.id, relation="derived_from",
        ))
        session.add(TraceLink(
            scan_id=scan.id, from_type="evidence", from_id=evidence.id,
            to_type="source_registry", to_id=entry.id, relation="derived_from",
        ))
        statement_codes.append(statement.code)
        evidence_codes.append(evidence.code)
    session.flush()
    return {"statements": statement_codes, "evidence": evidence_codes}
