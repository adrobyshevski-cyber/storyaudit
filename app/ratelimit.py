"""Rate limiting for expensive operations, counted in PostgreSQL.

No Redis: a single row per ``(bucket, window)`` is incremented atomically with
``INSERT ... ON CONFLICT DO UPDATE``, so several API instances share one budget
and a restart does not reset it. Windows are aligned to wall-clock boundaries
computed in SQL (``floor(epoch / window)``), so clock skew between instances does
not create extra windows either.

Counters are committed before the guarded work runs: even if the request later
fails, the attempt is counted, otherwise a failing client could loop for free.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import text

from app.config import get_settings
from app.db import get_session_factory
from app.errors import AppError

logger = logging.getLogger("storyaudit.ratelimit")

# One statement: claim the slot and learn how much budget is left in this window.
_HIT = text(
    """
    INSERT INTO rate_limit_counters (bucket, window_start, count, updated_at)
    VALUES (
        :bucket,
        to_timestamp(floor(extract(epoch FROM now()) / :window) * :window),
        1,
        now()
    )
    ON CONFLICT (bucket, window_start)
    DO UPDATE SET count = rate_limit_counters.count + 1, updated_at = now()
    RETURNING count,
              extract(epoch FROM window_start + make_interval(secs => :window))
              - extract(epoch FROM now()) AS retry_after
    """
)


def hit(bucket: str, limit: int, window_seconds: int) -> None:
    """Count one attempt; raise 429 when the window budget is exhausted."""
    settings = get_settings()
    if not settings.rate_limit_enabled or limit <= 0:
        return
    try:
        with get_session_factory()() as session:
            row = session.execute(
                _HIT, {"bucket": bucket, "limit": limit, "window": window_seconds}
            ).one()
            session.commit()
    except Exception:  # noqa: BLE001 - a broken counter must never block the product
        logger.warning("Rate limit counter failed, allowing the request", exc_info=True)
        return
    if row.count > limit:
        retry_after = max(1, int(float(row.retry_after or window_seconds)))
        raise AppError(
            429,
            "rate_limited",
            f"Слишком много запросов: лимит {limit} {_human_window(window_seconds)}. "
            f"Повторите через {retry_after} с.",
            headers={"Retry-After": str(retry_after)},
        )


def _human_window(window_seconds: int) -> str:
    if window_seconds >= 86_400:
        return "в сутки"
    if window_seconds % 3_600 == 0:
        return f"за {window_seconds // 3_600} ч"
    return f"за {window_seconds // 60} мин"


def _window(label: str) -> str:
    return label


# --- per-operation guards ---------------------------------------------------


def guard_scan_create() -> None:
    settings = get_settings()
    hit(_window("scan_create"), settings.rate_limit_scans_per_day, 86_400)


def guard_source_add(scan_id: uuid.UUID) -> None:
    settings = get_settings()
    hit(f"scan_source:{scan_id}", settings.rate_limit_sources_per_hour, 3_600)


def guard_run(scan_id: uuid.UUID) -> None:
    settings = get_settings()
    hit(f"scan_run:{scan_id}", settings.rate_limit_runs_per_hour, 3_600)


def guard_reanalysis(scan_id: uuid.UUID) -> None:
    settings = get_settings()
    hit(f"scan_reanalysis:{scan_id}", settings.rate_limit_reanalysis_per_hour, 3_600)


def guard_report_build(scan_id: uuid.UUID) -> None:
    settings = get_settings()
    hit(f"scan_report_build:{scan_id}", settings.rate_limit_report_builds_per_hour, 3_600)


def guard_report_pdf(scan_id: uuid.UUID) -> None:
    settings = get_settings()
    hit(f"scan_report_pdf:{scan_id}", settings.rate_limit_report_pdfs_per_hour, 3_600)


def guard_public_request(ip_hash: str, *, trusted: bool = False) -> None:
    """Limit free public audits by client IP and globally per day.

    The global counter is the capacity guard: one scan occupies a worker for
    roughly 15 minutes, so an unbounded public form would silently create a
    multi-day queue. The per-IP window is the abuse guard.

    ``trusted`` is set for calls carrying ``PUBLIC_INTERNAL_TOKEN`` (our own
    front-end worker). Such a caller keeps the per-IP window — it is the only
    thing standing between a leaked token and a drained queue — but manages the
    global budget itself, because a single event legitimately needs more than
    ``PUBLIC_REQUESTS_PER_DAY`` and that number is per-process, not per-event.
    """
    settings = get_settings()
    hit(
        f"public_request_ip:{ip_hash}",
        settings.public_requests_per_ip_per_day,
        86_400,
    )
    if not trusted:
        hit("public_request_total", settings.public_requests_per_day, 86_400)


def counters(session: Any) -> list[dict[str, Any]]:
    """Debug helper: current windows and counts, newest first."""
    rows = session.execute(
        text(
            "SELECT bucket, window_start, count FROM rate_limit_counters "
            "ORDER BY updated_at DESC LIMIT 50"
        )
    ).all()
    return [
        {"bucket": row.bucket, "window_start": row.window_start.isoformat(), "count": row.count}
        for row in rows
    ]
