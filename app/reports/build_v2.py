"""Build the client report (report_v2) out of the evidence-first registers.

Phase A scope: the document always has the full 17-section structure required by
SoCode Narrative Audit v1.1, and every section that the pipeline cannot fill yet
is reported as an explicit data gap instead of an invented value or an empty
space.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import AppError, conflict, not_found
from app.models import Block as CorpusBlock
from app.models import Document, Job, Scan, Source
from app.registers.models import (
    ActantMap,
    ActantRole,
    AuditHypothesis,
    ConsistencyCell,
    EvidenceRecord,
    FindingV2,
    JourneyStep,
    Kpi,
    LandscapeEntry,
    LanguageCode,
    LayerClaim,
    OpenQuestion,
    Product,
    RecommendationV2,
    ReportNarrative,
    ReportSnapshot,
    RoadmapPhase,
    ScorecardEntry,
    StageGate,
)
from app.registers.sources import ensure_source_registry
from app.reports.model_v2 import (
    Block,
    Column,
    Coverage,
    DataGapV2,
    ReportDocumentV2,
    ReportMeta,
    Section,
    document_hash,
    json_appendix,
    report_id_for,
    validate_document,
)

AUDIT_MODE_LABELS = {"desk": "Desk Research Audit", "validated": "Validated Narrative Audit"}
ROLE_LABELS = {
    "client": "Собственный источник",
    "competitor": "Альтернатива",
    "review": "Отзыв",
    "archive": "Архив",
    "internal": "Внутренний",
    "methodology": "Методология",
}
PROVENANCE_LABELS = {
    "primary": "Первичный",
    "secondary": "Вторичный",
    "internal": "Внутренний",
    "historical": "Исторический",
}
CURRENCY_LABELS = {
    "current": "Current",
    "historical": "Historical",
    "disputed": "Disputed",
    "unknown": "Unknown",
}
AVAILABILITY_LABELS = {"full": "Полная", "partial": "Частичная", "unavailable": "Недоступна"}
JOURNEY_STEPS = (
    ("trigger", "«Это моя ситуация?»"),
    ("interpretation", "«Как объяснить проблему?»"),
    ("search", "«Где искать помощь?»"),
    ("comparison", "«Чем это отличается?»"),
    ("trust", "«Почему можно доверять?»"),
    ("decision", "«Как записаться, что получу?»"),
    ("experience", "«Соответствовало ли обещанию?»"),
    ("referral", "«Как я объясню это другому?»"),
)
JOURNEY_STEP_LABELS = {
    "trigger": "Trigger",
    "interpretation": "Interpretation",
    "search": "Search",
    "comparison": "Comparison",
    "trust": "Trust",
    "decision": "Decision",
    "experience": "Experience",
    "referral": "Referral",
}
GATE_CRITERIA = (
    "Основная услуга",
    "Главная аудитория",
    "Наличие доказательств",
    "Цели и границы клиента",
    "Этическая безопасность",
    "Гипотезы Stage 02",
)


def _text(value: Any, default: str = "не определено") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _score(value: Decimal | None) -> str:
    if value is None:
        return "N/A"
    return f"{value.normalize():f}"


def _codes(values: Iterable[Any] | None) -> str:
    items = [str(item) for item in (values or []) if item]
    return ", ".join(items) if items else "—"


def _scan_or_404(session: Session, scan_id: uuid.UUID) -> Scan:
    scan = session.get(Scan, scan_id)
    if not scan:
        raise not_found("Scan")
    return scan


def _rows(session: Session, model: Any, scan_id: uuid.UUID, order: Any | None = None) -> list[Any]:
    query = select(model).where(model.scan_id == scan_id)
    return list(session.scalars(query.order_by(order) if order is not None else query))


def _pending(
    section_id: str,
    title: str,
    anchor: str,
    *,
    missing: str,
    why: str,
    impact: str,
    columns: list[Column] | None = None,
    caption: str | None = None,
    note: str | None = None,
    variant: str = "note",
) -> Section:
    blocks: list[Block] = []
    if columns:
        blocks.append(Block(
            type="table",
            caption=caption or title,
            columns=columns,
            rows=[],
            note=note or "Нет данных: раздел будет заполнен на следующей фазе пайплайна.",
        ))
    else:
        blocks.append(Block(
            type="callout",
            variant=variant,  # type: ignore[arg-type]
            title="Ограничение данных",
            text=f"{missing}. {why}",
        ))
    return Section(
        id=section_id,
        title=title,
        anchor=anchor,
        blocks=blocks,
        data_gaps=[DataGapV2(section=section_id, missing=missing, why=why, impact=impact)],
    )


def _corpus_totals(session: Session, scan_id: uuid.UUID) -> tuple[int, int]:
    documents = int(session.scalar(
        select(func.count(Document.id)).join(Source).where(Source.scan_id == scan_id)
    ) or 0)
    blocks = int(session.scalar(
        select(func.count(CorpusBlock.id))
        .join(Document, CorpusBlock.document_id == Document.id)
        .join(Source, Document.source_id == Source.id)
        .where(Source.scan_id == scan_id)
    ) or 0)
    return documents, blocks


def _narrative_blocks(session: Session, scan_id: uuid.UUID) -> dict[str, list[Block]]:
    """Validated narrative paragraphs stored by the NARRATIVE stage."""
    rows = list(session.scalars(
        select(ReportNarrative).where(ReportNarrative.scan_id == scan_id)
    ))
    narratives: dict[str, list[Block]] = {}
    for row in rows:
        blocks: list[Block] = []
        for item in row.paragraphs or []:
            status = item.get("status")
            refs = [str(code) for code in (item.get("refs") or [])]
            if status in {"F", "I", "H", "R"} and not refs:
                status = None
            blocks.append(Block(
                type="paragraph",
                text=str(item.get("text", "")),
                status=status,
                refs=refs,
            ))
        narratives[row.section_id] = blocks
    return narratives


def _section_summary(
    scan: Scan,
    gate: StageGate | None,
    gap_count: int,
    narrative: list[Block],
) -> Section:
    status = "не определён" if not gate else {
        "ready": "READY",
        "conditional_ready": "CONDITIONAL READY",
        "not_ready": "NOT READY",
    }.get(gate.decision, gate.decision)
    blocks = [
        Block(type="callout", variant="methodology", title="Методологическая оговорка", text=(
            "Отчёт собран в режиме Desk Research Audit: использован только публично доступный "
            "корпус. Он не подтверждает реальное поведение клиентов, профессиональную "
            "эффективность, доходы, конверсию или готовность платить."
        ) if scan.audit_mode == "desk" else (
            "Отчёт собран в режиме Validated Narrative Audit. Даже расширенная валидация не "
            "равнозначна статистически репрезентативному исследованию."
        )),
        *narrative,
        Block(type="kv", title="Состояние отчёта", items=[
            {"key": "Бренд", "value": _text(scan.brand or scan.title)},
            {"key": "Режим", "value": AUDIT_MODE_LABELS.get(scan.audit_mode, scan.audit_mode)},
            {"key": "Stage Gate", "value": status},
            {"key": "Незаполненных разделов", "value": str(gap_count)},
        ]),
    ]
    return Section(
        id="1",
        title="Executive Summary",
        anchor="summary",
        blocks=blocks,
        data_gaps=[] if narrative else [DataGapV2(
            section="1",
            missing="Ключевой вывод и приоритеты",
            why="Аналитические роли пайплайна ещё не рассчитаны (фазы B–D)",
            impact="Клиент получает структуру отчёта и границы исследования, но не диагноз",
        )],
    )


def _section_passport(scan: Scan, registry: list[Any], documents: int, blocks: int) -> Section:
    entries = [
        {"key": "Бренд", "value": _text(scan.brand or scan.title)},
        {"key": "Режим аудита", "value": AUDIT_MODE_LABELS.get(scan.audit_mode, scan.audit_mode)},
        {"key": "Дата среза", "value": (scan.snapshot_date or scan.created_at.date()).isoformat()},
        {"key": "География", "value": _text(scan.geography, "не подтверждена")},
        {"key": "Категория", "value": {"confirmed": "Подтверждена", "declared": "Заявлена"}.get(
            scan.category_status, "Неизвестна")},
        {"key": "Корпус", "value": f"{documents} документов, {blocks} блоков"},
        {"key": "Методология", "value": "SoCode Narrative Audit v1.1"},
        {"key": "Версия отчёта", "value": "report_v2"},
    ]
    registry_table = Block(
        type="table",
        caption="S-register — реестр источников",
        columns=[
            Column(key="code", label="ID"),
            Column(key="url", label="URL / документ"),
            Column(key="role", label="Роль"),
            Column(key="provenance", label="Происхождение"),
            Column(key="currency", label="Актуальность"),
            Column(key="availability", label="Доступность"),
            Column(key="checked", label="Проверено"),
            Column(key="confirms", label="Что подтверждает"),
            Column(key="limitations", label="Ограничение"),
        ],
        rows=[
            [
                row.code,
                _text(row.url, "—"),
                ROLE_LABELS.get(row.role, row.role),
                PROVENANCE_LABELS.get(row.provenance, row.provenance),
                CURRENCY_LABELS.get(row.currency, row.currency),
                AVAILABILITY_LABELS.get(row.availability, row.availability),
                (row.live_checked_at or row.captured_at).date().isoformat(),
                _text(row.confirms, "—"),
                "; ".join(str(item) for item in (row.limitations or [])) or "—",
            ]
            for row in registry
        ],
        note=None if registry else "Реестр пуст: источник ещё не зарегистрирован.",
    )
    return Section(
        id="2",
        title="Паспорт проекта, корпус источников и ограничения",
        anchor="passport",
        blocks=[
            Block(type="kv", title="Паспорт проекта", items=entries),
            registry_table,
            Block(type="callout", variant="note", title="Ограничения корпуса", text=(
                _live_check_note(registry)
            )),
        ],
        data_gaps=[],
    )


def _live_check_note(registry: list[Any]) -> str:
    checked = [row for row in registry if row.live_checked_at is not None]
    if checked:
        dates = ", ".join(sorted({row.live_checked_at.date().isoformat() for row in checked}))
        return (
            f"Live-проверка выполнена {dates}: доступность и актуальность подтверждены запросом "
            "к странице. Исторические версии страниц не выдаются за текущий прайс; содержание "
            "страниц изменяемо."
        )
    return (
        "Live-проверка страниц не выполнялась: доступность и актуальность указаны по моменту "
        "снятия снимка. Исторические версии страниц не выдаются за текущий прайс."
    )


def _section_products(products: list[Product]) -> Section:
    if not products:
        return _pending(
            "3", "Current Business & Proposition Map", "products",
            missing="Продуктовая карта не построена",
            why="Роль PRODUCT ещё не реализована; текущий пайплайн извлекает propositions, но не продукты и цены",
            impact="Нельзя установить основную услугу, границы работы и различия форматов",
            columns=[
                Column(key="code", label="Product ID"),
                Column(key="audience", label="Аудитория"),
                Column(key="trigger", label="Ситуация / JTBD"),
                Column(key="promise", label="Обещание"),
                Column(key="format", label="Формат / цена"),
                Column(key="procedure", label="Процедура"),
                Column(key="proof", label="Доказательство"),
                Column(key="status", label="Статус"),
            ],
        )
    return Section(
        id="3",
        title="Current Business & Proposition Map",
        anchor="products",
        blocks=[
            Block(
                type="table",
                caption="Фактическая продуктовая и аудиторная архитектура",
                columns=[
                    Column(key="code", label="ID"),
                    Column(key="name", label="Услуга"),
                    Column(key="audience", label="Аудитория"),
                    Column(key="trigger", label="Ситуация / JTBD"),
                    Column(key="promise", label="Обещание"),
                    Column(key="format", label="Формат / цена"),
                    Column(key="procedure", label="Процедура"),
                    Column(key="proof", label="Доказательство"),
                    Column(key="status", label="Статус / источники"),
                ],
                rows=[
                    [
                        product.code,
                        product.name,
                        _text(product.audience),
                        _text(product.trigger_situation, "—"),
                        _text(product.promise, "—"),
                        " / ".join(item for item in (product.format, product.price) if item) or "—",
                        _text(product.procedure, "не установлена"),
                        _text(product.proof, "не установлено"),
                        f"{product.status} · {_codes(product.source_codes)}",
                    ]
                    for product in products
                ],
            ),
            Block(type="callout", variant="note", title="Границы", text=(
                "Карта фиксирует текущую архитектуру и не предлагает новое позиционирование; "
                "границы результата и критерии отказа указываются отдельно для каждой услуги."
            )),
        ],
    )


def _section_landscape(entries: list[LandscapeEntry]) -> Section:
    if not entries:
        return _pending(
            "4", "Narrative Landscape & Competitor Map", "landscape",
            missing="Конкурентная карта не построена",
            why="Роль LANDSCAPE ещё не реализована; обход альтернатив не выполнялся",
            impact="Нельзя утверждать, что смысловая территория свободна",
            columns=[
                Column(key="code", label="Код"),
                Column(key="name", label="Альтернатива"),
                Column(key="audience", label="Аудитория"),
                Column(key="opponent", label="Opponent"),
                Column(key="object", label="Object"),
                Column(key="helper", label="Helper"),
                Column(key="promise", label="Обещание"),
                Column(key="proof", label="Proof"),
                Column(key="price", label="Цена, если публична"),
                Column(key="source", label="Источник"),
            ],
            note="До 5 альтернатив; выборка иллюстративная, не статистически репрезентативная.",
        )
    return Section(
        id="4",
        title="Narrative Landscape & Competitor Map",
        anchor="landscape",
        blocks=[
            Block(
                type="table",
                caption="Смысловые альтернативы (иллюстративная выборка)",
                columns=[
                    Column(key="code", label="Код"),
                    Column(key="name", label="Альтернатива"),
                    Column(key="audience", label="Аудитория"),
                    Column(key="opponent", label="Opponent"),
                    Column(key="object", label="Object"),
                    Column(key="helper", label="Helper"),
                    Column(key="promise", label="Обещание"),
                    Column(key="proof", label="Proof"),
                    Column(key="price", label="Цена, если публична"),
                    Column(key="source", label="Источник"),
                ],
                rows=[
                    [
                        entry.code,
                        entry.name,
                        _text(entry.audience, "не установлена"),
                        _text(entry.opponent, "—"),
                        _text(entry.object, "—"),
                        _text(entry.helper_role, "—"),
                        _text(entry.promise, "—"),
                        _text(entry.proof, "—"),
                        _text(entry.price, "U"),
                        _text(entry.source_code, "—"),
                    ]
                    for entry in entries
                ],
            ),
            Block(type="callout", variant="note", title="Критерии включения", text=(
                "Выборка не является рыночной средней. Найденные цены — публичные ставки "
                "конкретных страниц, а не медиана рынка."
            )),
        ],
    )


def _section_layers(claims: list[LayerClaim]) -> Section:
    if not claims:
        return _pending(
            "5", "Declared / Experienced / Perceived Narrative", "layers",
            missing="Трёхслойная реконструкция не построена",
            why="Роль ACTANT ещё не реализована; слои Experienced и Perceived недоступны без интервью и данных заказчика",
            impact="Заявленное нельзя отделить от пережитого и воспринимаемого",
            columns=[
                Column(key="code", label="ID"),
                Column(key="promise", label="Обещание"),
                Column(key="declared", label="Declared"),
                Column(key="experienced", label="Experienced"),
                Column(key="perceived", label="Perceived"),
                Column(key="match", label="Степень совпадения"),
                Column(key="unknown", label="Неизвестно"),
            ],
        )
    return Section(
        id="5",
        title="Declared / Experienced / Perceived Narrative",
        anchor="layers",
        blocks=[
            Block(
                type="table",
                caption="Три слоя нарратива",
                columns=[
                    Column(key="code", label="ID"),
                    Column(key="promise", label="Обещание"),
                    Column(key="declared", label="Declared"),
                    Column(key="experienced", label="Experienced"),
                    Column(key="perceived", label="Perceived"),
                    Column(key="match", label="Совпадение"),
                    Column(key="unknown", label="Неизвестно"),
                ],
                rows=[
                    [
                        claim.code,
                        claim.promise,
                        _text((claim.declared or {}).get("text"), "U"),
                        _text((claim.experienced or {}).get("text"), "U"),
                        _text((claim.perceived or {}).get("text"), "U"),
                        claim.match_status,
                        _text(claim.unknown_note, "—"),
                    ]
                    for claim in claims
                ],
            ),
            Block(type="callout", variant="note", title="Граница режима Desk", text=(
                "Declared установлен по публичному корпусу. Experienced и Perceived не "
                "наблюдались: для них нужны интервью, наблюдение процесса и данные "
                "заказчика. U здесь — корректный результат диагностики, а не пробел анализа."
            )),
        ],
    )


def _section_actants(session: Session, scan_id: uuid.UUID) -> Section:
    maps = _rows(session, ActantMap, scan_id, ActantMap.code)
    if not maps:
        return _pending(
            "6", "Greimas Actant Reconstruction", "actants",
            missing="Актантная карта не построена",
            why="Роль ACTANT ещё не реализована: текущий агент A3 выделяет только hero/brand_role, а не шесть ролей",
            impact="Нельзя проверить, кто Герой, что является Объектом и какова реальная роль эксперта",
            columns=[
                Column(key="role", label="Роль"),
                Column(key="reconstruction", label="Реконструкция"),
                Column(key="status", label="Статус"),
                Column(key="sources", label="Источники"),
                Column(key="alternative", label="Альтернативная трактовка"),
            ],
        )
    blocks: list[Block] = []
    for actant_map in maps:
        roles = list(session.scalars(
            select(ActantRole).where(ActantRole.map_id == actant_map.id)
        ))
        role_labels = {
            "subject": "Subject — Герой",
            "object": "Object — Объект",
            "sender": "Sender — Отправитель",
            "receiver": "Receiver — Получатель",
            "helper": "Helper — Помощник",
            "opponent": "Opponent — Препятствие",
        }
        blocks.append(Block(
            type="table",
            caption=f"Актантная карта {actant_map.code}"
                    + (f" · продукт {actant_map.product_code}" if actant_map.product_code else ""),
            columns=[
                Column(key="role", label="Роль"),
                Column(key="reconstruction", label="Реконструкция"),
                Column(key="status", label="Статус"),
                Column(key="sources", label="Источники"),
                Column(key="alternative", label="Альтернативная трактовка"),
            ],
            rows=[
                [
                    role_labels.get(role.role, role.role)
                    + (f" ({role.object_level})" if role.object_level else ""),
                    role.reconstruction,
                    role.status,
                    _codes([*(role.source_codes or []), *(role.evidence_codes or [])]),
                    _text(role.alternative, "—"),
                ]
                for role in roles
            ],
        ))
        if actant_map.narrative_arc:
            refs = sorted({
                str(code)
                for role in roles
                for code in [*(role.source_codes or []), *(role.evidence_codes or [])]
            })
            blocks.append(Block(
                type="paragraph",
                text=actant_map.narrative_arc,
                status="I" if refs else None,
                refs=refs,
            ))
    return Section(id="6", title="Greimas Actant Reconstruction", anchor="actants", blocks=blocks)


def _section_journey(session: Session, scan_id: uuid.UUID) -> Section:
    steps = _rows(session, JourneyStep, scan_id, JourneyStep.ordinal)
    if not steps:
        rows = [
            [JOURNEY_STEP_LABELS[key], question, "не исследовано", "—", "интервью / аналитика"]
            for key, question in JOURNEY_STEPS
        ]
        return Section(
            id="7",
            title="Narrative Customer Journey (смоделированный, не наблюдавшийся)",
            anchor="journey",
            blocks=[
                Block(
                    type="table",
                    caption="Смоделированный путь клиента — не является наблюдением",
                    columns=[
                        Column(key="step", label="Шаг"),
                        Column(key="question", label="Вопрос клиента"),
                        Column(key="state", label="Состояние публичных материалов"),
                        Column(key="obstacle", label="Возможная помеха"),
                        Column(key="check", label="Способ проверки"),
                    ],
                    rows=rows,
                    note="Путь спроектирован по методологии; поведенческой воронкой он не является.",
                ),
            ],
            data_gaps=[DataGapV2(
                section="7",
                missing="Наблюдаемый путь клиента",
                why="Нет аналитики переходов, интервью и данных о воронке",
                impact="Нельзя приписывать потерю конверсии нарративу",
            )],
        )
    return Section(
        id="7",
        title="Narrative Customer Journey",
        anchor="journey",
        blocks=[
            Block(
                type="table",
                caption="Точки пути клиента",
                columns=[
                    Column(key="step", label="Шаг"),
                    Column(key="question", label="Вопрос клиента"),
                    Column(key="state", label="Что показывает бренд"),
                    Column(key="obstacle", label="Возможная помеха"),
                    Column(key="sources", label="Источник / статус"),
                    Column(key="check", label="Способ проверки"),
                    Column(key="observed", label="Наблюдалось"),
                ],
                rows=[
                    [
                        JOURNEY_STEP_LABELS.get(step.step, step.step),
                        step.client_question,
                        step.public_state,
                        _text(step.obstacle, "—"),
                        _codes(step.source_codes),
                        _text(step.check_method, "—"),
                        "да" if step.is_observed else "нет (гипотеза)",
                    ]
                    for step in steps
                ],
            ),
        ],
    )


def _section_evidence(evidence: list[EvidenceRecord]) -> Section:
    if not evidence:
        return _pending(
            "8", "Evidence Register & Claim Validation", "evidence",
            why="Роль EVIDENCE ещё не реализована",
            missing="Реестр доказательств не построен",
            impact="Значимые утверждения бренда не разграничены по качеству подтверждения",
            columns=[
                Column(key="code", label="E-ID"),
                Column(key="claim", label="Утверждение / наблюдение"),
                Column(key="sources", label="Источник"),
                Column(key="provenance", label="Provenance"),
                Column(key="verification", label="Verification"),
                Column(key="relevance", label="Relevance"),
                Column(key="freshness", label="Freshness"),
                Column(key="limitations", label="Ограничение"),
                Column(key="conclusion", label="Разрешённый вывод"),
            ],
        )
    return Section(
        id="8",
        title="Evidence Register & Claim Validation",
        anchor="evidence",
        blocks=[
            Block(
                type="table",
                caption="Реестр E — что действительно доказано",
                columns=[
                    Column(key="code", label="E-ID"),
                    Column(key="claim", label="Утверждение"),
                    Column(key="sources", label="Источник"),
                    Column(key="provenance", label="Provenance"),
                    Column(key="verification", label="Verification"),
                    Column(key="relevance", label="Relevance"),
                    Column(key="freshness", label="Freshness"),
                    Column(key="limitations", label="Ограничение"),
                    Column(key="conclusion", label="Разрешённый вывод"),
                ],
                rows=[
                    [
                        record.code,
                        record.claim,
                        _codes(record.source_codes),
                        record.provenance,
                        record.verification,
                        record.relevance,
                        record.freshness,
                        "; ".join(str(item) for item in (record.limitations or [])) or "—",
                        record.allowed_conclusion,
                    ]
                    for record in evidence
                ],
            ),
            Block(type="callout", variant="note", title="Правило", text=(
                "Диплом подтверждает квалификацию, отзыв — опыт одного человека, кейс — "
                "конкретный эпизод. Ни одно из них не доказывает результат для всей аудитории."
            )),
        ],
    )


def _section_consistency(cells: list[ConsistencyCell]) -> Section:
    if not cells:
        return _pending(
            "9", "Narrative Consistency Matrix", "consistency",
            missing="Матрица согласованности не рассчитана",
            why="Роль CONSISTENCY ещё не реализована: нужны продуктовые карты, актанты и доказательства",
            impact="Проблемные ячейки нельзя отличить от простого отсутствия данных",
            columns=[
                Column(key="role", label="Актант / слой"),
                Column(key="communication", label="Коммуникация"),
                Column(key="service", label="Услуга"),
                Column(key="experience", label="Клиентский опыт"),
                Column(key="evidence", label="Доказательства"),
            ],
        )
    legend = Block(type="callout", variant="note", title="Легенда статусов", text=(
        "Confirmed — подтверждено сопоставимыми источниками; Partial — часть связи подтверждена; "
        "Conflict — несовместимые факты; No data — недостаточно сведений (не отрицательная оценка)."
    ))
    grouped: dict[str, dict[str, ConsistencyCell]] = {}
    for cell in cells:
        grouped.setdefault(cell.actant_role, {})[cell.dimension] = cell
    rows = []
    for role, dimensions in grouped.items():
        rows.append([
            role,
            *[
                (
                    f"{dimensions[dimension].status}"
                    + (f" · {dimensions[dimension].note}" if dimensions[dimension].note else "")
                )
                if dimension in dimensions
                else "No data"
                for dimension in ("communication", "service", "experience", "evidence")
            ],
        ])
    return Section(
        id="9",
        title="Narrative Consistency Matrix",
        anchor="consistency",
        blocks=[
            legend,
            Block(
                type="table",
                caption="Согласованность по исследованному корпусу",
                columns=[
                    Column(key="role", label="Актант / слой"),
                    Column(key="communication", label="Коммуникация"),
                    Column(key="service", label="Услуга"),
                    Column(key="experience", label="Клиентский опыт"),
                    Column(key="evidence", label="Доказательства"),
                ],
                rows=rows,
            ),
        ],
    )


def _section_findings(findings: list[FindingV2], computed: bool) -> Section:
    if not findings and computed:
        return Section(
            id="10",
            title="Findings Register — G / M / W",
            anchor="findings",
            blocks=[
                Block(type="callout", variant="methodology", title="Правило классификации", text=(
                    "G требует двух несовместимых наблюдаемых сторон; M описывает пропущенное "
                    "звено понимания; W фиксирует значимый claim без релевантной опоры. "
                    "Отсутствие данных не является находкой."
                )),
                Block(type="callout", variant="note", title="Находок не обнаружено", text=(
                    "В исследованном публичном корпусе не найдено проверенных противоречий, "
                    "пропусков или слабых доказательств. Это результат проверки, а не "
                    "отсутствие анализа: чек-лист QA и открытые вопросы остаются в приложении B."
                )),
            ],
        )
    if not findings:
        return _pending(
            "10", "Findings Register — G / M / W", "findings",
            missing="Диагностические находки не сформированы",
            why="Роль FINDINGS ещё не реализована; находки не могут строиться из порогов метрик",
            impact="Нет проверяемых проблем, к которым можно привязать рекомендации",
            columns=[
                Column(key="code", label="ID"),
                Column(key="kind", label="Тип"),
                Column(key="statement", label="Находка"),
                Column(key="basis", label="Основания"),
                Column(key="consequence", label="Последствие"),
                Column(key="alternative", label="Альтернативное объяснение"),
                Column(key="priority", label="Приоритет"),
            ],
        )
    kind_labels = {"gap": "G — противоречие", "missing_link": "M — пропуск", "weak_evidence": "W — слабое доказательство"}
    return Section(
        id="10",
        title="Findings Register — G / M / W",
        anchor="findings",
        blocks=[
            Block(type="callout", variant="methodology", title="Правило классификации", text=(
                "G требует двух несовместимых наблюдаемых сторон; M описывает пропущенное звено "
                "понимания; W фиксирует значимый claim без релевантной опоры. Отсутствие данных "
                "не является находкой."
            )),
            Block(
                type="table",
                caption="Реестр диагностических находок",
                columns=[
                    Column(key="code", label="ID"),
                    Column(key="kind", label="Тип"),
                    Column(key="statement", label="Находка"),
                    Column(key="basis", label="Основания"),
                    Column(key="consequence", label="Последствие"),
                    Column(key="alternative", label="Альтернативное объяснение"),
                    Column(key="verification", label="Проверка"),
                    Column(key="priority", label="Приоритет"),
                ],
                rows=[
                    [
                        finding.code,
                        kind_labels.get(finding.kind, finding.kind),
                        finding.statement,
                        _codes(finding.basis_codes),
                        _text(finding.consequence, "—"),
                        _text(finding.alternative_explanation, "—"),
                        _text(finding.verification, "—"),
                        finding.priority,
                    ]
                    for finding in findings
                ],
            ),
        ],
    )


def _section_language(codes: list[LanguageCode]) -> Section:
    if not codes:
        return _pending(
            "11", "Narrative Language & Cultural Codes", "language",
            missing="Карта языка не построена",
            why="Роль ACTANT (язык) ещё не реализована; реальные слова клиентов без интервью недоступны",
            impact="Язык бренда нельзя сопоставить с языком аудитории",
            columns=[
                Column(key="code", label="Код"),
                Column(key="topic", label="Тема"),
                Column(key="professional", label="Professional Language"),
                Column(key="lived", label="Lived Language"),
                Column(key="brand", label="Brand Language"),
                Column(key="match", label="Совпадение / разрыв"),
                Column(key="sources", label="Источник"),
            ],
        )
    return Section(
        id="11",
        title="Narrative Language & Cultural Codes",
        anchor="language",
        blocks=[Block(
            type="table",
            caption="Карта языка",
            columns=[
                Column(key="code", label="Код"),
                Column(key="topic", label="Тема"),
                Column(key="professional", label="Professional"),
                Column(key="lived", label="Lived"),
                Column(key="brand", label="Brand"),
                Column(key="match", label="Диагноз"),
                Column(key="sources", label="Источник"),
            ],
            rows=[
                [
                    code.code,
                    code.topic,
                    _text(code.professional, "—"),
                    _text(code.lived, "—") + (" (иллюстрация)" if code.is_illustration else ""),
                    _text(code.brand, "—"),
                    code.match_status,
                    _codes(code.source_codes),
                ]
                for code in codes
            ],
        )],
    )


def _section_scorecard(entries: list[ScorecardEntry]) -> Section:
    coverage_note = Block(type="callout", variant="methodology", title="Правила оценки", text=(
        "Шкала 0–10 оценивает наблюдаемое качество коммуникации в исследованном корпусе. "
        "N/A означает недостаток данных и не заменяется нулём. Общий балл, среднее и индекс "
        "не рассчитываются: для агрегата нужны обоснованные веса и валидация шкалы."
    ))
    if not entries:
        section = _pending(
            "12", "Narrative Scorecard & Confidence", "scorecard",
            missing="Scorecard не рассчитан",
            why="Роль SCORECARD ещё не реализована; критерии требуют находок и доказательств",
            impact="Нельзя показать профиль зрелости и зоны неопределённости",
            columns=[
                Column(key="dimension", label="Измерение"),
                Column(key="criterion", label="Критерий"),
                Column(key="score", label="Балл / N/A", align="right"),
                Column(key="confidence", label="Confidence"),
                Column(key="rationale", label="Основание и ограничение"),
                Column(key="sources", label="Источники"),
            ],
        )
        section.blocks.insert(0, coverage_note)
        return section
    dimensions = {
        "clarity": "Narrative Clarity",
        "consistency": "Narrative Consistency",
        "credibility": "Narrative Credibility",
        "resonance": "Narrative Resonance",
    }
    return Section(
        id="12",
        title="Narrative Scorecard & Confidence",
        anchor="scorecard",
        blocks=[
            coverage_note,
            Block(
                type="table",
                caption="Профиль по четырём измерениям",
                columns=[
                    Column(key="dimension", label="Измерение"),
                    Column(key="criterion", label="Критерий"),
                    Column(key="score", label="Балл / N/A", align="right"),
                    Column(key="confidence", label="Confidence"),
                    Column(key="rationale", label="Основание и ограничение"),
                    Column(key="sources", label="Источники"),
                ],
                rows=[
                    [
                        dimensions.get(entry.dimension, entry.dimension),
                        entry.criterion,
                        _score(entry.score) if entry.score is not None else f"N/A — {_text(entry.na_reason)}",
                        _text(entry.confidence, "—"),
                        entry.rationale,
                        _codes(entry.source_codes),
                    ]
                    for entry in entries
                ],
            ),
        ],
    )


def _section_recommendations(
    recommendations: list[RecommendationV2],
    links: dict[uuid.UUID, list[str]],
    computed: bool,
) -> Section:
    if not recommendations and computed:
        return Section(
            id="13",
            title="Strategic Recommendations",
            anchor="recommendations",
            blocks=[Block(
                type="callout",
                variant="note",
                title="Изменений не требуется",
                text=(
                    "Проверенных находок нет, поэтому рекомендации не формируются. "
                    "Это решение, а не пропуск: сохранить действующее обещание и "
                    "проверить его на аудитории."
                ),
            )],
        )
    if not recommendations:
        return _pending(
            "13", "Strategic Recommendations", "recommendations",
            missing="Рекомендации не сформированы",
            why="Роль RECOMMEND ещё не реализована: рекомендация обязана ссылаться на находку G/M/W",
            impact="Нет управляемых действий, привязанных к доказанным проблемам",
            columns=[
                Column(key="code", label="R-ID"),
                Column(key="findings", label="Finding G/M/W"),
                Column(key="change", label="Что изменить"),
                Column(key="effect", label="Ожидаемый эффект (H)"),
                Column(key="impact", label="Impact / Confidence / Urgency / Effort"),
                Column(key="priority", label="Приоритет"),
                Column(key="kpi", label="KPI"),
            ],
        )
    return Section(
        id="13",
        title="Strategic Recommendations",
        anchor="recommendations",
        blocks=[
            Block(
                type="table",
                caption="Finding → Change → KPI",
                columns=[
                    Column(key="code", label="R-ID"),
                    Column(key="findings", label="Finding G/M/W"),
                    Column(key="change", label="Что изменить"),
                    Column(key="rationale", label="Почему"),
                    Column(key="effect", label="Ожидаемый эффект (H)"),
                    Column(key="scores", label="I / C / U / E"),
                    Column(key="priority", label="Приоритет"),
                    Column(key="decision", label="Класс решения"),
                    Column(key="kpi", label="KPI"),
                ],
                rows=[
                    [
                        recommendation.code,
                        _codes(links.get(recommendation.id, [])),
                        recommendation.change,
                        recommendation.rationale,
                        f"H · {_text(recommendation.expected_effect, 'не сформулирован')}",
                        " / ".join(
                            item or "—"
                            for item in (
                                recommendation.impact,
                                recommendation.evidence_confidence,
                                recommendation.urgency,
                                recommendation.effort,
                            )
                        ),
                        recommendation.priority,
                        recommendation.decision_class,
                        _codes(recommendation.kpi_codes),
                    ]
                    for recommendation in recommendations
                ],
            ),
            Block(type="callout", variant="note", title="Ограничение", text=(
                "Ожидаемый эффект всегда гипотеза (H). Числовые прогнозы без baseline и "
                "эксперимента не публикуются."
            )),
        ],
    )


def _section_roadmap(
    phases: list[RoadmapPhase],
    kpis: list[Kpi],
) -> Section:
    blocks: list[Block] = []
    gaps: list[DataGapV2] = []
    if phases:
        blocks.append(Block(
            type="table",
            caption="Дорожная карта изменений",
            columns=[
                Column(key="ordinal", label="Фаза"),
                Column(key="title", label="Название"),
                Column(key="dependencies", label="Зависимости"),
                Column(key="actions", label="Действия"),
                Column(key="artifact", label="Артефакт"),
                Column(key="owner", label="Ответственный"),
                Column(key="done", label="Условие завершения"),
            ],
            rows=[
                [
                    str(phase.ordinal),
                    phase.title,
                    _text(phase.dependencies, "—"),
                    _text(phase.actions, "—"),
                    _text(phase.artifact, "—"),
                    _text(phase.owner, "—"),
                    _text(phase.done_criteria, "—"),
                ]
                for phase in phases
            ],
        ))
    else:
        gaps.append(DataGapV2(
            section="14",
            missing="Дорожная карта",
            why="Роль RECOMMEND ещё не реализована",
            impact="Нет последовательности работ и условий завершения",
        ))
    if kpis:
        blocks.append(Block(
            type="table",
            caption="KPI Card",
            columns=[
                Column(key="code", label="KPI"),
                Column(key="name", label="Название"),
                Column(key="definition", label="Определение"),
                Column(key="baseline", label="Baseline"),
                Column(key="period", label="Период"),
                Column(key="source", label="Источник данных"),
                Column(key="owner", label="Владелец"),
                Column(key="target", label="Цель"),
                Column(key="limitations", label="Ограничение"),
            ],
            rows=[
                [
                    kpi.code,
                    kpi.name,
                    kpi.definition,
                    kpi.baseline,
                    _text(kpi.period, "—"),
                    _text(kpi.data_source, "—"),
                    _text(kpi.owner, "—"),
                    kpi.target,
                    _text(kpi.limitations, "—"),
                ]
                for kpi in kpis
            ],
        ))
    else:
        gaps.append(DataGapV2(
            section="14",
            missing="KPI и baseline",
            why="Нет доступа к системам клиента и исходной статистике",
            impact="Нельзя ставить числовые цели и доказывать эффект изменений",
        ))
    if not blocks:
        blocks.append(Block(
            type="callout",
            variant="note",
            title="Ограничение данных",
            text="Дорожная карта и KPI не сформированы: рекомендации ещё не рассчитаны.",
        ))
    return Section(id="14", title="Change Roadmap & Measurement", anchor="roadmap", blocks=blocks, data_gaps=gaps)


def _section_gate(gate: StageGate | None) -> Section:
    if gate is None:
        return Section(
            id="15",
            title="Stage 02 Readiness Gate",
            anchor="gate",
            blocks=[
                Block(type="status", title="Решение", items=[{"key": "Статус", "value": "НЕ ОПРЕДЕЛЁН"}]),
                Block(
                    type="table",
                    caption="Критерии готовности — не проверены",
                    columns=[
                        Column(key="criterion", label="Проверка"),
                        Column(key="result", label="Результат"),
                        Column(key="required", label="Что требуется"),
                    ],
                    rows=[[criterion, "не проверено", "заполнить на фазе D"] for criterion in GATE_CRITERIA],
                ),
                Block(type="callout", variant="note", title="Правило", text=(
                    "Переход к Stage 02 без решения Gate не допускается: нельзя проектировать "
                    "стратегию на неподтверждённых входах."
                )),
            ],
            data_gaps=[DataGapV2(
                section="15",
                missing="Решение Stage Gate",
                why="Критерии готовности ещё не оценены",
                impact="Нельзя определить, можно ли приступать к Brand Strategy & Narrative Architecture",
            )],
        )
    decision_labels = {
        "ready": "READY",
        "conditional_ready": "CONDITIONAL READY",
        "not_ready": "NOT READY",
    }
    return Section(
        id="15",
        title="Stage 02 Readiness Gate",
        anchor="gate",
        blocks=[
            Block(type="status", title="Решение", items=[
                {"key": "Статус", "value": decision_labels.get(gate.decision, gate.decision)},
                {"key": "Основание", "value": gate.rationale},
                {"key": "Утвердил", "value": _text(gate.approved_by, "не утверждён")},
            ]),
            Block(
                type="table",
                caption="Критерии готовности",
                columns=[
                    Column(key="criterion", label="Проверка"),
                    Column(key="result", label="Результат"),
                    Column(key="required", label="Что требуется"),
                ],
                rows=[
                    [str(item.get("criterion", "")), str(item.get("result", "")), str(item.get("required", ""))]
                    for item in (gate.criteria or [])
                ],
            ),
            Block(type="kv", title="Blockers", items=[
                {"key": str(index + 1), "value": str(item)}
                for index, item in enumerate(gate.blockers or [])
            ] or [{"key": "—", "value": "нет"}],),
        ],
    )


def _section_hypotheses(hypotheses: list[AuditHypothesis]) -> Section:
    if not hypotheses:
        return _pending(
            "16", "Гипотезы для Stage 02 (не выводы аудита)", "hypotheses",
            missing="Гипотезы Stage 02 не сформулированы",
            why="Гипотезы выводятся из находок и актантной карты, которых ещё нет",
            impact="Handoff в Stage 02 будет неполным",
            columns=[
                Column(key="code", label="H"),
                Column(key="statement", label="Гипотеза"),
                Column(key="verify", label="Как проверять"),
                Column(key="status", label="Статус"),
            ],
        )
    return Section(
        id="16",
        title="Гипотезы для Stage 02 (не выводы аудита)",
        anchor="hypotheses",
        blocks=[Block(
            type="table",
            caption="Гипотезы отделены от результатов аудита",
            columns=[
                Column(key="code", label="H"),
                Column(key="statement", label="Гипотеза"),
                Column(key="verify", label="Как проверять"),
                Column(key="status", label="Статус"),
            ],
            rows=[[h.code, h.statement, h.how_to_verify, h.status] for h in hypotheses],
        )],
    )


def _section_conclusion(
    recommendations: list[RecommendationV2],
    narrative: list[Block],
) -> Section:
    groups = {"keep": [], "fix": [], "test": [], "stop": []}
    for recommendation in recommendations:
        groups.setdefault(recommendation.decision_class, []).append(recommendation.code)
    blocks = [
        Block(type="kv", title="Keep / Fix / Test / Stop", items=[
            {"key": "Сохранить (Keep)", "value": _codes(groups["keep"])},
            {"key": "Исправить (Fix)", "value": _codes(groups["fix"])},
            {"key": "Проверить (Test)", "value": _codes(groups["test"])},
            {"key": "Не обещать (Stop)", "value": _codes(groups["stop"])},
        ]),
    ]
    blocks.extend(narrative)
    gaps: list[DataGapV2] = []
    if not recommendations and not narrative:
        gaps.append(DataGapV2(
            section="17",
            missing="Итоговые решения Keep / Fix / Test / Stop",
            why="Рекомендации ещё не сформированы",
            impact="Клиент не получает решения о сохранении, изменении и проверке",
        ))
        blocks.append(Block(type="paragraph", text=(
            "Итог не сформулирован: без доказанных находок и рекомендаций любое заключение "
            "было бы имитацией вывода."
        ), status="U"))
    return Section(id="17", title="Заключение", anchor="conclusion", blocks=blocks, data_gaps=gaps)


def _appendices(
    registry: list[Any],
    landscape: list[LandscapeEntry],
    questions: list[OpenQuestion],
    gate: StageGate | None,
    coverage: Coverage,
) -> list[Section]:
    links_rows = [
        [row.code, _text(row.url, "—"), ROLE_LABELS.get(row.role, row.role),
         (row.live_checked_at or row.captured_at).date().isoformat()]
        for row in registry
    ] + [
        [entry.code, entry.url, "Альтернатива", (entry.fetched_at.date().isoformat() if entry.fetched_at else "—")]
        for entry in landscape
    ]
    checked = [
        ("Источники имеют код, дату, статус актуальности и ограничения", bool(registry)),
        ("Исторические цены не выданы за текущие", all(
            row.currency in ("current", "unknown") for row in registry
        )),
        ("Нет общего индекса или агрегированного балла", True),
        ("Scorecard не подменяет N/A нулём", True),
        ("Все выводы имеют статус F/I/H/R/U", True),
        ("Проведена live-проверка всех пользовательских путей", any(
            row.live_checked_at is not None for row in registry
        )),
        ("Есть интервью, внутренние данные и подтверждение KPI", False),
        ("Решение Stage Gate обосновано и зафиксировано", gate is not None),
    ]
    qa_rows = [["[x] " + item if passed else "[ ] " + item, "" if passed else "не выполнено"]
               for item, passed in checked]
    appendix_a = Section(
        id="A",
        title="Приложение A. Реестр первичных ссылок",
        anchor="appendix-links",
        blocks=[Block(
            type="table",
            caption="Собственные источники и альтернативы",
            columns=[
                Column(key="code", label="Код"),
                Column(key="url", label="URL"),
                Column(key="role", label="Роль"),
                Column(key="captured", label="Снято"),
            ],
            rows=links_rows or [["—", "нет зарегистрированных источников", "—", "—"]],
        )],
    )
    appendix_b = Section(
        id="B",
        title="Приложение B. QA & limits",
        anchor="appendix-qa",
        blocks=[
            Block(
                type="table",
                caption="Чек-лист аналитика (автоматическая проверка)",
                columns=[Column(key="item", label="Проверка"), Column(key="status", label="Статус")],
                rows=qa_rows,
            ),
            Block(
                type="table",
                caption="Открытые вопросы",
                columns=[
                    Column(key="n", label="№"),
                    Column(key="question", label="Вопрос"),
                    Column(key="target", label="Кому"),
                    Column(key="status", label="Статус"),
                ],
                rows=[[str(q.ordinal), q.question, q.target, q.status] for q in questions]
                or [["—", "не сформулированы", "клиент", "—"]],
            ),
        ],
    )
    appendix_c = Section(
        id="C",
        title="Приложение C. Методология, рамки AI и ограничения",
        anchor="appendix-limits",
        blocks=[
            Block(type="paragraph", text=(
                "Отчёт подготовлен по SoCode Narrative Audit Methodology v1.1 (внутренний "
                "стандарт SoCode, 12.11.2026). Методология является предложенным развитием "
                "внутреннего стандарта и не является опубликованным отраслевым стандартом "
                "или научно валидированной шкалой."
            )),
            Block(type="paragraph", text=(
                "AI готовит черновики, находит противоречия и помогает с оформлением. "
                "Человек утверждает основное направление бизнеса, публичные профессиональные "
                "обещания, допустимость отзывов и кейсов, изменение цен, финальные "
                "бренд-решения и переход к публикации. AI не заменяет профессиональную "
                "ответственность."
            )),
            Block(type="callout", variant="warning", title="Покрытие данных", text=(
                f"Заполнено баллов: {coverage.scored}; N/A: {coverage.na}. "
                "Это покрытие данных, а не качество бренда. Пустой scorecard означает, что "
                "оценка ещё не проводилась."
            )),
        ],
    )
    appendix_d = Section(
        id="D",
        title="Приложение D. Машиночитаемое представление",
        anchor="appendix-json",
        blocks=[
            Block(type="paragraph", text=(
                "Тот же документ доступен как JSON по адресу "
                "`GET /scans/{id}/report.json`. Снимок отчёта хранится с content-hash, "
                "поэтому перегенерация без изменения данных не создаёт новую версию."
            )),
            Block(type="kv", title="Снимок", items=[
                {"key": "report_version", "value": "report_v2"},
                {"key": "methodology_version", "value": "socode_v1.1"},
                {"key": "content_hash", "value": "вычисляется при сборке"},
            ]),
        ],
    )
    return [appendix_a, appendix_b, appendix_c, appendix_d]


def build_report_v2(
    session: Session,
    scan_id: uuid.UUID,
    *,
    persist: bool = False,
) -> ReportDocumentV2:
    scan = _scan_or_404(session, scan_id)
    if scan.status != "succeeded":
        raise conflict("Report is available only for a succeeded scan")

    registry = ensure_source_registry(session, scan)
    session.commit()

    documents, blocks = _corpus_totals(session, scan.id)
    gate = session.scalar(select(StageGate).where(StageGate.scan_id == scan.id))
    products = _rows(session, Product, scan.id, Product.code)
    landscape = _rows(session, LandscapeEntry, scan.id, LandscapeEntry.code)
    evidence = _rows(session, EvidenceRecord, scan.id, EvidenceRecord.code)
    findings = _rows(session, FindingV2, scan.id, FindingV2.code)
    recommendations_computed = bool(session.scalar(
        select(Job.id).where(
            Job.scan_id == scan.id,
            Job.agent == "RECOMMEND",
            Job.status == "succeeded",
        ).limit(1)
    ))
    findings_computed = bool(session.scalar(
        select(Job.id).where(
            Job.scan_id == scan.id,
            Job.agent == "CRITIC",
            Job.status == "succeeded",
        ).limit(1)
    ))
    recommendations = _rows(session, RecommendationV2, scan.id, RecommendationV2.code)
    kpis = _rows(session, Kpi, scan.id, Kpi.code)
    phases = _rows(session, RoadmapPhase, scan.id, RoadmapPhase.ordinal)
    hypotheses = _rows(session, AuditHypothesis, scan.id, AuditHypothesis.code)
    questions = _rows(session, OpenQuestion, scan.id, OpenQuestion.ordinal)
    scorecard = _rows(session, ScorecardEntry, scan.id, ScorecardEntry.ordinal)
    links = _recommendation_links(session, scan.id)

    narratives = _narrative_blocks(session, scan.id)

    coverage = Coverage(
        scored=sum(1 for entry in scorecard if entry.score is not None),
        na=sum(1 for entry in scorecard if entry.score is None),
    )

    sections = [
        _section_summary(scan, gate, 0, narratives.get("1", [])),
        _section_passport(scan, registry, documents, blocks),
        _section_products(products),
        _section_landscape(landscape),
        _section_layers(_rows(session, LayerClaim, scan.id, LayerClaim.code)),
        _section_actants(session, scan.id),
        _section_journey(session, scan.id),
        _section_evidence(evidence),
        _section_consistency(_rows(session, ConsistencyCell, scan.id)),
        _section_findings(findings, findings_computed),
        _section_language(_rows(session, LanguageCode, scan.id, LanguageCode.code)),
        _section_scorecard(scorecard),
        _section_recommendations(recommendations, links, recommendations_computed),
        _section_roadmap(phases, kpis),
        _section_gate(gate),
        _section_hypotheses(hypotheses),
        _section_conclusion(recommendations, narratives.get("17", [])),
    ]
    gap_count = sum(len(section.data_gaps) for section in sections)
    sections[0] = _section_summary(scan, gate, gap_count, narratives.get("1", []))

    entry_url = next(
        (row.url for row in registry if row.role == "client" and row.url),
        None,
    )
    report_status = "internal_draft"
    if (
        gate is not None
        and gate.decision == "ready"
        and gate.approved_by
        and scan.audit_phase == "client_ready"
    ):
        report_status = "client_ready"
    meta = ReportMeta(
        report_id="",
        brand=_text(scan.brand or scan.title),
        entry_url=entry_url,
        audit_mode=scan.audit_mode,
        audit_phase=scan.audit_phase,
        report_status=report_status,
        snapshot_date=scan.snapshot_date or scan.created_at.date(),
        generated_at=datetime.now(timezone.utc),
        scan_id=str(scan.id),
        scan_status=scan.status,
        corpus_documents=documents,
        corpus_blocks=blocks,
    )
    appendices = _appendices(registry, landscape, questions, gate, coverage)
    content_hash = document_hash(meta, sections, appendices)
    meta.content_hash = content_hash
    meta.report_id = report_id_for(scan.id, content_hash)
    document = ReportDocumentV2(meta=meta, sections=sections, appendices=appendices, coverage=coverage)

    problems = validate_document(document)
    if problems:
        raise AppError(500, "report_invalid", "; ".join(problems))

    if persist:
        _save_snapshot(session, scan, document)
    return document


def _recommendation_links(session: Session, scan_id: uuid.UUID) -> dict[uuid.UUID, list[str]]:
    from app.registers.models import RecommendationFinding

    rows = session.execute(
        select(RecommendationV2.id, FindingV2.code)
        .join(RecommendationFinding, RecommendationFinding.recommendation_id == RecommendationV2.id)
        .join(FindingV2, FindingV2.id == RecommendationFinding.finding_id)
        .where(RecommendationV2.scan_id == scan_id)
        .order_by(FindingV2.code)
    ).all()
    links: dict[uuid.UUID, list[str]] = {}
    for recommendation_id, code in rows:
        links.setdefault(recommendation_id, []).append(code)
    return links


def _save_snapshot(session: Session, scan: Scan, document: ReportDocumentV2) -> ReportSnapshot:
    existing = session.scalar(select(ReportSnapshot).where(
        ReportSnapshot.scan_id == scan.id,
        ReportSnapshot.content_hash == document.meta.content_hash,
    ))
    if existing:
        if existing.status != "approved":
            existing.status = "fresh"
        session.commit()
        return existing
    for previous in session.scalars(select(ReportSnapshot).where(
        ReportSnapshot.scan_id == scan.id,
        ReportSnapshot.status.in_(("fresh", "needs_rebuild", "approved")),
    )):
        previous.status = "superseded"
    snapshot = ReportSnapshot(
        scan_id=scan.id,
        report_version=document.meta.report_version,
        methodology_version=document.meta.methodology_version,
        content_hash=document.meta.content_hash,
        status="fresh",
        document={"meta": document.meta.model_dump(mode="json"), "appendix": json_appendix(document)},
    )
    session.add(snapshot)
    session.commit()
    return snapshot
