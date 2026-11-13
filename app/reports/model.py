"""Report document model (report_v1).

A report is built from stored scan results into this document, then rendered to
HTML/PDF. Keeping the document explicit gives us three things at once: the PDF,
a machine-readable JSON appendix, and a stable hash for idempotent caching.

Section availability is part of the model: whatever the current methodology
cannot compute yet is reported as an explicit `DataGap` instead of being
silently omitted or, worse, invented.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

REPORT_VERSION = "report_v1"
SUPPORTED_TYPES = ("free", "full")
ReportType = Literal["free", "full"]


class Indicator(BaseModel):
    code: str
    label: str
    value: float | None = None
    scale: str = "score"
    display: str = "н/д"
    interpretation: str = ""
    available: bool = True


class Summary(BaseModel):
    headline: str
    diagnosis: str
    decision: str
    indicators: list[Indicator] = Field(default_factory=list)
    confidence: str = ""
    disclaimer: str = ""


class CorpusRow(BaseModel):
    label: str
    name: str
    channel: str
    url: str | None = None
    documents: int = 0
    blocks: int = 0
    note: str = ""


class Corpus(BaseModel):
    rows: list[CorpusRow] = Field(default_factory=list)
    total_documents: int = 0
    total_blocks: int = 0
    total_propositions: int = 0
    channels_present: list[str] = Field(default_factory=list)
    channels_missing: list[str] = Field(default_factory=list)
    coverage_note: str = ""


class RoleRow(BaseModel):
    role: str
    current: str
    target: str = "н/д"


class StageRow(BaseModel):
    stage: str
    presence: str
    observation: str = ""


class Architecture(BaseModel):
    greimas: list[RoleRow] = Field(default_factory=list)
    campbell: list[StageRow] = Field(default_factory=list)
    sb7: list[StageRow] = Field(default_factory=list)
    summary: str = ""


class Observation(BaseModel):
    number: str
    title: str
    quote: str
    locator: str = ""
    diagnosis: str = ""
    consequence: str = ""
    action: str = ""


class GapRow(BaseModel):
    code: str
    name: str
    sources: str = ""
    severity: str
    severity_label: str = ""
    confidence: str = "н/д"
    priority: str = "P2"
    note: str = ""


class EvidenceRef(BaseModel):
    label: str
    quote: str
    locator: str = ""


class FindingBlock(BaseModel):
    code: str
    title: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
    method: str = ""
    finding: str = ""
    confidence: str = "н/д"
    consequence: str = ""
    action: str = ""
    verification: str = ""


class ContradictionRow(BaseModel):
    question: str
    values: list[str] = Field(default_factory=list)
    verdict: str = ""


class CapitalAsset(BaseModel):
    code: str
    label: str
    value: float | None = None
    display: str = "н/д"
    interpretation: str = ""
    recommendation: str = ""


class ActionRow(BaseModel):
    priority: str
    timeframe: str
    owner: str
    action: str
    done_criteria: str


class MeasurementRow(BaseModel):
    hypothesis: str
    method: str
    metric: str


class QualityRow(BaseModel):
    item: str
    value: str


class DataGap(BaseModel):
    section: str
    missing: str
    why: str
    impact: str = ""


class ReportMeta(BaseModel):
    report_id: str
    report_type: ReportType
    report_version: str = REPORT_VERSION
    generated_at: datetime
    scan_id: str
    brand: str
    entry_url: str | None = None
    scan_created_at: datetime
    scan_completed_at: datetime | None = None
    methodology_version: str = ""
    formula_version: str = ""
    model_version: str = ""
    scan_status: str = ""
    snapshot_hash: str = ""
    data_completeness: float = 0.0


class ReportDocument(BaseModel):
    meta: ReportMeta
    summary: Summary
    corpus: Corpus
    architecture: Architecture
    quantitative: list[Indicator] = Field(default_factory=list)
    gaps: list[GapRow] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    findings: list[FindingBlock] = Field(default_factory=list)
    contradictions: list[ContradictionRow] = Field(default_factory=list)
    capitals: list[CapitalAsset] = Field(default_factory=list)
    action_plan: list[ActionRow] = Field(default_factory=list)
    measurement: list[MeasurementRow] = Field(default_factory=list)
    quality: list[QualityRow] = Field(default_factory=list)
    data_gaps: list[DataGap] = Field(default_factory=list)
    appendix: dict[str, Any] = Field(default_factory=dict)

    def json_appendix(self) -> dict[str, Any]:
        return {
            "report_version": self.meta.report_version,
            "report_type": self.meta.report_type,
            "brand": self.meta.brand,
            "scan_id": self.meta.scan_id,
            "generated_at": self.meta.generated_at.isoformat(),
            "snapshot_hash": self.meta.snapshot_hash,
            "data_completeness": self.meta.data_completeness,
            "indicators": {row.code: (None if row.value is None else round(row.value, 2)) for row in self.summary.indicators},
            "findings": [
                {
                    "finding_id": item.code,
                    "title": item.title,
                    "confidence": item.confidence,
                    "evidence": [e.quote for e in item.evidence],
                    "action": item.action,
                }
                for item in self.findings
            ],
            "gaps": [{"id": gap.code, "name": gap.name, "severity": gap.severity, "priority": gap.priority} for gap in self.gaps],
            "data_gaps": [gap.section for gap in self.data_gaps],
        }


def report_hash(document: ReportDocument) -> str:
    import hashlib
    import json

    payload = json.dumps(document.json_appendix(), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def report_id_for(scan_id: uuid.UUID, report_type: ReportType, snapshot: str) -> str:
    return f"{report_type}-{scan_id}-{snapshot[:8]}"
