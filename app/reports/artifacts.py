"""Database-backed PDF cache keyed by the immutable report content hash."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.registers.models import ReportArtifact
from app.reports.model_v2 import ReportDocumentV2
from app.reports.render_v2 import render_pdf

PDF_MEDIA_TYPE = "application/pdf"


def get_or_render_pdf(
    session: Session, scan_id: uuid.UUID, document: ReportDocumentV2
) -> tuple[bytes, bool]:
    """Return ``(pdf, cache_hit)``; identical content is rendered only once."""
    cached = _get(session, scan_id, document.meta.content_hash)
    if cached is not None:
        return bytes(cached.content), True

    pdf = render_pdf(document)
    session.add(ReportArtifact(
        scan_id=scan_id,
        content_hash=document.meta.content_hash,
        media_type=PDF_MEDIA_TYPE,
        content=pdf,
    ))
    try:
        session.commit()
    except IntegrityError:
        # Two API workers may render the same snapshot concurrently. The unique
        # key makes one authoritative artifact; the loser reuses that result.
        session.rollback()
        cached = _get(session, scan_id, document.meta.content_hash)
        if cached is not None:
            return bytes(cached.content), True
        raise
    return pdf, False


def _get(session: Session, scan_id: uuid.UUID, content_hash: str) -> ReportArtifact | None:
    return session.scalar(select(ReportArtifact).where(
        ReportArtifact.scan_id == scan_id,
        ReportArtifact.content_hash == content_hash,
        ReportArtifact.media_type == PDF_MEDIA_TYPE,
    ))
