"""Recommendations, KPI cards, roadmap, hypotheses and the Stage 02 gate.

Each recommendation is bound to a finding and to a KPI with `baseline=unknown`.
Expected effect is always a hypothesis (`H`), never a number: the DB column can
only store `H`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.registers.codes import allocate_code
from app.registers.models import (
    AuditHypothesis,
    FindingV2,
    Kpi,
    Product,
    RecommendationFinding,
    RecommendationV2,
    RoadmapPhase,
    ScorecardEntry,
    StageGate,
)

KPI_BY_MARKER = (
    ("границ", ("Доля клиентов, верно понимающих границы услуги",
                "Верно назвали, что услуга не гарантирует / все опрошенные в тесте понимания")),
    ("аудитор", ("Доля профильных обращений",
                 "Обращения из целевой аудитории / все обращения за период")),
    ("различия форматов", ("Доля верно выбранного формата",
                           "Верно сопоставили ситуацию и формат / участники теста понимания")),
    ("путь записи", ("Конверсия из посещения в обращение",
                     "Обращения / посещения страницы услуги за период")),
    ("цена", ("Причины выбора и отказа по цене",
              "Доля респондентов, назвавших цену решающим фактором")),
    ("триггер", ("Доля обращений из целевой ситуации",
                 "Обращения с описанной ситуацией / все обращения")),
    ("противореч", ("Количество конфликтующих актуальных страниц",
                    "Число страниц с противоречащими условиями после сверки")),
    ("пересека", ("Доля верно названных отличий от альтернатив",
                  "Респонденты, назвавшие отличие от альтернатив / участники теста")),
)
DEFAULT_KPI = ("Понимание предложения (тест)",
               "Верно пересказали предложение / участники теста понимания")


@dataclass
class KpiDraft:
    name: str
    definition: str
    code: str = ""
    baseline: str = "unknown"
    period: str = "месяц"
    data_source: str = "система записи / обезличенный учёт"
    owner: str = "клиент"
    target: str = "определить после baseline"
    limitations: str = "Изменение показателя не доказывает причинный эффект только нарратива"


@dataclass
class RecommendationDraft:
    change: str
    rationale: str
    finding_codes: list[str]
    kpi: KpiDraft
    expected_effect: str | None = None
    impact: str = "medium"
    evidence_confidence: str = "medium"
    urgency: str = "medium"
    effort: str = "medium"
    priority: str = "P2"
    decision_class: str = "fix"
    owner: str = "клиент / SoCode"


@dataclass
class HypothesisDraft:
    statement: str
    how_to_verify: str


@dataclass
class PlanDraft:
    recommendations: list[RecommendationDraft] = field(default_factory=list)
    roadmap: list[dict] = field(default_factory=list)
    gate: dict = field(default_factory=dict)
    hypotheses: list[HypothesisDraft] = field(default_factory=list)


def _kpi_for(statement: str) -> KpiDraft:
    lowered = statement.lower()
    for marker, (name, definition) in KPI_BY_MARKER:
        if marker in lowered:
            return KpiDraft(name=name, definition=definition)
    name, definition = DEFAULT_KPI
    return KpiDraft(name=name, definition=definition)


def _recommendation_for(finding: FindingV2) -> RecommendationDraft:
    statement = finding.statement
    lowered = statement.lower()
    kpi = _kpi_for(statement)
    if "границ" in lowered:
        return RecommendationDraft(
            change="Добавить в описание услуги блок границ: что не гарантируется, когда нужен другой формат",
            rationale=statement,
            finding_codes=[finding.code], kpi=kpi,
            expected_effect="Меньше неопределённых ожиданий после записи",
            impact="high", urgency="high", effort="low", priority="P1", decision_class="fix",
        )
    if "аудитор" in lowered:
        return RecommendationDraft(
            change="Уточнить аудиторию и первый экран под конкретную ситуацию обращения",
            rationale=statement,
            finding_codes=[finding.code], kpi=kpi,
            expected_effect="Выше доля профильных обращений",
            impact="high", urgency="high", effort="medium", priority="P1", decision_class="test",
        )
    if "различия форматов" in lowered:
        return RecommendationDraft(
            change="Развести форматы: кому подходит, что происходит, какой результат и чего не будет",
            rationale=statement,
            finding_codes=[finding.code], kpi=kpi,
            expected_effect="Клиент выбирает формат без переписки",
            impact="high", urgency="medium", effort="medium", priority="P1", decision_class="fix",
        )
    if "путь записи" in lowered:
        return RecommendationDraft(
            change="Описать маршрут записи: что происходит после обращения и что подготовить",
            rationale=statement,
            finding_codes=[finding.code], kpi=kpi,
            expected_effect="Меньше брошенных обращений",
            impact="medium", urgency="medium", effort="low", priority="P2", decision_class="fix",
        )
    if "цена" in lowered:
        return RecommendationDraft(
            change="Собрать и опубликовать проверяемые proof points, объясняющие уровень цены",
            rationale=statement,
            finding_codes=[finding.code], kpi=kpi,
            expected_effect="Ценовое возражение получает обоснованный ответ",
            impact="high", urgency="high", effort="high", priority="P1", decision_class="test",
        )
    if "триггер" in lowered:
        return RecommendationDraft(
            change="Проверить реальные триггеры обращения и переписать первый экран под них",
            rationale=statement,
            finding_codes=[finding.code], kpi=kpi,
            expected_effect="Первый экран совпадает с ситуацией клиента",
            impact="medium", urgency="medium", effort="medium", priority="P2", decision_class="test",
        )
    if "пересека" in lowered:
        return RecommendationDraft(
            change="Проверить отличия от альтернатив и сформулировать только подтверждённые",
            rationale=statement,
            finding_codes=[finding.code], kpi=kpi,
            expected_effect="Клиент понимает, чем бренд отличается, без неподтверждённых заявлений",
            impact="high", urgency="medium", effort="medium", priority="P1", decision_class="test",
        )
    if "противореч" in lowered or "разными ценами" in lowered:
        return RecommendationDraft(
            change="Сверить актуальные страницы и зафиксировать единые условия и прайс",
            rationale=statement,
            finding_codes=[finding.code], kpi=kpi,
            expected_effect="Нет противоречивых контактов с брендом",
            impact="high", urgency="high", effort="low", priority="P1", decision_class="fix",
        )
    return RecommendationDraft(
        change=f"Проверить и устранить: {statement[:300]}",
        rationale=statement,
        finding_codes=[finding.code], kpi=kpi,
    )


def build_plan(
    findings: list[FindingV2],
    products: list[Product],
    scorecard: list[ScorecardEntry],
    *,
    audit_mode: str,
    live_checked: bool,
    landscape_present: bool,
    client_brief: bool,
) -> PlanDraft:
    plan = PlanDraft()
    primary = next((product for product in products if product.is_primary), products[0] if products else None)
    plan.recommendations = [
        _recommendation_for(finding) for finding in findings
    ]
    if not plan.recommendations:
        plan.recommendations.append(RecommendationDraft(
            change="Сохранить действующее обещание и не вводить новые обещания без доказательств",
            rationale="Проверенных противоречий и пропусков не найдено в исследованном корпусе",
            finding_codes=[],
            kpi=KpiDraft(*DEFAULT_KPI),
            expected_effect="Текущая ясность сохраняется до следующей проверки",
            impact="low", urgency="low", effort="low", priority="P3", decision_class="keep",
        ))

    weak_findings = [finding for finding in findings if finding.kind == "weak_evidence"]
    if primary and primary.promise:
        plan.recommendations.insert(0, RecommendationDraft(
            change="Сохранить действующее обещание и профессиональное ядро без переписывания",
            rationale=(
                "Обещание сформулировано и не противоречит найденным доказательствам; "
                "замена без проверки создаст новый риск"
            ),
            finding_codes=[],
            kpi=KpiDraft(*DEFAULT_KPI),
            expected_effect="Текущая ясность сохраняется до следующей проверки",
            impact="low", urgency="low", effort="low", priority="P3", decision_class="keep",
        ))
    if weak_findings:
        plan.recommendations.append(RecommendationDraft(
            change="Не публиковать обещания результата и превосходства без проверяемых доказательств",
            rationale=weak_findings[0].statement,
            finding_codes=[weak_findings[0].code],
            kpi=KpiDraft(*DEFAULT_KPI),
            expected_effect="Снижается риск недоказанных обещаний",
            impact="medium", urgency="high", effort="low", priority="P1", decision_class="stop",
        ))

    plan.roadmap = [
        {"title": "0. Проверка фактов", "dependencies": "Доступ к актуальным материалам",
         "actions": "Сверить продукты, цены, ограничения и маршрут записи",
         "artifact": "Исправленная факт-база", "owner": "Клиент / SoCode",
         "done_criteria": "Нет критических конфликтов в актуальных материалах"},
        {"title": "1. Продукт и сайт", "dependencies": "Фаза 0",
         "actions": "Уточнить страницы услуг, границы и путь записи",
         "artifact": "ТЗ на изменения", "owner": "Клиент / дизайнер",
         "done_criteria": "Страницы услуг утверждены"},
        {"title": "2. Доказательства", "dependencies": "Согласия и материалы",
         "actions": "Собрать профессиональные доказательства и разрешённые примеры",
         "artifact": "Proof Library", "owner": "Клиент / SoCode",
         "done_criteria": "Релевантность доказательств проверена"},
        {"title": "3. Пилот", "dependencies": "Внедрение изменений",
         "actions": "Проверить гипотезы на аудитории и снять baseline",
         "artifact": "Test Report", "owner": "SoCode",
         "done_criteria": "Получены наблюдения и исходные показатели"},
        {"title": "4. Передача Stage 02", "dependencies": "Stage Gate",
         "actions": "Сформировать бриф стратегии и открытые вопросы",
         "artifact": "Бриф Stage 02", "owner": "SoCode",
         "done_criteria": "Решение Gate зафиксировано"},
    ]

    if primary and primary.promise:
        plan.hypotheses.append(HypothesisDraft(
            statement=f"Ценность предложения «{primary.promise[:160]}» совпадает с реальным критерием выбора клиента",
            how_to_verify="6–10 интервью о причинах обращения и выбора; сравнение с альтернативами",
        ))
    if weak_findings:
        plan.hypotheses.append(HypothesisDraft(
            statement="Публичных proof points достаточно, чтобы снять ценовое возражение без скидки",
            how_to_verify="Тест карточек доказательств на целевой аудитории; замер причин отказа по цене",
        ))
    plan.hypotheses.append(HypothesisDraft(
        statement="Первый экран точнее совпадает с триггером обращения, если описать ситуацию, а не роль",
        how_to_verify="Интервью о моменте обращения и A/B-тест формулировок первого экрана",
    ))

    products_ready = bool(products)
    audience_ready = any(
        product.audience not in {"не определена", "unknown"} for product in products
    )
    evidence_ready = bool(scorecard) and any(entry.score is not None for entry in scorecard)
    criteria = [
        {
            "criterion": "Основная услуга",
            "result": "Ready" if products_ready else "Not Ready",
            "required": "Установить основную услугу" if not products_ready else "Подтвердить приоритетность у клиента",
        },
        {
            "criterion": "Главная аудитория",
            "result": "Ready" if audience_ready else "Conditional",
            "required": "Подтвердить аудиторию интервью" if not audience_ready else "Проверить на интервью",
        },
        {
            "criterion": "Наличие доказательств",
            "result": "Conditional" if weak_findings else ("Ready" if evidence_ready else "Not Ready"),
            "required": "Закрыть слабые доказательства" if weak_findings else "Собрать доказательства",
        },
        {
            "criterion": "Цели и границы клиента",
            "result": "Ready" if client_brief else "Not established",
            "required": "Заполнить Client Brief v2.0",
        },
        {
            "criterion": "Этическая безопасность",
            "result": "Conditional",
            "required": "Согласовать профессиональные обещания и допустимость кейсов",
        },
        {
            "criterion": "Гипотезы Stage 02",
            "result": "Ready" if plan.hypotheses else "Conditional",
            "required": "Сформулировать гипотезы с планом проверки",
        },
    ]
    critical = [item for item in criteria if item["result"] in {"Not Ready", "Not established"}]
    others = [item for item in criteria if item["result"] in {"Conditional"}]
    if not products_ready:
        decision = "not_ready"
        rationale = "Основная услуга не установлена в исследованном корпусе: проектировать стратегию не на чем."
    elif critical:
        decision = "conditional_ready"
        rationale = (
            "Можно проектировать сценарии Stage 02, но окончательные решения зависят от "
            "незакрытых пунктов: " + "; ".join(item["criterion"] for item in critical) + "."
        )
    elif others:
        decision = "conditional_ready"
        rationale = (
            "Аудит достаточен для разработки гипотез, но часть критериев остаётся "
            "условной: " + "; ".join(item["criterion"] for item in others) + "."
        )
    else:
        decision = "ready"
        rationale = "Все критерии готовности выполнены на согласованных входах."

    plan.gate = {
        "decision": decision,
        "rationale": rationale + (
            " Режим Desk: восприятие и клиентский опыт не проверялись."
            if audit_mode == "desk" else ""
        ),
        "criteria": criteria,
        "blockers": [
            finding.statement for finding in findings if finding.priority == "blocker"
        ],
        "required_validations": [
            item
            for item, needed in (
                ("Интервью с клиентами и наблюдение опыта (Desk → Validated)", True),
                ("Live-проверка страниц и актуальность цен", not live_checked),
                ("Конкурентная выборка до 5 альтернатив", not landscape_present),
                ("Client Brief v2.0", not client_brief),
            )
            if needed
        ],
    }

    return plan


def persist_plan(session: Session, scan, plan: PlanDraft) -> dict[str, list[str]]:
    recommendation_codes: list[str] = []
    kpi_codes: list[str] = []
    finding_by_code = {
        finding.code: finding
        for finding in session.scalars(select(FindingV2).where(FindingV2.scan_id == scan.id))
    }
    for draft in plan.recommendations:
        kpi = Kpi(
            scan_id=scan.id,
            code=allocate_code(session, scan.id, "KPI"),
            name=draft.kpi.name,
            definition=draft.kpi.definition,
            baseline=draft.kpi.baseline,
            period=draft.kpi.period,
            data_source=draft.kpi.data_source,
            owner=draft.kpi.owner,
            target=draft.kpi.target,
            limitations=draft.kpi.limitations,
        )
        session.add(kpi)
        session.flush()
        recommendation = RecommendationV2(
            scan_id=scan.id,
            code=allocate_code(session, scan.id, "R"),
            change=draft.change,
            rationale=draft.rationale,
            expected_effect=draft.expected_effect,
            expected_effect_status="H",
            impact=draft.impact,
            evidence_confidence=draft.evidence_confidence,
            urgency=draft.urgency,
            effort=draft.effort,
            priority=draft.priority,
            decision_class=draft.decision_class,
            owner=draft.owner,
            kpi_codes=[kpi.code],
        )
        session.add(recommendation)
        session.flush()
        for code in draft.finding_codes:
            finding = finding_by_code.get(code)
            if finding is not None:
                session.add(RecommendationFinding(
                    recommendation_id=recommendation.id, finding_id=finding.id,
                ))
        recommendation_codes.append(recommendation.code)
        kpi_codes.append(kpi.code)

    for ordinal, phase in enumerate(plan.roadmap, start=1):
        session.add(RoadmapPhase(
            scan_id=scan.id,
            ordinal=ordinal,
            title=phase["title"],
            dependencies=phase.get("dependencies"),
            actions=phase.get("actions"),
            artifact=phase.get("artifact"),
            owner=phase.get("owner"),
            done_criteria=phase.get("done_criteria"),
            kpi_codes=kpi_codes,
        ))

    existing_gate = session.scalar(select(StageGate).where(StageGate.scan_id == scan.id))
    if existing_gate is None:
        session.add(StageGate(
            scan_id=scan.id,
            decision=plan.gate["decision"],
            rationale=plan.gate["rationale"],
            criteria=plan.gate["criteria"],
            blockers=plan.gate["blockers"],
            required_validations=plan.gate["required_validations"],
        ))

    hypothesis_codes: list[str] = []
    for draft in plan.hypotheses:
        code = allocate_code(session, scan.id, "H")
        session.add(AuditHypothesis(
            scan_id=scan.id,
            code=code,
            statement=draft.statement,
            how_to_verify=draft.how_to_verify,
        ))
        hypothesis_codes.append(code)
    session.flush()
    return {
        "recommendations": recommendation_codes,
        "kpis": kpi_codes,
        "hypotheses": hypothesis_codes,
    }


def plan_from_payload(payload: dict) -> PlanDraft:
    """Convert validated LLM output into a draft ready for persistence."""
    plan = PlanDraft()
    for item in payload.get("recommendations", []):
        kpi = item["kpi"]
        plan.recommendations.append(RecommendationDraft(
            change=item["change"],
            rationale=item["rationale"],
            finding_codes=[str(code) for code in item["finding_codes"]],
            kpi=KpiDraft(
                name=kpi["name"],
                definition=kpi["definition"],
                baseline=kpi.get("baseline") or "unknown",
                period=kpi.get("period"),
                data_source=kpi.get("data_source"),
                owner=kpi.get("owner"),
                target=kpi.get("target") or "определить после baseline",
                limitations=kpi.get("limitations"),
            ),
            expected_effect=item.get("expected_effect"),
            impact=item["impact"],
            evidence_confidence=item["evidence_confidence"],
            urgency=item["urgency"],
            effort=item["effort"],
            priority=item["priority"],
            decision_class=item["decision_class"],
            owner=item.get("owner"),
        ))
    plan.roadmap = list(payload.get("roadmap", []))
    plan.gate = dict(payload["gate"])
    plan.hypotheses = [
        HypothesisDraft(statement=item["statement"], how_to_verify=item["how_to_verify"])
        for item in payload.get("hypotheses", [])
    ]
    return plan
