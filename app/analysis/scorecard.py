"""Narrative Scorecard: four dimensions, 0–10 or N/A, no aggregate.

A score is only produced when the corpus can support it. Everything that needs
interviews, analytics or a competitor landscape stays `N/A` with a reason — the
methodology explicitly forbids folding N/A into zero or into an average.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.analysis.landscape import overlap_with_competitors
from app.registers.models import (
    ActantRole,
    ConsistencyCell,
    EvidenceRecord,
    FindingV2,
    LandscapeEntry,
    Product,
    ScorecardEntry,
)

PROOF_MARKERS = ("лет", "стаж", "сертифик", "образовани", "кейс", "отзыв")
PROCESS_MARKERS = ("процесс", "этап", "как проходит", "формат", "подготов")
STATUS_SCORE = {"F": 8.0, "I": 8.0, "H": 4.0}


@dataclass
class ScoreDraft:
    dimension: str
    criterion: str
    score: float | None
    rationale: str
    na_reason: str | None = None
    confidence: str | None = None
    source_codes: list[str] = field(default_factory=list)

    def as_payload(self) -> dict:
        return {
            "dimension": self.dimension,
            "criterion": self.criterion,
            "score": self.score,
            "na_reason": self.na_reason,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "source_codes": self.source_codes,
        }


def _role(roles: list[ActantRole], name: str, level: str | None = None) -> ActantRole | None:
    for role in roles:
        if role.role == name and (level is None or role.object_level == level):
            return role
    return None


def _role_score(role: ActantRole | None, unknown_reason: str) -> tuple[float | None, str | None, str]:
    if role is None:
        return None, unknown_reason, "Роль не реконструирована"
    score = STATUS_SCORE.get(role.status)
    if role.status == "U":
        return None, unknown_reason, role.reconstruction
    return score, None, role.reconstruction


def _confidence(codes: list[str]) -> str | None:
    if not codes:
        return None
    return "high" if len(codes) >= 2 else "medium"


def build_scorecard_draft(
    products: list[Product],
    evidence: list[EvidenceRecord],
    roles: list[ActantRole],
    cells: list[ConsistencyCell],
    findings: list[FindingV2],
    *,
    audit_mode: str,
    landscape: list[LandscapeEntry] | None = None,
) -> list[ScoreDraft]:
    primary = next((product for product in products if product.is_primary), products[0] if products else None)
    product_sources = [str(code) for code in (primary.source_codes or [])] if primary else []
    drafts: list[ScoreDraft] = []

    def add(dimension: str, criterion: str, role: ActantRole | None, unknown: str,
            extra_codes: list[str] | None = None) -> None:
        score, reason, text = _role_score(role, unknown)
        codes = [*((role.source_codes or []) if role else []), *((role.evidence_codes or []) if role else [])]
        codes = [str(code) for code in codes] + [code for code in (extra_codes or []) if code not in codes]
        drafts.append(ScoreDraft(
            dimension=dimension,
            criterion=criterion,
            score=score,
            na_reason=reason,
            rationale=text[:800],
            confidence=_confidence(codes) if score is not None else None,
            source_codes=codes[:5],
        ))

    add("clarity", "Hero", _role(roles, "subject"), "Аудитория не установлена в корпусе")
    add("clarity", "Opponent", _role(roles, "opponent"), "Препятствие не названо в корпусе")
    add("clarity", "Object", _role(roles, "object", "functional"), "Функциональный результат не сформулирован")
    add("clarity", "Helper", _role(roles, "helper"), "Роль эксперта не реконструирована")

    if primary and primary.promise:
        if primary.boundary and primary.procedure:
            drafts.append(ScoreDraft(
                dimension="clarity", criterion="Обещание и выбор формата",
                score=8.0, rationale="Обещание подкреплено границами и процедурой",
                confidence="high", source_codes=product_sources,
            ))
        else:
            missing = ", ".join(name for name, value in (
                ("границы", primary.boundary), ("процедура", primary.procedure)
            ) if not value)
            drafts.append(ScoreDraft(
                dimension="clarity", criterion="Обещание и выбор формата",
                score=6.0,
                rationale=f"Обещание сформулировано, но не хватает: {missing}",
                confidence="medium", source_codes=product_sources,
            ))
    else:
        drafts.append(ScoreDraft(
            dimension="clarity", criterion="Обещание и выбор формата",
            score=None, na_reason="Обещание не сформулировано",
            rationale="В корпусе нет формулировки результата",
        ))

    communication = [cell for cell in cells if cell.dimension == "communication"]
    if communication:
        if any(cell.status == "conflict" for cell in communication):
            pages_score, pages_note = 3.0, "В коммуникации найден конфликт"
        elif all(cell.status in {"confirmed", "no_data"} for cell in communication):
            pages_score, pages_note = 7.0, "Доступные страницы согласованы"
        else:
            pages_score, pages_note = 5.0, "Часть ролей раскрыта не полностью"
        drafts.append(ScoreDraft(
            dimension="consistency", criterion="Согласованность доступных страниц",
            score=pages_score, rationale=pages_note,
            confidence=_confidence(product_sources), source_codes=product_sources,
        ))
    else:
        drafts.append(ScoreDraft(
            dimension="consistency", criterion="Согласованность доступных страниц",
            score=None, na_reason="Матрица согласованности не рассчитана",
            rationale="Нет ячеек согласованности",
        ))

    service_cells = [cell for cell in cells if cell.dimension == "service"]
    if service_cells and not any(cell.status == "conflict" for cell in service_cells):
        partial = sum(1 for cell in service_cells if cell.status == "partial")
        drafts.append(ScoreDraft(
            dimension="consistency", criterion="Коммуникация ↔ услуга",
            score=7.0 if not partial else 6.0,
            rationale="Линейка услуг связана с заявленными ролями, но различия форматов неполны",
            confidence="medium", source_codes=product_sources,
        ))
    else:
        drafts.append(ScoreDraft(
            dimension="consistency", criterion="Коммуникация ↔ услуга",
            score=None, na_reason="Нет продукта или найден конфликт",
            rationale="Сопоставление невозможно",
        ))

    drafts.append(ScoreDraft(
        dimension="consistency", criterion="Коммуникация ↔ фактический опыт",
        score=None,
        na_reason="Desk Research: опыт взаимодействия не наблюдался" if audit_mode == "desk"
        else "Нет данных о результате взаимодействия",
        rationale="Без интервью и внутренних данных опыт не устанавливается",
    ))

    qualifications = [
        record for record in evidence if any(marker in record.claim.lower() for marker in PROOF_MARKERS)
    ]
    if qualifications:
        drafts.append(ScoreDraft(
            dimension="credibility", criterion="Квалификации и опыт в коммуникации",
            score=6.0,
            rationale="Квалификации заявлены, независимая проверка не выполнялась",
            confidence=_confidence([record.code for record in qualifications]),
            source_codes=[record.code for record in qualifications][:3],
        ))
    else:
        drafts.append(ScoreDraft(
            dimension="credibility", criterion="Квалификации и опыт в коммуникации",
            score=None, na_reason="В корпусе нет заявлений о квалификации",
            rationale="Отсутствие заявления не является отрицательной оценкой",
        ))

    process = [
        record for record in evidence if any(marker in record.claim.lower() for marker in PROCESS_MARKERS)
    ]
    if process:
        drafts.append(ScoreDraft(
            dimension="credibility", criterion="Доказательства профессионального процесса",
            score=5.0,
            rationale="Процесс описан фрагментарно; итог встречи не зафиксирован",
            confidence="medium", source_codes=[record.code for record in process][:3],
        ))
    else:
        drafts.append(ScoreDraft(
            dimension="credibility", criterion="Доказательства профессионального процесса",
            score=None, na_reason="Процесс работы не описан в корпусе",
            rationale="Нет релевантных доказательств процесса",
        ))

    priced = [product for product in products if product.price]
    if priced:
        proof_present = bool(qualifications or process)
        drafts.append(ScoreDraft(
            dimension="credibility", criterion="Доказательство ценностного обещания",
            score=5.0 if proof_present else 3.0,
            rationale=(
                "Цена сопровождается частичными proof points"
                if proof_present else
                "Публичная цена не сопровождается сопоставимыми proof points"
            ),
            confidence="medium", source_codes=product_sources,
        ))
    else:
        drafts.append(ScoreDraft(
            dimension="credibility", criterion="Доказательство ценностного обещания",
            score=None, na_reason="Цена не опубликована",
            rationale="Ценовое обещание не наблюдалось",
        ))

    audience_known = bool(primary and primary.audience not in {"не определена", "unknown"})
    trigger_known = bool(primary and primary.trigger_situation)
    if audience_known:
        drafts.append(ScoreDraft(
            dimension="resonance", criterion="Узнаваемость задачи по тексту",
            score=7.0 if trigger_known else 6.0,
            rationale="Ситуация обращения сформулирована; подтверждений из интервью нет",
            confidence="medium", source_codes=product_sources,
        ))
    else:
        drafts.append(ScoreDraft(
            dimension="resonance", criterion="Узнаваемость задачи по тексту",
            score=None, na_reason="Аудитория не установлена",
            rationale="Неясно, кто узнаёт себя в тексте",
        ))

    entries = list(landscape or [])
    if entries and primary and primary.promise:
        ratio, shared, closest = overlap_with_competitors(primary.promise, entries)
        if ratio >= 0.5:
            uniqueness_score = 3.0
        elif ratio >= 0.2:
            uniqueness_score = 5.0
        else:
            uniqueness_score = 7.0
        shared_note = ", ".join(shared[:5]) if shared else "общих ключевых слов не найдено"
        drafts.append(ScoreDraft(
            dimension="resonance", criterion="Относительная смысловая уникальность",
            score=uniqueness_score,
            rationale=(
                f"Сравнение с {len(entries)} альтернативами; пересечение формулировок: {shared_note}. "
                "Уникальность оценивается только в изученной выборке"
            ),
            confidence="medium",
            source_codes=(
                [closest.code, *[e.code for e in entries if e is not closest]][:3]
                if closest is not None else [e.code for e in entries][:3]
            ),
        ))
    else:
        drafts.append(ScoreDraft(
            dimension="resonance", criterion="Относительная смысловая уникальность",
            score=None, na_reason="Конкурентная выборка не построена",
            rationale="Без сравнения с альтернативами уникальность не утверждается",
        ))

    drafts.extend([
        ScoreDraft(
            dimension="resonance", criterion="Реальное узнавание у аудитории",
            score=None, na_reason="Нет интервью и опросов",
            rationale="Текстовый анализ не заменяет исследование аудитории",
        ),
        ScoreDraft(
            dimension="resonance", criterion="Органическое распространение",
            score=None, na_reason="Нет реферальных и социальных метрик",
            rationale="Распространение не наблюдалось",
        ),
    ])
    return drafts


def persist_scorecard(session: Session, scan_id, drafts: list[ScoreDraft]) -> int:
    for ordinal, draft in enumerate(drafts, start=1):
        session.add(ScorecardEntry(
            scan_id=scan_id,
            ordinal=ordinal,
            dimension=draft.dimension,
            criterion=draft.criterion,
            score=None if draft.score is None else round(draft.score, 1),
            na_reason=draft.na_reason if draft.score is None else None,
            confidence=draft.confidence,
            rationale=draft.rationale,
            source_codes=draft.source_codes,
        ))
    session.flush()
    return len(drafts)
