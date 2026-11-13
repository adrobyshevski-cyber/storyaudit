"""LLM narrative editor with a deterministic guard.

The editor may write prose, but it cannot invent facts:

- every paragraph with status F/I/H/R must reference existing register codes;
- any number with three or more digits must already exist in the registers;
- causal promises and pseudo-index language are rejected;
- on rejection the section falls back to a template built from the registers,
  and the fallback is recorded, never hidden.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.registers.models import (
    AuditHypothesis,
    EvidenceRecord,
    FindingV2,
    Kpi,
    Product,
    RecommendationV2,
    ReportNarrative,
    ScorecardEntry,
    StageGate,
)

BANNED_PATTERNS = (
    (
        re.compile(
            r"гарантиру[а-яё]*\s+(?:[а-яё]+\s+){0,3}(?:результат|исход|эффект|успех)",
            re.IGNORECASE,
        ),
        "обещание гарантированного результата",
    ),
    (re.compile(r"увеличит\s+на\s+\d", re.IGNORECASE), "числовой прогноз"),
    (
        re.compile(r"подтверждено\s+исследованием", re.IGNORECASE),
        "ссылка на несуществующее исследование",
    ),
    (re.compile(r"средн[а-яё]*\s+балл", re.IGNORECASE), "средний балл"),
    (re.compile(r"nci", re.IGNORECASE), "агрегированный индекс"),
    (re.compile(r"(?<!не )уникальн", re.IGNORECASE), "заявление об уникальности"),
)
NARRATIVE_SECTIONS = ("1", "17")
NUMBER_RE = re.compile(r"\d{3,}")


def build_bundle(session: Session, scan_id) -> dict:
    products = list(session.scalars(select(Product).where(Product.scan_id == scan_id)))
    evidence = list(session.scalars(
        select(EvidenceRecord).where(EvidenceRecord.scan_id == scan_id).order_by(EvidenceRecord.code)
    ))
    findings = list(session.scalars(
        select(FindingV2).where(FindingV2.scan_id == scan_id).order_by(FindingV2.code)
    ))
    recommendations = list(session.scalars(
        select(RecommendationV2).where(RecommendationV2.scan_id == scan_id).order_by(RecommendationV2.code)
    ))
    scorecard = list(session.scalars(
        select(ScorecardEntry).where(ScorecardEntry.scan_id == scan_id).order_by(ScorecardEntry.ordinal)
    ))
    kpis = list(session.scalars(select(Kpi).where(Kpi.scan_id == scan_id).order_by(Kpi.code)))
    hypotheses = list(session.scalars(
        select(AuditHypothesis).where(AuditHypothesis.scan_id == scan_id).order_by(AuditHypothesis.code)
    ))
    gate = session.scalar(select(StageGate).where(StageGate.scan_id == scan_id))
    return {
        "products": products,
        "evidence": evidence,
        "findings": findings,
        "recommendations": recommendations,
        "scorecard": scorecard,
        "kpis": kpis,
        "hypotheses": hypotheses,
        "gate": gate,
    }


def allowed_refs(bundle: dict) -> set[str]:
    codes: set[str] = set()
    for product in bundle["products"]:
        codes.add(product.code)
    for collection in ("evidence", "findings", "recommendations", "kpis", "hypotheses"):
        for item in bundle[collection]:
            codes.add(item.code)
    for entry in bundle["scorecard"]:
        codes.update(str(code) for code in (entry.source_codes or []))
    return codes


def numeric_corpus(bundle: dict) -> set[str]:
    chunks: list[str] = []
    for product in bundle["products"]:
        chunks.extend([product.name, product.promise or "", product.price or "", product.boundary or ""])
    for evidence in bundle["evidence"]:
        chunks.append(evidence.claim)
    for finding in bundle["findings"]:
        chunks.append(finding.statement)
    corpus = " ".join(chunks).replace(" ", "").replace("\u00a0", "")
    return set(NUMBER_RE.findall(corpus))


def build_narrative_draft(bundle: dict) -> dict[str, list[dict]]:
    findings = bundle["findings"]
    recommendations = bundle["recommendations"]
    products = bundle["products"]
    gate = bundle["gate"]
    primary = next((product for product in products if product.is_primary), products[0] if products else None)

    summary: list[dict] = [
        {
            "text": (
                "Отчёт фиксирует текущее состояние публичного нарратива: что бренд заявляет, "
                "чем это подтверждено и какие неизвестные остаются. Новое позиционирование "
                "здесь не создаётся — это граница Stage 01."
            ),
            "status": None,
            "refs": [],
        }
    ]
    if findings:
        top = sorted(findings, key=lambda item: (item.priority, item.code))[0]
        summary.append({
            "text": (
                f"Ключевая находка: {top.statement}. Основание и альтернативное объяснение — "
                "в реестре находок."
            ),
            "status": "I",
            "refs": [top.code],
        })
    else:
        summary.append({
            "text": (
                "Проверенных противоречий, пропусков и слабых доказательств в исследованном "
                "корпусе не обнаружено. Это результат проверки, а не отсутствие анализа."
            ),
            "status": "I" if products else "U",
            "refs": [products[0].code] if products else [],
        })
    if gate is not None:
        summary.append({
            "text": (
                f"Решение Stage Gate: {gate.decision}. Проектирование стратегии опирается "
                "на подтверждённые входы; непроверенное обозначено как неизвестное."
            ),
            "status": None,
            "refs": [],
        })
    summary.append({
        "text": (
            "Реальное восприятие клиентов, опыт взаимодействия и конверсия не установлены: "
            "для них нужны интервью и данные заказчика. В режиме Desk это корректный вывод."
        ),
        "status": "U",
        "refs": [],
    })

    conclusion: list[dict] = []
    if primary:
        conclusion.append({
            "text": (
                "Сохранить действующее обещание и не заменять его новым без проверки: "
                f"{primary.promise or primary.name}."
            ),
            "status": "I",
            "refs": [primary.code],
        })
    for decision_class, label in (("fix", "Исправить"), ("test", "Проверить"), ("stop", "Не обещать")):
        selected = [item for item in recommendations if item.decision_class == decision_class]
        if not selected:
            continue
        item = selected[0]
        conclusion.append({
            "text": f"{label}: {item.change}",
            "status": "R",
            "refs": [item.code],
        })
    if not conclusion:
        conclusion.append({
            "text": "Изменений не требуется: доказанных проблем в исследованном корпусе нет.",
            "status": "U",
            "refs": [],
        })
    return {"1": summary, "17": conclusion}


def validate_narrative(
    sections: dict[str, list[dict]],
    *,
    refs: set[str],
    numbers: set[str],
    require_sections: bool = True,
) -> list[str]:
    problems: list[str] = []
    if require_sections:
        missing = [section for section in NARRATIVE_SECTIONS if not sections.get(section)]
        if missing:
            problems.append(f"missing narrative sections: {', '.join(missing)}")
    for section_id, paragraphs in sections.items():
        if section_id not in NARRATIVE_SECTIONS:
            problems.append(f"unexpected narrative section: {section_id}")
        for index, paragraph in enumerate(paragraphs):
            text = str(paragraph.get("text", "")).strip()
            status = paragraph.get("status")
            refs_list = [str(code) for code in paragraph.get("refs", [])]
            where = f"{section_id}[{index}]"
            if not text:
                problems.append(f"{where}: empty text")
                continue
            for pattern, label in BANNED_PATTERNS:
                if pattern.search(text):
                    problems.append(f"{where}: {label}")
            if status in {"F", "I", "H", "R"} and not refs_list:
                problems.append(f"{where}: {status} paragraph without references")
            unknown = [code for code in refs_list if code not in refs]
            if unknown:
                problems.append(f"{where}: unknown references {', '.join(unknown)}")
            for number in NUMBER_RE.findall(text.replace(" ", "").replace("\u00a0", "")):
                if number not in numbers:
                    problems.append(f"{where}: number {number} is not present in registers")
    return problems


def persist_narrative(
    session: Session,
    scan_id,
    sections: dict[str, list[dict]],
    *,
    status: str,
    model_version: str,
) -> int:
    existing = {
        row.section_id: row
        for row in session.scalars(select(ReportNarrative).where(ReportNarrative.scan_id == scan_id))
    }
    for section_id, paragraphs in sections.items():
        row = existing.get(section_id)
        if row is None:
            session.add(ReportNarrative(
                scan_id=scan_id,
                section_id=section_id,
                paragraphs=paragraphs,
                status=status,
                model_version=model_version,
            ))
        else:
            row.paragraphs = paragraphs
            row.status = status
            row.model_version = model_version
    session.flush()
    return len(sections)
