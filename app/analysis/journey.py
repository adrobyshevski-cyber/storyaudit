"""Narrative Customer Journey: a modelled path, never a behavioural funnel.

Every step is marked `is_observed=False`. `experience` and `referral` stay
"нет данных" in a Desk audit because they cannot be observed from the site.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.registers.models import EvidenceRecord, JourneyStep, Product

STEP_QUESTIONS = {
    "trigger": "«Это моя ситуация?»",
    "interpretation": "«Как объяснить проблему?»",
    "search": "«Где искать помощь?»",
    "comparison": "«Чем это отличается?»",
    "trust": "«Почему можно доверять?»",
    "decision": "«Как записаться, что получу?»",
    "experience": "«Соответствовало ли обещанию?»",
    "referral": "«Как я объясню это другому?»",
}
STEP_ORDER = tuple(STEP_QUESTIONS)
CTA_MARKERS = ("запис", "заявк", "связаться", "консультац", "напишите", "telegram", "whatsapp")
PROOF_MARKERS = ("лет", "стаж", "сертифик", "образовани", "кейс", "отзыв", "практик")


@dataclass
class JourneyDraft:
    step: str
    public_state: str
    obstacle: str | None = None
    check_method: str | None = None
    source_codes: list[str] = field(default_factory=list)
    is_observed: bool = False

    def as_payload(self) -> dict:
        return {
            "step": self.step,
            "public_state": self.public_state,
            "obstacle": self.obstacle,
            "check_method": self.check_method,
            "source_codes": self.source_codes,
            "is_observed": self.is_observed,
        }


def _find(evidence: list[EvidenceRecord], *markers: str) -> EvidenceRecord | None:
    for record in evidence:
        lowered = record.claim.lower()
        if any(marker in lowered for marker in markers):
            return record
    return None


def build_journey_draft(
    products: list[Product],
    evidence: list[EvidenceRecord],
    existing: list[JourneyStep] | None = None,
) -> list[JourneyDraft]:
    if existing:
        return [
            JourneyDraft(
                step=step.step,
                public_state=step.public_state,
                obstacle=step.obstacle,
                check_method=step.check_method,
                source_codes=[str(code) for code in (step.source_codes or [])],
                is_observed=step.is_observed,
            )
            for step in sorted(existing, key=lambda item: item.ordinal)
        ]
    primary = next((product for product in products if product.is_primary), None) or (
        products[0] if products else None
    )
    source_codes = [str(code) for code in (primary.source_codes or [])] if primary else []
    trigger = _find(evidence, *("тупик", "теря", "не ", "сложн", "проблем"))
    trust = _find(evidence, *PROOF_MARKERS)
    decision = _find(evidence, *CTA_MARKERS)
    promise = (primary.promise if primary else None) or (evidence[0].claim if evidence else None)

    drafts = [
        JourneyDraft(
            step="trigger",
            public_state=(primary.trigger_situation if primary and primary.trigger_situation else
                          (trigger.claim if trigger else "не исследовано")),
            obstacle=None,
            check_method="Интервью о ситуациях первых обращений",
            source_codes=source_codes if (primary and primary.trigger_situation) else (
                [trigger.source_codes[0]] if trigger and trigger.source_codes else []
            ),
        ),
        JourneyDraft(
            step="interpretation",
            public_state=promise or "не исследовано",
            check_method="Проверка понимания проблемы аудиторией",
            source_codes=source_codes if promise else [],
        ),
        JourneyDraft(
            step="search",
            public_state=primary.name if primary else "не исследовано",
            check_method="Анализ поисковых запросов и рефералов",
            source_codes=source_codes if primary else [],
        ),
        JourneyDraft(
            step="comparison",
            public_state="не исследовано: конкурентная выборка не построена",
            check_method="Сравнительная карта альтернатив",
            source_codes=[],
        ),
        JourneyDraft(
            step="trust",
            public_state=(trust.claim if trust else "не исследовано: proof points не найдены"),
            check_method="Проверка доказательств и их независимости",
            source_codes=[trust.source_codes[0]] if trust and trust.source_codes else [],
        ),
        JourneyDraft(
            step="decision",
            public_state=(decision.claim if decision else "не исследовано: путь записи не описан"),
            check_method="Тест пути записи на разных устройствах",
            source_codes=[decision.source_codes[0]] if decision and decision.source_codes else [],
        ),
        JourneyDraft(
            step="experience",
            public_state="нет данных",
            obstacle="Наблюдение опыта невозможно без интервью и внутренних данных",
            check_method="Обезличенные интервью после встречи",
            source_codes=[],
        ),
        JourneyDraft(
            step="referral",
            public_state="нет данных",
            obstacle="Каналы и причины рекомендаций не наблюдались",
            check_method="Интервью, обезличенный учёт источников",
            source_codes=[],
        ),
    ]
    return drafts


def persist_journey(session, scan, drafts: list[JourneyDraft]) -> list[str]:
    steps: list[str] = []
    for ordinal, draft in enumerate(drafts, start=1):
        session.add(JourneyStep(
            scan_id=scan.id,
            ordinal=ordinal,
            step=draft.step,
            client_question=STEP_QUESTIONS[draft.step],
            public_state=draft.public_state,
            obstacle=draft.obstacle,
            source_codes=draft.source_codes,
            check_method=draft.check_method,
            is_observed=False if scan.audit_mode == "desk" else draft.is_observed,
        ))
        steps.append(draft.step)
    session.flush()
    return steps
