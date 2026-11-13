"""Render report_v2 to HTML and PDF.

The HTML and PDF are produced from the same document; the PDF keeps the footer
with page numbers and honours the CSS `@page appendix` landscape rule.
"""

from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pypdf import PdfReader
from markupsafe import Markup

from app.reports.model_v2 import ReportDocumentV2, json_appendix
from app.reports.render import html_to_pdf

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "report_v2"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

logger = logging.getLogger(__name__)

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)

STATUS_LABELS = {
    "F": "Факт",
    "I": "Интерпретация",
    "H": "Гипотеза",
    "R": "Рекомендация",
    "U": "Неизвестно",
}


def render_html(
    document: ReportDocumentV2,
    inline_css: bool = False,
    page_numbers: dict[str, int] | None = None,
) -> str:
    template = _env.get_template("report.html")
    css = ""
    if inline_css:
        # page.set_content() has no base URL, so a linked stylesheet would not
        # resolve and Chromium would fall back to Times.
        css = Markup((STATIC_DIR / "report_v2.css").read_text(encoding="utf-8"))
    return template.render(
        doc=document,
        status_labels=STATUS_LABELS,
        json_payload=json.dumps(json_appendix(document), ensure_ascii=False, indent=2),
        inline_css=css,
        page_numbers=page_numbers or {},
    )


def render_pdf(document: ReportDocumentV2) -> bytes:
    # Chromium cannot evaluate CSS target-counter(). Use a draft PDF to locate
    # section headings, then print the same layout with a populated TOC.
    draft_html = render_html(document, inline_css=True)
    draft_pdf = html_to_pdf(draft_html, _footer(document), honor_css_page_size=True)
    try:
        page_numbers = _find_section_pages(draft_pdf, document)
    except Exception as exc:  # a TOC enhancement must not make PDF unavailable
        logger.warning("Could not resolve report TOC pages: %s", exc)
        return draft_pdf
    if not page_numbers:
        return draft_pdf
    html = render_html(document, inline_css=True, page_numbers=page_numbers)
    return html_to_pdf(html, _footer(document), honor_css_page_size=True)


def _footer(document: ReportDocumentV2) -> str:
    return (
        '<div style="width:100%;font-size:8px;color:#8b98a5;'
        'font-family:Segoe UI,Arial,sans-serif;padding:0 12mm;'
        'display:flex;justify-content:space-between;">'
        f"<span>SoCode.ru · Narrative Audit · {document.meta.brand}</span>"
        '<span>стр. <span class="pageNumber"></span> / <span class="totalPages"></span></span>'
        "</div>"
    )


def _find_section_pages(pdf: bytes, document: ReportDocumentV2) -> dict[str, int]:
    reader = PdfReader(io.BytesIO(pdf))
    page_texts = [re.sub(r"\s+", " ", page.extract_text() or " ").strip() for page in reader.pages]
    result: dict[str, int] = {}
    # Page 1 is the cover and page 2 is the TOC itself; searching there first
    # would map every section to the TOC instead of its actual heading.
    for section in [*document.sections, *document.appendices]:
        needle = re.sub(r"\s+", " ", f"{section.id} {section.title}").strip()
        for page_number, text in enumerate(page_texts[2:], start=3):
            if needle in text:
                result[section.anchor] = page_number
                break
    return result
