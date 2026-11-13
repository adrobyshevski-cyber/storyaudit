"""Independent Critic: a second pass over the findings register.

The critic does not re-classify silently. It fills alternative explanations,
marks weak findings for human review and records why. A finding is never
deleted here — rejection is a human decision (`review_events`).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.registers.models import FindingV2, SourceRegistryEntry

DEFAULT_ALTERNATIVE = (
    "Альтернативное объяснение: сведения могут передаваться лично или не попадать "
    "в исследованный публичный корпус"
)


@dataclass
class CriticDraft:
    code: str
    alternative_explanation: str
    needs_review: bool
    note: str | None = None

    def as_payload(self) -> dict:
        return {
            "code": self.code,
            "alternative_explanation": self.alternative_explanation,
            "needs_review": self.needs_review,
            "note": self.note,
        }


def build_critic_draft(
    findings: list[FindingV2],
    registry: list[SourceRegistryEntry],
) -> list[CriticDraft]:
    historical = {
        str(code)
        for entry in registry
        if entry.currency in {"historical", "disputed", "unknown"}
        for code in [entry.code]
    }
    drafts: list[CriticDraft] = []
    for finding in findings:
        basis = [str(code) for code in (finding.basis_codes or [])]
        touches_historical = bool(set(basis) & historical)
        weak = finding.kind == "weak_evidence" or finding.confidence == "low"
        reason = None
        if touches_historical:
            reason = "Основание опирается на исторический или спорный источник"
        elif finding.kind == "weak_evidence":
            reason = "Слабое доказательство требует проверки, а не удаления"
        elif finding.kind == "gap" and len(set(basis)) < 2:
            reason = "Для противоречия указана только одна сторона"
        drafts.append(CriticDraft(
            code=finding.code,
            alternative_explanation=(
                (finding.alternative_explanation or "").strip() or DEFAULT_ALTERNATIVE
            ),
            needs_review=bool(weak or touches_historical),
            note=reason,
        ))
    return drafts


def apply_critic_draft(session: Session, findings: list[FindingV2], drafts: list[CriticDraft]) -> None:
    by_code = {finding.code: finding for finding in findings}
    for draft in drafts:
        finding = by_code.get(draft.code)
        if finding is None:
            continue
        finding.alternative_explanation = draft.alternative_explanation
        if draft.needs_review:
            finding.needs_review = True
    session.flush()
