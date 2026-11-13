"""Shared helpers for the analysis roles."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Block, Document, Proposition, Scan, Source
from app.registers.models import SourceRegistryEntry
from app.registers.sources import ensure_source_registry


def registry_map(session: Session, scan: Scan) -> dict[uuid.UUID, SourceRegistryEntry]:
    ensure_source_registry(session, scan)
    rows = session.scalars(
        select(SourceRegistryEntry).where(SourceRegistryEntry.scan_id == scan.id)
    )
    return {row.source_id: row for row in rows if row.source_id is not None}


def propositions_by_source(
    session: Session,
    scan_id: uuid.UUID,
) -> dict[uuid.UUID, list[Proposition]]:
    rows = session.execute(
        select(Source.id, Proposition)
        .join(Document, Document.source_id == Source.id)
        .join(Block, Block.document_id == Document.id)
        .join(Proposition, Proposition.block_id == Block.id)
        .where(Source.scan_id == scan_id)
        .order_by(Document.created_at, Document.id, Block.ordinal, Proposition.ordinal)
    ).all()
    grouped: dict[uuid.UUID, list[Proposition]] = {}
    for source_id, proposition in rows:
        grouped.setdefault(source_id, []).append(proposition)
    return grouped
