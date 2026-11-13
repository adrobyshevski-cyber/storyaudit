"""Report layer.

`report_v1` (metrics/capitals/index) is kept behind `scans.internal_metrics` for
comparison only. `report_v2` is the single client-facing report.
"""

from app.reports.build import build_report
from app.reports.build_v2 import build_report_v2
from app.reports.model import (
    REPORT_VERSION,
    SUPPORTED_TYPES,
    ReportDocument,
    report_hash,
    report_id_for,
)
from app.reports.model_v2 import REPORT_VERSION as REPORT_VERSION_V2
from app.reports.model_v2 import ReportDocumentV2, validate_document
from app.reports.render import render_html, render_pdf
from app.reports.render_v2 import render_html as render_html_v2
from app.reports.render_v2 import render_pdf as render_pdf_v2

__all__ = [
    "REPORT_VERSION",
    "REPORT_VERSION_V2",
    "SUPPORTED_TYPES",
    "ReportDocument",
    "ReportDocumentV2",
    "build_report",
    "build_report_v2",
    "render_html",
    "render_html_v2",
    "render_pdf",
    "render_pdf_v2",
    "report_hash",
    "report_id_for",
    "validate_document",
]
