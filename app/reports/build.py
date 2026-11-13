"""Build a report document from stored scan results."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import AppError
from app.models import (
    Annotation,
    Block,
    Capital,
    Document,
    Finding,
    FindingEvidence,
    IndexResult,
    Job,
    Metric,
    Proposition,
    Recommendation,
    Scan,
    Source,
    SourceProfile,
)
from app.reports.model import (
    ActionRow,
    Architecture,
    CapitalAsset,
    ContradictionRow,
    Corpus,
    CorpusRow,
    DataGap,
    EvidenceRef,
    FindingBlock,
    GapRow,
    Indicator,
    MeasurementRow,
    Observation,
    QualityRow,
    ReportDocument,
    ReportMeta,
    ReportType,
    RoleRow,
    Summary,
    report_hash,
    report_id_for,
)
from app.utils import stable_hash

METRIC_LABELS = {
    "narrative_clarity": "Narrative clarity",
    "narrative_coherence": "Narrative coherence",
    "narrative_depth": "Narrative depth",
    "transformation_completeness": "Transformation completeness",
}
CAPITAL_LABELS = {
    "meaning_capital": "Meaning capital",
    "change_capital": "Change capital",
}
GAP_MAP = {
    "LOW_CLARITY": ("Meaning Gap", "GAP-01"),
    "LOW_COHERENCE": ("Coherence Gap", "GAP-04"),
    "LOW_DEPTH": ("Meaning Depth Gap", "GAP-05"),
    "LOW_TRANSFORMATION": ("Transformation Gap", "GAP-03"),
}
ROLE_LABELS = {
    "hero": "Субъект (герой)",
    "object": "Объект (цель)",
    "problem": "Отправитель (проблема)",
    "brand_role": "Помощник (роль бренда)",
    "customer": "Получатель",
}
CHANNEL_LABELS = {"website": "Сайт"}
SEVERITY_LABELS = {"high": "высокая", "medium": "средняя", "low": "низкая"}
EXPECTED_CHANNELS = ["website", "linkedin", "pitch", "case", "review"]

FREE_OBSERVATION_LIMIT = 3
# Share of format-1.0 sections that the current stack can fill honestly.
DATA_COMPLETENESS = {"free": 0.55, "full": 0.55}
MAX_FINDINGS = 12


def _label_text(label: dict | None) -> str:
    if not label:
        return "не определено"
    value = str(label.get("display_label") or label.get("label") or "").strip()
    if not value or value == "unknown":
        return "не определено"
    return value


def _scan_or_404(session: Session, scan_id: uuid.UUID) -> Scan:
    scan = session.get(Scan, scan_id)
    if not scan:
        raise AppError(404, "not_found", "Scan not found")
    return scan


def _source_stats(session: Session, scan_id: uuid.UUID) -> tuple[list[CorpusRow], int, int, int]:
    sources = list(session.scalars(select(Source).where(Source.scan_id == scan_id).order_by(Source.created_at, Source.id)))
    rows: list[CorpusRow] = []
    total_documents = total_blocks = total_propositions = 0
    for index, source in enumerate(sources, start=1):
        document_ids = list(session.scalars(select(Document.id).where(Document.source_id == source.id)))
        blocks = 0
        propositions = 0
        if document_ids:
            blocks = int(session.scalar(select(func.count(Block.id)).where(Block.document_id.in_(document_ids))) or 0)
            propositions = int(session.scalar(
                select(func.count(Proposition.id)).join(Block, Proposition.block_id == Block.id).where(Block.document_id.in_(document_ids))
            ) or 0)
        url = session.scalar(select(Document.url).where(Document.source_id == source.id).limit(1))
        rows.append(CorpusRow(
            label=f"SRC-{index:02d}",
            name=source.name,
            channel=source.type,
            url=url or source.source_metadata.get("entry_url"),
            documents=len(document_ids),
            blocks=blocks,
            note=f"{propositions} propositions",
        ))
        total_documents += len(document_ids)
        total_blocks += blocks
        total_propositions += propositions
    return rows, total_documents, total_blocks, total_propositions


def _indicators(session: Session, scan_id: uuid.UUID) -> tuple[list[Indicator], list[CapitalAsset]]:
    metrics = list(session.scalars(select(Metric).where(Metric.scan_id == scan_id, Metric.is_current.is_(True)).order_by(Metric.code)))
    indicators: list[Indicator] = []
    for metric in metrics:
        value = float(metric.value)
        indicators.append(Indicator(
            code=metric.code,
            label=METRIC_LABELS.get(metric.code, metric.code),
            value=value,
            scale="score",
            display=f"{value:.1f}/100",
            interpretation=_metric_interpretation(metric.code, value),
        ))

    capitals: list[CapitalAsset] = []
    for capital in session.scalars(select(Capital).where(Capital.scan_id == scan_id, Capital.is_current.is_(True)).order_by(Capital.code)):
        value = float(capital.value)
        capitals.append(CapitalAsset(
            code=capital.code,
            label=CAPITAL_LABELS.get(capital.code, capital.code),
            value=value,
            display=f"{value:.1f}/100",
            interpretation="",
        ))
        indicators.append(Indicator(
            code=capital.code,
            label=CAPITAL_LABELS.get(capital.code, capital.code),
            value=value,
            display=f"{value:.1f}/100",
            interpretation="",
        ))

    index = session.scalar(select(IndexResult).where(IndexResult.scan_id == scan_id, IndexResult.is_current.is_(True)))
    if index:
        value = float(index.value)
        indicators.insert(0, Indicator(
            code="storyaudit_index",
            label="StoryAudit Index",
            value=value,
            display=f"{int(round(value))}/100",
            interpretation="Композитный индекс версии formulas_v1 (0.6 × meaning + 0.4 × change).",
        ))
    return indicators, capitals


def _metric_interpretation(code: str, value: float) -> str:
    thresholds = {
        "narrative_clarity": (40, 60, "низкая", "средняя"),
        "narrative_coherence": (40, 70, "низкая", "средняя"),
        "narrative_depth": (25, 50, "низкая", "средняя"),
        "transformation_completeness": (40, 60, "низкая", "средняя"),
    }
    low, medium, low_word, mid_word = thresholds.get(code, (40, 60, "низкая", "средняя"))
    if value < low:
        return f"{low_word} относительно порога {low}"
    if value < medium:
        return f"{mid_word} относительно порога {medium}"
    return "выше среднего порога"


def _profile(session: Session, scan_id: uuid.UUID) -> dict:
    profiles = list(session.scalars(select(SourceProfile).where(SourceProfile.scan_id == scan_id, SourceProfile.is_current.is_(True))))
    merged: dict = {}
    for profile in profiles:
        for key, value in profile.payload.items():
            if key in {"input_hash"}:
                continue
            if isinstance(value, dict) and "weight" in value:
                current = merged.get(key)
                if not current or float(value.get("weight", 0)) > float(current.get("weight", 0)):
                    merged[key] = value
            elif isinstance(value, list):
                merged.setdefault(key, [])
                merged[key].extend(value)
    return merged


def _conflict(session: Session, scan_id: uuid.UUID) -> dict:
    annotation = session.scalar(
        select(Annotation).where(
            Annotation.scan_id == scan_id, Annotation.agent == "A3",
            Annotation.is_current.is_(True),
        ).order_by(Annotation.created_at.desc())
    )
    if not annotation:
        return {}
    return annotation.payload.get("conflict", {}) or {}


def _architecture(profile: dict, conflict: dict) -> Architecture:
    current = {
        "hero": _label_text(profile.get("hero")),
        "object": _label_text(profile.get("object")),
        "problem": _label_text(profile.get("problem")),
        "brand_role": _label_text(profile.get("brand_role")),
        "customer": _label_text(profile.get("customer")),
    }
    greimas = [
        RoleRow(role=ROLE_LABELS["hero"], current=current["hero"]),
        RoleRow(role=ROLE_LABELS["object"], current=current["object"]),
        RoleRow(role=ROLE_LABELS["problem"], current=current["problem"]),
        RoleRow(role=ROLE_LABELS["brand_role"], current=current["brand_role"]),
        RoleRow(
            role="Оппонент",
            current=(conflict.get("description") or "не определён") if conflict.get("present") else "не определён",
        ),
        RoleRow(role=ROLE_LABELS["customer"], current=current["customer"]),
    ]
    summary = (
        "Карта ролей собрана из текущих аннотаций A2/A3. Целевая колонка требует "
        "утверждённой модели целевого нарратива (registry v2) и в этой версии не заполняется."
    )
    return Architecture(
        greimas=greimas,
        campbell=[],
        sb7=[],
        summary=summary,
    )


def _findings(session: Session, scan_id: uuid.UUID) -> tuple[list[FindingBlock], list[GapRow], list[Observation]]:
    findings = list(session.scalars(
        select(Finding).where(Finding.scan_id == scan_id, Finding.is_current.is_(True)).order_by(Finding.severity, Finding.code)
    ))[:MAX_FINDINGS]
    blocks: list[FindingBlock] = []
    gaps: list[GapRow] = []
    observations: list[Observation] = []
    for index, finding in enumerate(findings, start=1):
        metric = session.get(Metric, finding.metric_id)
        evidence_ids = list(session.scalars(select(FindingEvidence.proposition_id).where(FindingEvidence.finding_id == finding.id)))
        evidence: list[EvidenceRef] = []
        for proposition_id in evidence_ids:
            row = session.execute(
                select(Proposition, Block, Document, Source)
                .join(Block, Proposition.block_id == Block.id)
                .join(Document, Block.document_id == Document.id)
                .join(Source, Document.source_id == Source.id)
                .where(Proposition.id == proposition_id)
            ).first()
            if not row:
                continue
            proposition, block, document, source = row
            evidence.append(EvidenceRef(
                label=f"{source.name} / {document.title} / block-{block.ordinal + 1}",
                quote=" ".join(proposition.text.split()),
                locator=f"block {block.id}",
            ))
        recommendation = session.scalar(select(Recommendation).where(Recommendation.finding_id == finding.id))
        gap_name, gap_code = GAP_MAP.get(finding.code, (finding.title, "GAP-00"))
        blocks.append(FindingBlock(
            code=f"FIND-{index:02d}",
            title=finding.title,
            evidence=evidence,
            method=f"Правило расчёта metrics/{metric.code if metric else 'unknown'} (formulas_v1)",
            finding=finding.explanation,
            consequence=(recommendation.expected_effect if recommendation else ""),
            action=(recommendation.action if recommendation else ""),
            verification="Метрика должна вырасти выше среднего порога при повторном аудите того же корпуса.",
        ))
        gaps.append(GapRow(
            code=gap_code,
            name=gap_name,
            sources=", ".join(sorted({item.label.split(" / ")[0] for item in evidence})) or "н/д",
            severity=finding.severity,
            severity_label=SEVERITY_LABELS.get(finding.severity, finding.severity),
            priority="P0" if finding.severity == "high" else "P1",
            note=f"metric: {metric.code}" if metric else "",
        ))
        if evidence:
            observations.append(Observation(
                number=f"{index:02d}",
                title=finding.title,
                quote=evidence[0].quote,
                locator=evidence[0].label,
                diagnosis=finding.explanation,
                consequence=(recommendation.expected_effect if recommendation else ""),
                action=(recommendation.action if recommendation else ""),
            ))
    return blocks, gaps, observations[:FREE_OBSERVATION_LIMIT]


def _totals(session: Session, scan_id: uuid.UUID) -> dict[str, int]:
    annotations = int(session.scalar(select(func.count(Annotation.id)).where(Annotation.scan_id == scan_id)) or 0)
    jobs = int(session.scalar(select(func.count(Job.id)).where(Job.scan_id == scan_id)) or 0)
    return {"annotations": annotations, "jobs": jobs}


def _action_plan(gaps: list[GapRow]) -> list[ActionRow]:
    plan: list[ActionRow] = []
    if any(gap.code == "GAP-01" for gap in gaps):
        plan.append(ActionRow(
            priority="P0", timeframe="Недели 1–2", owner="CMO + Product Marketing",
            action="Согласовать героя, объект и обещание; переписать первый экран вокруг результата клиента.",
            done_criteria="Единая формулировка ролей и обещания, проверенная на достоверность продукта.",
        ))
    if any(gap.code == "GAP-03" for gap in gaps):
        plan.append(ActionRow(
            priority="P0", timeframe="Недели 2–4", owner="Web + Content",
            action="Добавить явное «до → после» клиента на главной и страницах решения.",
            done_criteria="На странице видны исходное препятствие и состояние клиента после внедрения.",
        ))
    if any(gap.code in {"GAP-04", "GAP-05"} for gap in gaps):
        plan.append(ActionRow(
            priority="P1", timeframe="Недели 4–8", owner="Content + Social",
            action="Синхронизировать каналы с единой нарративной архитектурой, убрать противоречия ролей.",
            done_criteria="Повторная разметка не находит критических противоречий между каналами.",
        ))
    if not plan:
        plan.append(ActionRow(
            priority="P2", timeframe="Недели 1–4", owner="Marketing",
            action="Поддерживать текущую структуру сообщений и собирать новые источники для расширенного аудита.",
            done_criteria="Корпус расширен минимум до трёх каналов.",
        ))
    plan.append(ActionRow(
        priority="P2", timeframe="Недели 10–12", owner="CMO",
        action="Повторный аудит на новом корпусе и сравнение метрик с текущим срезом.",
        done_criteria="Сопоставимые источники и рассчитанная дельта метрик.",
    ))
    return plan


def _measurement() -> list[MeasurementRow]:
    return [
        MeasurementRow(
            hypothesis="Клиенты быстрее понимают предложение",
            method="Comprehension / 5-second test на первом экране",
            metric="Доля верно понявших ценность",
        ),
        MeasurementRow(
            hypothesis="Изменённое сообщение увеличивает первый шаг",
            method="A/B тест страницы с baseline до изменений",
            metric="Conversion rate / demo requests",
        ),
        MeasurementRow(
            hypothesis="Единая история снижает расхождение каналов",
            method="Повторная разметка корпуса тем же registry",
            metric="Coherence, Hero Ratio",
        ),
    ]


def _data_gaps(single_channel: bool) -> list[DataGap]:
    gaps = [
        DataGap(
            section="Narrative Capital Index",
            missing="Композиты NCI версии 2.0: Meaning, Participation, Reputation, Campbell, Greimas, StoryBrand, Hero Ratio, Ego Index.",
            why="В formulas_v1 реализованы 4 метрики и 2 капитала; registry v2 не утверждён.",
            impact="Отчёт показывает индекс версии v1, несопоставимый с примерами формата 1.0.",
        ),
        DataGap(
            section="Количественный профиль",
            missing="Подсчёт предложений с брендом/клиентом в роли субъекта, Hero Ratio, Ego Index.",
            why="Нет стадии атрибуции субъекта на уровне предложения.",
            impact="Раздел 3 отчёта остаётся пустым, Hero Ratio недоступен.",
        ),
        DataGap(
            section="Кэмпбелл и StoryBrand SB7",
            missing="Присутствие стадий пути героя и элементов SB7 по каналам.",
            why="Агенты не размечают стадии; доступны только роли Греймаса из A2/A3.",
            impact="Нарративная архитектура описана частично.",
        ),
        DataGap(
            section="Gap Register",
            missing="Полная таксономия разрывов (Promise–Proof, Channel Consistency, Plan, Reputation) с confidence.",
            why="Findings формируются только по порогам 4 метрик, без поля confidence.",
            impact="Реестр разрывов короче и без приоритезации по уверенности.",
        ),
    ]
    if single_channel:
        gaps.append(DataGap(
            section="Межканальная матрица",
            missing="Сравнение сайта, LinkedIn, презентации, кейсов и отзывов.",
            why="MVP принимает только сайт (sources.type = website).",
            impact="Противоречия между каналами не измеряются.",
        ))
    gaps.append(DataGap(
        section="Rewriting Engine",
        missing="Три варианта переписывания (conservative / balanced / aggressive) с guardrails.",
        why="Агент переписывания не реализован.",
        impact="В отчёте есть только действие рекомендации, но нет готовых формулировок.",
    ))
    return gaps


def build_report(session: Session, scan_id: uuid.UUID, report_type: ReportType = "free") -> ReportDocument:
    if report_type not in {"free", "full"}:
        raise AppError(422, "validation_error", "report_type must be 'free' or 'full'")
    scan = _scan_or_404(session, scan_id)
    if scan.status != "succeeded":
        raise AppError(409, "conflict", f"Отчёт доступен только для завершённого scan (status={scan.status})")
    if scan.current_calculation_run_id is None:
        raise AppError(409, "conflict", "У scan нет актуального calculation run")

    corpus_rows, total_documents, total_blocks, total_propositions = _source_stats(session, scan_id)
    indicators, capitals = _indicators(session, scan_id)
    profile = _profile(session, scan_id)
    conflict = _conflict(session, scan_id)
    architecture = _architecture(profile, conflict)
    findings, gaps, observations = _findings(session, scan_id)
    totals = _totals(session, scan_id)

    channels_present = sorted({row.channel for row in corpus_rows})
    single_channel = len(channels_present) <= 1
    data_gaps = _data_gaps(single_channel)

    for asset in capitals:
        if asset.code == "meaning_capital":
            asset.interpretation = "Насколько ясно и глубоко выражена смысловая основа сообщений."
            asset.recommendation = "Собрать обещание в связку «проблема → результат → механизм»."
        else:
            asset.interpretation = "Насколько последовательно и полно показана трансформация клиента."
            asset.recommendation = "Явно описать состояния «до» и «после» с проверяемыми деталями."

    index = next((row for row in indicators if row.code == "storyaudit_index"), None)
    weakest = min(indicators, key=lambda row: row.value) if indicators and any(r.value is not None for r in indicators) else None
    headline = (
        f"Индекс StoryAudit {index.display}, слабейший показатель — {weakest.label.lower()} ({weakest.display})."
        if index and weakest else "Данных недостаточно для главного вывода."
    )
    diagnosis_parts = [f"Проанализировано {total_documents} документ(ов) с {total_blocks} блоками"]
    if conflict.get("present"):
        diagnosis_parts.append("в сообщениях зафиксирован конфликт")
    else:
        diagnosis_parts.append("явный конфликт в сообщениях не зафиксирован")
    diagnosis = "; ".join(diagnosis_parts) + "."
    decision = (
        f"Сфокусироваться на «{weakest.label.lower()}» — это самый низкий показатель корпуса."
        if weakest else "Расширить корпус и повторить аудит."
    )

    summary = Summary(
        headline=headline,
        diagnosis=diagnosis,
        decision=decision,
        indicators=indicators,
        confidence="Расчёт детерминирован (formulas_v1) поверх LLM-аннотаций; интерпретация требует проверки.",
        disclaimer=(
            "Отчёт построен на реальных данных этого scan. Разделы, недоступные в текущей "
            "методологии, перечислены в «Ограничения данных» и не заполнены вымышленными значениями."
        ),
    )

    contradictions: list[ContradictionRow] = []
    if not single_channel:
        contradictions.append(ContradictionRow(question="Кто герой?", values=[], verdict=""))

    observation_limit = FREE_OBSERVATION_LIMIT if report_type == "free" else len(observations)
    document = ReportDocument(
        meta=ReportMeta(
            report_id="pending",
            report_type=report_type,
            generated_at=datetime.now(timezone.utc),
            scan_id=str(scan.id),
            brand=(corpus_rows[0].name if corpus_rows else scan.title),
            entry_url=(corpus_rows[0].url if corpus_rows else None),
            scan_created_at=scan.created_at,
            scan_completed_at=scan.completed_at,
            methodology_version=scan.methodology_version,
            formula_version="formulas_v1",
            model_version=scan.model_version,
            scan_status=scan.status,
            data_completeness=DATA_COMPLETENESS[report_type],
        ),
        summary=summary,
        corpus=Corpus(
            rows=corpus_rows,
            total_documents=total_documents,
            total_blocks=total_blocks,
            total_propositions=total_propositions,
            channels_present=[CHANNEL_LABELS.get(channel, channel) for channel in channels_present],
            channels_missing=[CHANNEL_LABELS.get(channel, channel) for channel in EXPECTED_CHANNELS if channel not in channels_present],
            coverage_note=(
                "Один канал (сайт). FULL-аудит по формату 1.0 требует минимум 3 канала: "
                "сайт, соцсети и кейсы/презентации."
            ),
        ),
        architecture=architecture,
        quantitative=[
            Indicator(code="documents", label="Documents", value=float(total_documents), scale="count", display=str(total_documents)),
            Indicator(code="blocks", label="Blocks", value=float(total_blocks), scale="count", display=str(total_blocks)),
            Indicator(code="propositions", label="Propositions", value=float(total_propositions), scale="count", display=str(total_propositions)),
            Indicator(code="annotations", label="Annotations", value=float(totals["annotations"]), scale="count", display=str(totals["annotations"])),
            Indicator(code="jobs", label="Jobs", value=float(totals["jobs"]), scale="count", display=str(totals["jobs"])),
            Indicator(code="hero_ratio", label="Hero Ratio", value=None, scale="ratio", display="н/д", available=False),
            Indicator(code="ego_index", label="Ego Index", value=None, scale="score", display="н/д", available=False),
        ],
        gaps=gaps,
        observations=observations[:observation_limit],
        findings=findings,
        contradictions=contradictions,
        capitals=capitals,
        action_plan=_action_plan(gaps),
        measurement=_measurement(),
        quality=[
            QualityRow(item="Report version", value="report_v1"),
            QualityRow(item="Formula version", value="formulas_v1"),
            QualityRow(item="Methodology", value=scan.methodology_version),
            QualityRow(item="Model", value=scan.model_version),
            QualityRow(item="Analysis generation", value=str(scan.analysis_generation)),
            QualityRow(item="Scan status", value=scan.status),
            QualityRow(item="Snapshot built at", value=datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
        ],
        data_gaps=data_gaps,
    )

    snapshot = stable_hash({
        "scan": str(scan.id),
        "run": str(scan.current_calculation_run_id),
        "generation": scan.analysis_generation,
        "metrics": [(row.code, row.display) for row in indicators],
        "findings": [(item.code, item.title) for item in findings],
        "report_version": document.meta.report_version,
    })
    document.meta.snapshot_hash = snapshot
    document.meta.report_id = report_id_for(scan.id, report_type, snapshot)
    document.appendix = document.json_appendix()
    document.appendix["content_hash"] = report_hash(document)
    return document
