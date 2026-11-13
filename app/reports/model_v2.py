"""Report document model (report_v2).

The client report is a projection of the registers, not a calculation. Keeping
the document generic (`Section` + `Block`) means new sections do not require
model changes, and the same document renders to HTML, PDF and JSON.

Hard invariants live in `validate_document` and are checked on every build:
no aggregate score anywhere, every labelled statement carries references, and
all mandatory sections exist.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

REPORT_VERSION = "report_v2"
METHODOLOGY_VERSION = "socode_v1.1"

REQUIRED_SECTION_IDS = tuple(str(i) for i in range(1, 18))
REQUIRED_APPENDIX_IDS = ("A", "B", "C", "D")

ASSERTION_STATUSES = ("F", "I", "H", "R", "U")

DISCLAIMER = (
    "Этот документ не подтверждает реальное поведение клиентов, профессиональную "
    "эффективность практики, доходы, конверсию или готовность платить. Состояние "
    "бренда установлено в пределах исследованного публичного корпуса; непроверенное "
    "обозначено как неизвестное, а не как отрицательная оценка."
)

BlockType = Literal["paragraph", "table", "callout", "kv", "status"]
CalloutVariant = Literal["note", "warning", "blocker", "methodology"]


class Column(BaseModel):
    key: str
    label: str
    align: Literal["left", "right", "center"] = "left"


class Block(BaseModel):
    type: BlockType
    text: str | None = None
    status: str | None = None
    refs: list[str] = Field(default_factory=list)
    caption: str | None = None
    columns: list[Column] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    note: str | None = None
    variant: CalloutVariant | None = None
    title: str | None = None
    items: list[dict[str, str]] = Field(default_factory=list)


class DataGapV2(BaseModel):
    section: str
    missing: str
    why: str
    impact: str = ""


class Section(BaseModel):
    id: str
    title: str
    anchor: str
    blocks: list[Block] = Field(default_factory=list)
    data_gaps: list[DataGapV2] = Field(default_factory=list)


class Coverage(BaseModel):
    scored: int = 0
    na: int = 0
    label: str = "покрытие данных, не качество бренда"


class ReportMeta(BaseModel):
    report_id: str
    report_version: str = REPORT_VERSION
    methodology_version: str = METHODOLOGY_VERSION
    brand: str
    entry_url: str | None = None
    audit_mode: str = "desk"
    audit_phase: str = "draft"
    report_status: str = "internal_draft"
    snapshot_date: date | None = None
    generated_at: datetime
    scan_id: str
    scan_status: str = ""
    corpus_documents: int = 0
    corpus_blocks: int = 0
    disclaimer: str = DISCLAIMER
    content_hash: str = ""


class ReportDocumentV2(BaseModel):
    meta: ReportMeta
    sections: list[Section] = Field(default_factory=list)
    appendices: list[Section] = Field(default_factory=list)
    coverage: Coverage = Field(default_factory=Coverage)

    def section(self, section_id: str) -> Section:
        for item in self.sections:
            if item.id == section_id:
                return item
        raise KeyError(section_id)


def document_hash(meta: ReportMeta, sections: list[Section], appendices: list[Section]) -> str:
    """Content hash that ignores generation time, so rebuilds stay idempotent."""
    payload = {
        "report_version": meta.report_version,
        "methodology_version": meta.methodology_version,
        "brand": meta.brand,
        "entry_url": meta.entry_url,
        "audit_mode": meta.audit_mode,
        "audit_phase": meta.audit_phase,
        "report_status": meta.report_status,
        "scan_id": meta.scan_id,
        "snapshot_date": meta.snapshot_date.isoformat() if meta.snapshot_date else None,
        "sections": [section.model_dump(mode="json") for section in sections],
        "appendices": [appendix.model_dump(mode="json") for appendix in appendices],
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def report_id_for(scan_id: uuid.UUID | str, content_hash: str) -> str:
    return f"narrative-audit-{str(scan_id)[:8]}-{content_hash[:8]}"


def validate_document(document: ReportDocumentV2) -> list[str]:
    """Return hard violations. An empty list means the document may be stored."""
    problems: list[str] = []
    ids = [section.id for section in document.sections]
    missing = [sid for sid in REQUIRED_SECTION_IDS if sid not in ids]
    if missing:
        problems.append(f"missing sections: {', '.join(missing)}")
    appendix_ids = [section.id for section in document.appendices]
    missing_appendix = [sid for sid in REQUIRED_APPENDIX_IDS if sid not in appendix_ids]
    if missing_appendix:
        problems.append(f"missing appendices: {', '.join(missing_appendix)}")

    for section in [*document.sections, *document.appendices]:
        for block in section.blocks:
            if block.status and block.status not in ASSERTION_STATUSES:
                problems.append(f"section {section.id}: unknown assertion status {block.status!r}")
            if block.status in ("F", "I", "H", "R") and not block.refs:
                problems.append(f"section {section.id}: {block.status} block without references")
            lowered = (block.text or "").lower()
            if "storyaudit_index" in lowered or "индекс нарративного капитала" in lowered:
                problems.append(f"section {section.id}: aggregate index leaked into the report")

    if _contains_aggregate_score(document):
        problems.append("aggregate score field found in report payload")
    return problems


def _contains_aggregate_score(document: ReportDocumentV2) -> bool:
    payload = document.model_dump(mode="json")
    forbidden = {"index", "index_value", "aggregate", "capital", "capital_value", "score_total"}
    return _scan_keys(payload, forbidden)


def _scan_keys(value: Any, forbidden: set[str]) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in forbidden:
                return True
            if _scan_keys(item, forbidden):
                return True
    elif isinstance(value, list):
        return any(_scan_keys(item, forbidden) for item in value)
    return False


def json_appendix(document: ReportDocumentV2) -> dict[str, Any]:
    return {
        "report_version": document.meta.report_version,
        "methodology_version": document.meta.methodology_version,
        "report_id": document.meta.report_id,
        "scan_id": document.meta.scan_id,
        "brand": document.meta.brand,
        "audit_mode": document.meta.audit_mode,
        "audit_phase": document.meta.audit_phase,
        "report_status": document.meta.report_status,
        "content_hash": document.meta.content_hash,
        "coverage": document.coverage.model_dump(),
        "sections": [
            {
                "id": section.id,
                "title": section.title,
                "data_gaps": [gap.model_dump() for gap in section.data_gaps],
            }
            for section in document.sections
        ],
    }
