"""Retention: delete data that grows without bound but has no audit value.

What actually grows in a long-lived installation:

* ``report_artifacts`` — a PDF snapshot per report content hash, 1-3 MB each in
  PostgreSQL. This is the only table that can realistically fill the disk.
* ``llm_usage`` — one row per provider request (hundreds per scan).
* ``annotations`` — every reanalysis adds a superseded generation.

Nothing here touches the immutable corpus, the current annotation set, findings,
registers or report snapshots' metadata: only superseded/duplicated rows and
binary blobs. Everything is dry-run by default and safe to interrupt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings

logger = logging.getLogger("storyaudit.retention")


@dataclass
class PruneReport:
    name: str
    deleted: int = 0
    freed_bytes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"table": self.name, "deleted": self.deleted, "freed_bytes": self.freed_bytes}


def _cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def prune_report_artifacts(
    session: Session, *, older_than_days: int, keep_per_scan: int, dry_run: bool = True
) -> PruneReport:
    """Drop old PDF snapshots, always keeping the newest one per scan."""
    keep = max(1, keep_per_scan)
    stale = session.execute(
        text(
            """
            WITH ranked AS (
                SELECT id,
                       created_at,
                       octet_length(content) AS size,
                       row_number() OVER (
                           PARTITION BY scan_id ORDER BY created_at DESC, id DESC
                       ) AS rn
                FROM report_artifacts
            )
            SELECT id, size FROM ranked
            WHERE rn > 1 AND (rn > :keep OR created_at < :cutoff)
            """
        ),
        {"keep": keep, "cutoff": _cutoff(older_than_days)},
    ).all()
    report = PruneReport("report_artifacts", len(stale), sum(row.size for row in stale))
    if dry_run or not stale:
        return report
    session.execute(
        text("DELETE FROM report_artifacts WHERE id = ANY(:ids)"),
        {"ids": [row.id for row in stale]},
    )
    return report


def prune_llm_usage(session: Session, *, older_than_days: int, dry_run: bool = True) -> PruneReport:
    cutoff = _cutoff(older_than_days)
    count = session.scalar(
        text("SELECT count(*) FROM llm_usage WHERE created_at < :cutoff"), {"cutoff": cutoff}
    ) or 0
    report = PruneReport("llm_usage", int(count))
    if dry_run or not count:
        return report
    session.execute(text("DELETE FROM llm_usage WHERE created_at < :cutoff"), {"cutoff": cutoff})
    return report


def prune_superseded_annotations(
    session: Session, *, older_than_days: int, dry_run: bool = True
) -> PruneReport:
    """Drop superseded annotation versions after the grace period.

    The surviving rows are never current, so no profile, metric, finding or report
    reads them. Their predecessors' links are cleared first, otherwise the
    self-referencing foreign key blocks the delete.
    """
    cutoff = _cutoff(older_than_days)
    count = session.scalar(
        text(
            "SELECT count(*) FROM annotations WHERE is_current = false AND created_at < :cutoff"
        ),
        {"cutoff": cutoff},
    ) or 0
    report = PruneReport("annotations", int(count))
    if dry_run or not count:
        return report
    session.execute(
        text(
            """
            UPDATE annotations SET supersedes_annotation_id = NULL
            WHERE supersedes_annotation_id IN (
                SELECT id FROM annotations WHERE is_current = false AND created_at < :cutoff
            )
            """
        ),
        {"cutoff": cutoff},
    )
    session.execute(
        text("DELETE FROM annotations WHERE is_current = false AND created_at < :cutoff"),
        {"cutoff": cutoff},
    )
    return report


def table_sizes(session: Session) -> dict[str, str]:
    rows = session.execute(
        text(
            """
            SELECT relname, pg_size_pretty(pg_total_relation_size(c.oid)) AS pretty
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND relname IN ('report_artifacts', 'llm_usage', 'annotations', 'jobs',
                              'documents', 'blocks', 'propositions')
            ORDER BY pg_total_relation_size(c.oid) DESC
            """
        )
    ).all()
    return {row.relname: row.pretty for row in rows}


def run_retention(session: Session, *, dry_run: bool = True) -> dict[str, Any]:
    """Apply every retention rule from settings. Returns a JSON-ready report."""
    settings = get_settings()
    reports = [
        prune_report_artifacts(
            session,
            older_than_days=settings.retention_report_artifacts_days,
            keep_per_scan=settings.retention_report_artifacts_keep,
            dry_run=dry_run,
        ),
        prune_llm_usage(
            session, older_than_days=settings.retention_llm_usage_days, dry_run=dry_run
        ),
    ]
    if settings.retention_annotations_days > 0:
        reports.append(
            prune_superseded_annotations(
                session,
                older_than_days=settings.retention_annotations_days,
                dry_run=dry_run,
            )
        )
    if dry_run:
        session.rollback()
    else:
        session.commit()
    result = {
        "dry_run": dry_run,
        "tables": [report.as_dict() for report in reports],
        "deleted": sum(report.deleted for report in reports),
        "freed_bytes": sum(report.freed_bytes for report in reports),
        "sizes": table_sizes(session),
    }
    logger.info(
        "Retention finished",
        extra={"service": "retention", "deleted": result["deleted"], "dry_run": dry_run},
    )
    return result
