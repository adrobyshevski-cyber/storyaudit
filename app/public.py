"""Unauthenticated public surface: landing page and free audit requests.

Everything internal stays behind ``APP_ACCESS_TOKEN`` (``/ui/...`` and the JSON
API). This module is the only place that accepts input from the open internet, so
it owns the abuse controls and the capability-token checks.
"""

from __future__ import annotations

import hmac
import re
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.audit_requests import (
    create_audit_request,
    get_request_by_token,
    hash_client,
    progress_steps,
    public_state,
    status_payload,
)
from app.config import get_settings
from app.db import get_db
from app.errors import AppError
from app.ratelimit import guard_public_request, guard_report_build, guard_report_pdf
from app.reports import build_report_v2, render_html_v2
from app.reports.artifacts import get_or_render_pdf

public = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory="app/templates")

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
SAMPLE_REPORT = Path(__file__).resolve().parent / "templates" / "sample_report.html"


def _public_base(request: Request) -> str:
    configured = get_settings().public_base_url.strip()
    return (configured or str(request.base_url)).rstrip("/")


def _client_ip(request: Request, *, trusted: bool = False) -> str:
    # Render terminates TLS and sets X-Forwarded-For. The first comma-separated
    # value is the actual client for the rate-limit key.
    #
    # A trusted front-end sits behind an edge that rewrites X-Forwarded-For with
    # the address it saw, i.e. the front-end's own address, so every visitor would
    # share one bucket. Such a caller repeats the real client address in
    # ``x-client-ip``, and we only read it when the internal token matched.
    if trusted:
        explicit = (request.headers.get("x-client-ip") or "").strip()
        if explicit:
            return explicit
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _trusted_caller(request: Request) -> bool:
    """True when the request carries PUBLIC_INTERNAL_TOKEN (our front-end worker).

    Empty configuration disables the whole mechanism, so an unset secret can never
    make an arbitrary caller trusted.
    """
    expected = get_settings().public_internal_token.strip()
    if not expected:
        return False
    provided = (request.headers.get("x-internal-token") or "").strip()
    return hmac.compare_digest(provided, expected)


def _wants_json(request: Request) -> bool:
    return "application/json" in (request.headers.get("accept") or "").lower()


def _render_landing(
    request: Request,
    form: dict | None = None,
    error: str | None = None,
    status_code: int = 200,
    headers: dict | None = None,
):
    return templates.TemplateResponse(
        request=request,
        name="landing.html",
        context={
            "form": form or {},
            "error": error,
            "public_base_url": _public_base(request),
        },
        status_code=status_code,
        headers=headers,
    )


def _render_status(
    request: Request,
    *,
    audit_token: str | None = None,
    record=None,
    state: str | None = None,
    missing: bool = False,
    notready: bool = False,
    status_code: int = 200,
):
    steps = progress_steps(state or "ingesting") if not missing else []
    report_url = f"/audit/{audit_token}/report" if state == "ready" else None
    pdf_url = f"/audit/{audit_token}/report.pdf" if state == "ready" else None
    return templates.TemplateResponse(
        request=request,
        name="request_status.html",
        context={
            "record": record,
            "state": state,
            "steps": steps,
            "report_url": report_url,
            "pdf_url": pdf_url,
            "missing": missing,
            "notready": notready,
            "year": _year(),
        },
        status_code=status_code,
        headers={"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-store"},
    )


def _year() -> int:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).year


@public.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return _render_landing(request)


@public.post("/audit")
def submit_audit(
    request: Request,
    website: str = Form(""),
    email: str = Form(""),
    company: str = Form(""),
    competitor_1: str = Form(""),
    competitor_2: str = Form(""),
    consent: str = Form(""),
    company_website: str = Form(""),
    session: Session = Depends(get_db),
):
    form = {
        "website": website.strip(),
        "email": email.strip(),
        "competitor_1": competitor_1.strip(),
        "competitor_2": competitor_2.strip(),
    }
    if company_website.strip():
        # Honeypot: real browsers never fill this hidden field.
        return _render_landing(request, form, "Не удалось отправить заявку.", 400)
    if not website.strip():
        return _render_landing(request, form, "Укажите адрес сайта.", 422)
    if consent not in {"on", "1", "true", "yes"}:
        return _render_landing(
            request, form,
            "Нужно согласие на обработку адреса сайта и email.", 422,
        )
    if email and not EMAIL_RE.match(email.strip()):
        return _render_landing(request, form, "Проверьте адрес электронной почты.", 422)

    trusted = _trusted_caller(request)
    ip = _client_ip(request, trusted=trusted)
    try:
        guard_public_request(hash_client(ip) or "", trusted=trusted)
        _, raw_token = create_audit_request(
            session,
            website_url=website,
            email=email,
            company=company,
            competitors=[competitor_1, competitor_2],
            ip=ip,
            user_agent=request.headers.get("user-agent"),
            utm={
                key: request.query_params.get(key)
                for key in ("utm_source", "utm_medium", "utm_campaign")
                if request.query_params.get(key)
            },
        )
    except AppError as exc:
        return _render_landing(
            request, form, exc.message, exc.status_code,
            headers=getattr(exc, "headers", None),
        )
    if _wants_json(request):
        # Machine clients (our own front-end worker) cannot follow a 303 and
        # should not parse Location: hand back the capability token directly.
        return JSONResponse(
            {
                "token": raw_token,
                "website_url": website.strip(),
                "state_url": f"/audit/{raw_token}/state",
                "report_url": f"/audit/{raw_token}/report",
                "pdf_url": f"/audit/{raw_token}/report.pdf",
            },
            status_code=201,
            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"},
        )
    return RedirectResponse(f"/audit/{raw_token}", status_code=303)


