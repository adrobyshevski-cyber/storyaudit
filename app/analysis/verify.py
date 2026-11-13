"""Source Verification Layer: live-check an already captured source.

The check never fails a job. A failed probe is recorded as a limitation, because
"we could not check" is not the same as "the page is gone" (methodology §2.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.website import USER_AGENT, normalize_site_url

LIVE_CHECK_LIMITATION_PREFIX = "Live-проверка"


@dataclass
class LiveCheck:
    skipped: bool = False
    status_code: int | None = None
    final_url: str | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)


def http_probe(url: str, timeout: float) -> LiveCheck:
    """Single GET that stops after the response headers.

    Isolated in its own function so tests can replace it without network access.
    """
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout) as client:
            with client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as response:
                return LiveCheck(
                    status_code=response.status_code,
                    final_url=str(response.url),
                )
    except httpx.HTTPError as exc:
        return LiveCheck(error=type(exc).__name__)


def verify_source(session: Session, entry) -> LiveCheck:
    """Update a `source_registry` row with the result of a live probe."""
    settings = get_settings()
    if not entry.url:
        _add_limitation(entry, "URL не зафиксирован: live-проверка невозможна")
        return LiveCheck(skipped=True, error="no_url")
    if not settings.report_live_check:
        _add_limitation(entry, f"{LIVE_CHECK_LIMITATION_PREFIX} не выполнялась (REPORT_LIVE_CHECK=false)")
        return LiveCheck(skipped=True)

    try:
        url = normalize_site_url(entry.url)
    except Exception as exc:  # noqa: BLE001 - any URL problem is a limitation, not a crash
        _add_limitation(entry, f"URL не прошёл валидацию: {type(exc).__name__}")
        return LiveCheck(skipped=True, error="invalid_url")

    result = http_probe(url, settings.report_live_check_timeout_seconds)
    entry.live_checked_at = datetime.now(timezone.utc)
    entry.http_status = result.status_code
    if result.error:
        _add_limitation(entry, f"{LIVE_CHECK_LIMITATION_PREFIX} недоступна: {result.error}")
        return result
    if result.status_code and result.status_code >= 400:
        entry.availability = "partial"
        _add_limitation(entry, f"{LIVE_CHECK_LIMITATION_PREFIX}: HTTP {result.status_code}")
        return result
    if result.final_url and _host(result.final_url) != _host(url):
        result.notes.append(f"Редирект на другой хост: {result.final_url}")
        entry.entry_metadata = {**(entry.entry_metadata or {}), "live_final_url": result.final_url}
    entry.availability = "full"
    return result


def _host(url: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(url).hostname or "").lower()


def _add_limitation(entry, text: str) -> None:
    limitations = [str(item) for item in (entry.limitations or [])]
    if text not in limitations:
        limitations.append(text)
    entry.limitations = limitations
