"""Greimas actant reconstruction and the three-layer claim per product.

Statuses are the point of this module: `F` only for a direct publication,
`I` for an inference drawn from it, `H` for a hypothesis to test, `U` when the
corpus simply does not say. Emotional/transformational Object levels are almost
always `U` or `H` in a Desk audit — that is the correct outcome, not a gap to
paper over.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.registers.models import EvidenceRecord, Product

UNAVAILABLE = {
    "status": "unavailable",
    "reason": "Desk Research: нет интервью и внутренних данных",
}

PROBLEM_MARKERS = ("теря", "не видит", "не понима", "тупик", "сложн", "застрева", "проблем")


@dataclass
class RoleDraft:
    role: str
    reconstruction: str
    status: str
    object_level: str | None = None
    alternative: str | None = None
    source_codes: list[str] = field(default_factory=list)
    evidence_codes: list[str] = field(default_factory=list)

    def as_payload(self) -> dict:
        return {
            "role": self.role,
            "object_level": self.object_level,
            "reconstruction": self.reconstruction,
            "status": self.status,
            "alternative": self.alternative,
            "source_codes": self.source_codes,
            "evidence_codes": self.evidence_codes,
        }


@dataclass
class ActantDraft:
    narrative_arc: str
    roles: list[RoleDraft]
    layer_promise: str
    layer_declared: dict
    layer_unknown_note: str

    def payload(self) -> dict:
        return {"narrative_arc": self.narrative_arc, "roles": [role.as_payload() for role in self.roles]}


def _top_evidence(evidence: list[EvidenceRecord], limit: int = 3) -> list[EvidenceRecord]:
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(evidence, key=lambda item: (order.get(item.relevance, 3), item.code))[:limit]


def build_actant_draft(product: Product, evidence: list[EvidenceRecord]) -> ActantDraft:
    codes = [str(code) for code in (product.source_codes or [])]
    top = _top_evidence(evidence)
    evidence_codes = [item.code for item in top]
    promise = (product.promise or "").strip()
    audience = (product.audience or "").strip()
    audience_known = bool(audience) and audience not in {"не определена", "unknown"}
    trigger = (product.trigger_situation or "").strip()
    has_problem = bool(trigger) and any(marker in trigger.lower() for marker in PROBLEM_MARKERS)

    roles: list[RoleDraft] = [
        RoleDraft(
            role="subject",
            reconstruction=audience if audience_known else "Действующий субъект не установлен",
            status="I" if audience_known else "U",
            source_codes=codes,
            alternative="Субъект может быть шире заявленной аудитории; требует проверки",
        ),
        RoleDraft(
            role="object",
            object_level="functional",
            reconstruction=promise[:500] if promise else "Функциональный результат не сформулирован",
            status="I" if promise else "U",
            source_codes=codes,
            evidence_codes=evidence_codes if promise else [],
            alternative="Формулировка может быть маркетинговой, а не операционной целью",
        ),
        RoleDraft(
            role="object",
            object_level="emotional",
            reconstruction="Не установлено: эмоциональный результат требует интервью с клиентами",
            status="U",
            source_codes=codes,
            alternative="Эмоциональный уровень может быть не выражен в публичном тексте",
        ),
        RoleDraft(
            role="object",
            object_level="transformational",
            reconstruction="Не установлено: трансформационный результат не проверялся",
            status="U",
            source_codes=codes,
            alternative="Возможная гипотеза требует проверки и не должна подаваться как обещание",
        ),
        RoleDraft(
            role="sender",
            reconstruction=trigger[:500] if trigger else "Триггер обращения не установлен",
            status="I" if trigger else "U",
            source_codes=codes,
            evidence_codes=evidence_codes if trigger else [],
            alternative="Реальный триггер обращения может отличаться от описанного на сайте",
        ),
        RoleDraft(
            role="receiver",
            reconstruction=f"{audience} (прямой получатель)" if audience_known else "Получатель не установлен",
            status="I" if audience_known else "U",
            source_codes=codes,
            alternative="Косвенный получатель пользы не установлен",
        ),
        RoleDraft(
            role="helper",
            reconstruction=(
                f"{product.name}: {promise[:300]}" if promise else product.name
            ),
            status="I" if promise else "U",
            source_codes=codes,
            evidence_codes=evidence_codes,
            alternative="Роль помощника заявлена, но механизм работы не раскрыт",
        ),
        RoleDraft(
            role="opponent",
            reconstruction=trigger[:500] if has_problem else "Препятствие не названо в исследованном корпусе",
            status="I" if has_problem else "U",
            source_codes=codes,
            evidence_codes=evidence_codes if has_problem else [],
            alternative="Препятствие может подразумеваться, но не формулироваться прямо",
        ),
    ]
    arc = (
        f"«{trigger[:160]}» → поиск решения → встреча с Helper: {product.name} → "
        "возможное новое действие → дальнейшее решение Героя"
        if trigger
        else (
            f"Герой: {audience if audience_known else 'не установлен'} → "
            f"желаемый результат: {promise[:120] or 'не сформулирован'} → "
            f"Helper: {product.name} → результат не наблюдался"
        )
    )
    return ActantDraft(
        narrative_arc=arc,
        roles=roles,
        layer_promise=promise or (top[0].claim if top else product.name),
        layer_declared={
            "text": promise or (top[0].claim if top else "не сформулировано"),
            "source_codes": codes,
            "evidence_codes": evidence_codes,
            "status": "observed",
        },
        layer_unknown_note=(
            "Experienced и Perceived недоступны в режиме Desk: нужны интервью, "
            "наблюдение процесса и данные заказчика"
        ),
    )


def distance_from_validation(roles: list[RoleDraft]) -> int:
    """How many roles still need external validation (used by the critic)."""
    return sum(1 for role in roles if role.status in {"H", "U"})