@public.get("/audit/{audit_token}", response_class=HTMLResponse)
def audit_status(
    audit_token: str,
    request: Request,
    notready: bool = False,
    session: Session = Depends(get_db),
):
    try:
        record = get_request_by_token(session, audit_token)
    except AppError:
        return _render_status(request, missing=True, status_code=404)
    state = public_state(session, record)
    return _render_status(
        request, audit_token=audit_token, record=record, state=state,
        notready=notready,
    )


@public.get("/audit/{audit_token}/state")
def audit_state(audit_token: str, session: Session = Depends(get_db)):
    record = get_request_by_token(session, audit_token)
    return JSONResponse(
        status_payload(session, record, audit_token),
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"},
    )


@public.get("/audit/{audit_token}/report", response_class=HTMLResponse)
def audit_report(
    audit_token: str,
    request: Request,
    inline: int = 0,
    session: Session = Depends(get_db),
):
    record = get_request_by_token(session, audit_token)
    if public_state(session, record) != "ready" or record.scan_id is None:
        return RedirectResponse(f"/audit/{audit_token}?notready=true", status_code=303)
    guard_report_build(record.scan_id)
    document = build_report_v2(session, record.scan_id)
    if document.meta.report_status != "client_ready":
        return RedirectResponse(f"/audit/{audit_token}?notready=true", status_code=303)
    html = render_html_v2(
        document,
        # inline=1 embeds the stylesheet, so the HTML stays readable when it is
        # saved as a file or served from another domain (our marketing site
        # republishes the report under its own address).
        inline_css=bool(inline),
        download_url=f"/audit/{audit_token}/report.pdf",
    )
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store",
            "X-Robots-Tag": "noindex, nofollow",
            "Referrer-Policy": "no-referrer",
        },
    )


@public.get("/audit/{audit_token}/report.pdf")
def audit_report_pdf(audit_token: str, session: Session = Depends(get_db)):
    try:
        record = get_request_by_token(session, audit_token)
    except AppError:
        return RedirectResponse(f"/audit/{audit_token}", status_code=303)
    if public_state(session, record) != "ready" or record.scan_id is None:
        return RedirectResponse(f"/audit/{audit_token}?notready=true", status_code=303)
    guard_report_pdf(record.scan_id)
    document = build_report_v2(session, record.scan_id)
    if document.meta.report_status != "client_ready":
        return RedirectResponse(f"/audit/{audit_token}?notready=true", status_code=303)
    pdf, cache_hit = get_or_render_pdf(session, record.scan_id, document)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="narrative-audit-{record.scan_id}.pdf"',
            "Cache-Control": "no-store",
            "ETag": f'"{document.meta.content_hash}"',
            "X-Report-Cache": "HIT" if cache_hit else "MISS",
            "X-Robots-Tag": "noindex, nofollow",
            "Referrer-Policy": "no-referrer",
        },
    )


@public.get("/sample-report", response_class=HTMLResponse)
def sample_report():
    return HTMLResponse(
        SAMPLE_REPORT.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


@public.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="privacy.html",
        context={"year": _year()},
    )


@public.get("/robots.txt", response_class=Response)
def robots(request: Request):
    base = _public_base(request)
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /audit/\n"
        "Disallow: /ui/\n"
        "Disallow: /scans\n"
        "Disallow: /health/\n"
        f"Sitemap: {base}/sitemap.xml\n"
    )
    return Response(body, media_type="text/plain")


@public.get("/sitemap.xml", response_class=Response)
def sitemap(request: Request):
    base = _public_base(request)
    from xml.sax.saxutils import escape
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{escape(base)}/</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>\n"
        f"  <url><loc>{escape(base)}/privacy</loc><changefreq>yearly</changefreq><priority>0.2</priority></url>\n"
        "</urlset>\n"
    )
    return Response(body, media_type="application/xml")
