"""Findings G / M / W.

The classifier is deliberately conservative:

- `G` (contradiction) needs two incompatible observable sides; prices on two
  different service pages are not a contradiction.
- `M` (missing link) describes a missing piece of understanding.
- `W` (weak evidence) requires a significant claim with irrelevant or weak
  support.

Absence of data never becomes a finding; it stays `No data` in the registers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.consistency import CellDraft
from app.analysis.landscape import overlap_with_competitors
from app.registers.models import (
    ActantRole,
    ConsistencyCell,
    EvidenceRecord,
    FindingV2,
    JourneyStep,
    LandscapeEntry,
    Product,
)

PRICE_RE = re.compile(r"(\d[\d\s\u00a0]{2,9})\s*(?:₽|руб(?:\.|лей|ля)?|р\.)", re.IGNORECASE)
PROOF_MARKERS = ("лет", "стаж", "сертифик", "образовани", "кейс", "отзыв")
KIND_PREFIX = {"gap": "G", "missing_link": "M", "weak_evidence": "W"}
DEFAULT_ALTERNATIVE = (
    "Альтернативное объяснение: сведения могут передаваться лично или не попадать "
    "в исследованный публичный корпус"
)


@dataclass
class FindingDraft:
    kind: str
    statement: str
    basis_codes: list[str] = field(default_factory=list)
    consequence: str | None = None
    alternative_explanation: str = DEFAULT_ALTERNATIVE
    verification: str | None = None
    priority: str = "P2"
    confidence: str = "medium"

    def as_payload(self) -> dict:
        return {
            "kind": self.kind,
            "statement": self.statement,
            "basis_codes": self.basis_codes,
            "consequence": self.consequence,
            "alternative_explanation": self.alternative_explanation,
            "verification": self.verification,
            "priority": self.priority,
            "confidence": self.confidence,
        }


def _prices(value: str | None) -> set[str]:
    if not value:
        return set()
    return {" ".join(match.group(0).split()) for match in PRICE_RE.finditer(value)}


def _price_value(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"(\d[\d\s ]{2,})", text)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


def draft_findings(
    products: list[Product],
    evidence: list[EvidenceRecord],
    actant_roles: list[ActantRole],
    consistency: list[ConsistencyCell] | list[CellDraft],
    journey: list[JourneyStep],
    landscape: list[LandscapeEntry] | None = None,
) -> list[FindingDraft]:
    drafts: list[FindingDraft] = []
    product_codes = [product.code for product in products]
    evidence_codes = [record.code for record in evidence]

    if products and all(not product.boundary for product in products):
        drafts.append(FindingDraft(
            kind="missing_link",
            statement=(
                "Не сформулированы границы результата: что услуга не гарантирует "
                "и где заканчивается"
            ),
            basis_codes=[*product_codes[:2], *evidence_codes[:1]],
            consequence="Клиент может ожидать больше, чем услуга предполагает",
            verification="Проверить с экспертом и добавить границы в описание услуги",
            priority="P1",
        ))

    unknown_audiences = [
        product for product in products if product.audience in {"не определена", "unknown"}
    ]
    if products and len(unknown_audiences) == len(products):
        drafts.append(FindingDraft(
            kind="missing_link",
            statement="Аудитория услуги не установлена: в исследованном корпусе нет явного адресата",
            basis_codes=[*product_codes[:2], *evidence_codes[:1]],
            consequence="Неясно, кто узнаёт себя в предложении и кому оно адресовано",
            verification="Интервью и анализ обращений; не выводить аудиторию из догадок",
            priority="P1",
        ))

    if len(products) >= 2 and all(not product.procedure for product in products):
        drafts.append(FindingDraft(
            kind="missing_link",
            statement="Различия форматов не описаны: процедура и результат услуг не разведены",
            basis_codes=[*product_codes[:2], *evidence_codes[:1]],
            consequence="Выбор между форматами остаётся неопределённым",
            verification="Описать процесс и результат каждого формата, проверить на клиентах",
            priority="P2",
        ))

    proofs = [
        record
        for record in evidence
        if any(marker in record.claim.lower() for marker in PROOF_MARKERS)
    ]
    priced = [product for product in products if product.price]
    if priced and not proofs and evidence_codes:
        drafts.append(FindingDraft(
            kind="weak_evidence",
            statement="Публичная цена не сопровождается сопоставимыми proof points",
            basis_codes=[priced[0].code, evidence_codes[0]],
            consequence="Ценовое возражение остаётся без ответа в публичном корпусе",
            verification="Собрать проверяемые доказательства или обосновать цену иначе",
            priority="P1",
        ))

    sender = next((role for role in actant_roles if role.role == "sender"), None)
    if sender is not None and sender.status == "U" and evidence_codes:
        drafts.append(FindingDraft(
            kind="weak_evidence",
            statement=(
                "Триггер обращения не подтверждён: непонятно, какое событие запускает "
                "поиск помощи"
            ),
            basis_codes=evidence_codes[:2],
            consequence="Первый экран может не совпадать с реальной ситуацией обращения",
            verification="Интервью о моменте обращения; не подменять гипотезой",
            priority="P2",
        ))

    decision_steps = [step for step in journey if step.step == "decision"]
    if decision_steps and decision_steps[0].public_state.startswith("не исследовано") and product_codes:
        drafts.append(FindingDraft(
            kind="missing_link",
            statement="Путь записи не описан в исследованном корпусе",
            basis_codes=product_codes[:1],
            consequence="Клиент не понимает, что произойдёт после обращения",
            verification="Проверить фактический маршрут записи и описать его",
            priority="P2",
        ))

    conflicts = [cell for cell in consistency if cell.status == "conflict"]
    if conflicts and len(evidence_codes) >= 2:
        drafts.append(FindingDraft(
            kind="gap",
            statement="Источники сообщают несовместимые сведения об одном и том же обещании",
            basis_codes=evidence_codes[:2],
            consequence="Пользователь получает противоречивую картину",
            verification="Сверить актуальные страницы и подтвердить расхождение",
            priority="P1",
        ))

    entries = list(landscape or [])
    primary = next((product for product in products if product.is_primary), products[0] if products else None)
    if entries and primary and primary.promise:
        ratio, shared, closest = overlap_with_competitors(primary.promise, entries)
        if closest is not None and ratio >= 0.3:
            own_price = _price_value(primary.price)
            other_price = _price_value(closest.price)
            premium = (
                own_price is not None and other_price is not None and own_price > other_price
            )
            drafts.append(FindingDraft(
                kind="weak_evidence",
                statement=(
                    "Обещание пересекается с формулировками альтернатив; "
                    "уникальность в изученной выборке не доказана"
                ),
                basis_codes=[primary.code, closest.code],
                consequence=(
                    "Отличие от альтернатив не считывается, а при более высокой цене "
                    "возражение усиливается"
                ),
                verification=(
                    "Сравнить формулировки с выборкой и проверить отличия на интервью; "
                    "не объявлять территорию свободной"
                ),
                priority="P1" if premium else "P2",
            ))

    # A real contradiction is two recorded prices for the same offer, not two
    # different offers with different prices.
    by_name: dict[str, list[Product]] = {}
    for product in products:
        by_name.setdefault(" ".join(product.name.lower().split()), []).append(product)
    for same_name in by_name.values():
        if len(same_name) < 2:
            continue
        distinct: dict[str, Product] = {}
        for product in same_name:
            for price in _prices(product.price):
                distinct.setdefault(price, product)
        if len(distinct) >= 2:
            drafts.append(FindingDraft(
                kind="gap",
                statement="Одна и та же услуга опубликована с разными ценами",
                basis_codes=[product.code for product in list(distinct.values())[:2]],
                consequence="Пользователь не понимает актуальную стоимость",
                verification="Сверить живые страницы и зафиксировать единый прайс",
                priority="P1",
            ))
            break
    return drafts


def persist_findings(session: Session, scan_id, drafts: list[FindingDraft]) -> list[str]:
    from app.registers.codes import allocate_code

    existing = {
        " ".join(statement.lower().split())
        for statement in session.scalars(
            select(FindingV2.statement).where(FindingV2.scan_id == scan_id)
        )
    }
    codes: list[str] = []
    for draft in drafts:
        normalized = " ".join(draft.statement.lower().split())
        if normalized in existing:
            continue
        existing.add(normalized)
        finding = FindingV2(
            scan_id=scan_id,
            code=allocate_code(session, scan_id, KIND_PREFIX[draft.kind]),
            kind=draft.kind,
            statement=draft.statement,
            basis_codes=draft.basis_codes,
            consequence=draft.consequence,
            alternative_explanation=draft.alternative_explanation,
            verification=draft.verification,
            priority=draft.priority,
            confidence=draft.confidence,
        )
        session.add(finding)
        codes.append(finding.code)
    session.flush()
    return codes
