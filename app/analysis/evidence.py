"""Evidence Auditor: claim selection and E-register persistence.

An evidence record never claims more than the publication itself proves:
`verification` is always `publication_only`, and `allowed_conclusion` is phrased
as "the brand publicly states ...".
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Proposition, Scan
from app.registers.codes import allocate_code
from app.registers.models import EvidenceRecord, SourceRegistryEntry, Statement, TraceLink

# Weighted signals that a sentence is a claim with proof potential rather than a
# slogan. Figures, results, clients and credentials rank high; navigation and
# legal boilerplate is penalised below.
PROOF_SIGNALS: tuple[tuple[str, float], ...] = (
    ("₽", 3), ("руб", 3), ("$", 3), ("€", 3), ("%", 3), ("тыс", 2), ("млн", 2),
    ("экономи", 3), ("результат", 2), ("кейс", 2), ("проект", 1), ("внедр", 2),
    ("сократ", 2), ("сократил", 2), ("увелич", 2), ("снизил", 2), ("ускор", 2),
    ("клиент", 1), ("бренд", 1), ("партн", 1), ("дочерн", 1),
    ("лет", 1), ("год", 1), ("опыт", 1), ("стаж", 1),
    ("сертифик", 2), ("рейтинг", 2), ("награ", 2), ("преми", 1),
    ("консульт", 1), ("практик", 1), ("метод", 1), ("подход", 1),
    ("обучен", 1), ("терапи", 1), ("супервиз", 1), ("процесс", 1),
)

# Generic claim language: a slogan is still a claim, and a claim without proof is
# exactly what a W finding is built on, so these must not be filtered out.
CLAIM_SIGNALS: tuple[str, ...] = (
    "мы ", "наш", "является", "созда", "помога", "предлага", "работ", "команд",
    "культур", "инновац", "обеспеч", "гарант", "качеств", "ценност", "миссия",
    "подход", "сервис", "услуг",
)

# Navigation, legal and promotional text that is never evidence.
NOISE_SIGNALS: tuple[str, ...] = (
    "ваканси", "cookie", "куки", "©", "all right", "все права защищены",
    "политик", "контакт", "новост", "подпис", "меню", "читать далее",
    "карта сайта", "личный кабинет", "регистрац", "поиск по сайту",
    "оставить заявку", "перезвон", "карьер", "конференц", "вебинар",
    "подкаст", "мероприяти",
)

MEASURE_RE = re.compile(r"\d[\d\s.,]*\s*(?:%|₽|руб|\$|€|тыс|млн|млрд|трлн|лет|года|год)")
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
TOKEN_RE = re.compile(r"[a-zа-яё0-9]+")
# A claim shorter than this is a label, not a statement.
MIN_CLAIM_LENGTH = 20
# Below this a proposition carries neither proof nor claim language (length alone
# gives at most 0.4), so it is a heading or a fragment.
MIN_CLAIM_SCORE = 0.5
SIMILARITY_THRESHOLD = 0.8
ABBREVIATIONS = {
    "ии": "искусственный интеллект",
    "ai": "искусственный интеллект",
    "мо": "машинное обучение",
    "ml": "машинное обучение",
}
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
    """How much a proposition looks like evidence worth registering.

    Deterministic and explainable: figures, results, clients and credentials raise
    the score, navigation/legal boilerplate lowers it. The previous marker-only
    score ranked a "no suitable vacancy?" block above a case study with a $400k
    result, so the strongest proof disappeared from the register.
    """
    lowered = text.lower()
    score = sum(weight for marker, weight in PROOF_SIGNALS if marker in lowered)
    score += 0.5 * sum(1 for marker in CLAIM_SIGNALS if marker in lowered)
    score -= 4.0 * sum(1 for marker in NOISE_SIGNALS if marker in lowered)
    if MEASURE_RE.search(lowered):
        score += 3
    if YEAR_RE.search(lowered):
        score += 1
    score += min(len(text), 400) / 1000
    return round(score, 3)


def _tokens(text: str) -> set[str]:
    """Token set used for near-duplicate detection (abbreviations expanded)."""
    tokens: set[str] = set()
    for token in TOKEN_RE.findall(text.lower()):
        tokens.update(ABBREVIATIONS.get(token, token).split())
    return tokens


def _is_duplicate(tokens: set[str], selected: list[set[str]]) -> bool:
    if len(tokens) < 4:
        return False
    for other in selected:
        union = tokens | other
        if union and len(tokens & other) / len(union) >= SIMILARITY_THRESHOLD:
            return True
    return False


def per_source_limit(settings, *, role: str) -> int:
    """Budget share of one source: the client gets the larger part by default.

    Client claims drive §8 and the findings; competitor claims mostly feed the §4
    landscape, which has its own C-register.
    """
    if role == "competitor":
        return max(0, settings.report_max_statements_per_competitor)
    return max(0, settings.report_max_statements_per_source)


def select_claims(
    propositions: list[Proposition],
    *,
    limit: int,
    source_code: str,
) -> list[ClaimDraft]:
    """Pick the most evidence-like propositions, de-duplicated.

    Ranking is by ``_score``; near-duplicates (menus repeated on several pages,
    "AI" vs "искусственный интеллект") are merged so one meaning does not take two
    slots of the statement budget.
    """
    ranked = sorted(propositions, key=lambda item: (-_score(item.text), item.text))
    claims: list[ClaimDraft] = []
    seen: set[str] = set()
    selected_tokens: list[set[str]] = []
    for proposition in ranked:
        if len(claims) >= limit:
            break
        text = " ".join(proposition.text.split())
        if len(text) < MIN_CLAIM_LENGTH:
            continue
        if _score(text) < MIN_CLAIM_SCORE:
            continue
        normalized = text.lower()
        if normalized in seen:
            continue
        tokens = _tokens(text)
        if _is_duplicate(tokens, selected_tokens):
            continue
        seen.add(normalized)
        selected_tokens.append(tokens)
        hits = sum(1 for marker, _ in PROOF_SIGNALS if marker in normalized)
        claims.append(ClaimDraft(
            statement=text[:MAX_CLAIM_LENGTH],
            relevance="high" if hits >= 3 else "medium",
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
