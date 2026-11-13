"""Render a report document to HTML and PDF."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from app.errors import AppError
from app.reports.model import ReportDocument

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "report"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)

TOC = {
    "free": [
        "Нарратив за 30 секунд",
        "Карта нарративных ролей (Греймас)",
        "Наблюдения по источнику",
        "Предварительная карта разрывов",
        "Возможное изменение",
        "Что делать дальше",
        "Качество данных и ограничения",
    ],
    "full": [
        "Executive Summary",
        "Корпус источников и качество покрытия",
        "Нарративная архитектура",
        "Количественный профиль",
        "Narrative Gap Register",
        "Evidence → Finding → Impact → Action",
        "Межканальная матрица противоречий",
        "Narrative Capital",
        "План действий",
        "Измерение результатов и бизнес-гипотезы",
        "Контроль качества",
        "Приложение: машинно-читаемый фрагмент",
    ],
}

_render_lock = threading.Lock()


def render_html(document: ReportDocument, inline_css: bool = False) -> str:
    template = _env.get_template("report.html")
    css = ""
    if inline_css:
        # page.set_content() has no document base URL, so a <link> would not
        # resolve and Chromium would render unstyled Times. Embed the CSS.
        css = Markup((STATIC_DIR / "report.css").read_text(encoding="utf-8"))
    return template.render(
        doc=document,
        toc=TOC[document.meta.report_type],
        appendix_json=json.dumps(document.appendix, ensure_ascii=False, indent=2),
        inline_css=css,
    )


def render_pdf(document: ReportDocument) -> bytes:
    """Render the report to PDF through Chromium (Playwright).

    A local Chrome install is preferred (`PLAYWRIGHT_CHANNEL=chrome`), otherwise
    Playwright's bundled Chromium is used (`playwright install chromium`).
    """
    html = render_html(document, inline_css=True)
    footer = (
        '<div style="width:100%;font-size:8px;color:#8b98a5;'
        'font-family:Segoe UI,Arial,sans-serif;padding:0 16mm;'
        'display:flex;justify-content:space-between;">'
        f'<span>StoryAudit.ai · {document.meta.report_id}</span>'
        '<span>стр. <span class="pageNumber"></span> / <span class="totalPages"></span></span>'
        "</div>"
    )
    return html_to_pdf(html, footer)


def html_to_pdf(html: str, footer: str | None = None, *, honor_css_page_size: bool = False) -> bytes:
    """Turn a ready HTML document into PDF bytes.

    `honor_css_page_size=True` switches the document to the CSS `@page` rules,
    which is required for mixed portrait/landscape reports (report_v2
    appendices use a named `@page appendix` rule).
    """
    channel = os.environ.get("PLAYWRIGHT_CHANNEL", "chrome").strip() or None
    footer = footer or (
        '<div style="font-size:8px;color:#8b98a5;text-align:center;width:100%;">'
        '<span class="pageNumber"></span> / <span class="totalPages"></span></div>'
    )
    header = "<div></div>"

    with _render_lock:
        restore = _ensure_windows_proactor()
        try:
            pdf = _render_pdf_bytes(html, channel, footer, header, honor_css_page_size)
        finally:
            if restore is not None:
                asyncio.set_event_loop_policy(restore)

    if not pdf.startswith(b"%PDF"):
        raise AppError(500, "render_failed", "PDF renderer produced an invalid file")
    return pdf


def _ensure_windows_proactor():
    """uvicorn switches Windows to the selector loop, which cannot spawn
    subprocesses. Playwright needs the proactor loop to start Chromium."""
    if sys.platform != "win32":
        return None
    policy = asyncio.get_event_loop_policy()
    proactor = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
    if proactor is None or isinstance(policy, proactor):
        return None
    asyncio.set_event_loop_policy(proactor())
    return policy


def _render_pdf_bytes(
    html: str,
    channel: str | None,
    footer: str,
    header: str,
    honor_css_page_size: bool = False,
) -> bytes:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = _launch(playwright, channel)
        try:
            page = browser.new_page(viewport={"width": 1240, "height": 1754})
            page.set_content(html, wait_until="load")
            page.emulate_media(media="print")
            return page.pdf(
                format="A4",
                print_background=True,
                display_header_footer=True,
                header_template=header,
                footer_template=footer,
                margin={"top": "18mm", "bottom": "18mm", "left": "14mm", "right": "14mm"},
                prefer_css_page_size=honor_css_page_size,
            )
        finally:
            browser.close()


def _launch(playwright, channel: str | None):
    from playwright.sync_api import Error as PlaywrightError

    attempts = [{"channel": channel}] if channel else []
    attempts.append({})
    last_error: Exception | None = None
    for kwargs in attempts:
        try:
            return playwright.chromium.launch(args=["--no-sandbox"], **kwargs)
        except PlaywrightError as exc:  # channel missing or browser not installed
            last_error = exc
            continue
    raise AppError(
        500,
        "render_unavailable",
        "Не удалось запустить Chromium для PDF. Установите Chrome или выполните "
        "'playwright install chromium'.",
    ) from last_error
