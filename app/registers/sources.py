"""S-register: mutable source metadata on top of the immutable corpus."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Document, Scan, Source
from app.registers.codes import allocate_code
from app.registers.models import SourceRegistryEntry

DEFAULT_WEBSITE_LIMITS = ["Содержание страниц изменяемо", "Прочитан только доступный HTML"]


def ensure_source_registry(session: Session, scan: Scan) -> list[SourceRegistryEntry]:
    """Give every corpus source a stable S-code. Idempotent."""
    existing = {
        row.source_id: row
        for row in session.scalars(
            select(SourceRegistryEntry).where(SourceRegistryEntry.scan_id == scan.id)
        )
    }
    sources = list(
        session.scalars(select(Source).where(Source.scan_id == scan.id).order_by(Source.created_at, Source.id))
    )
    created: list[SourceRegistryEntry] = []
    for source in sources:
        document = session.scalar(
            select(Document).where(Document.source_id == source.id).order_by(Document.created_at, Document.id)
        )
        metadata = source.source_metadata or {}
        role = metadata.get("role") if metadata.get("role") in {"client", "competitor"} else "client"
        is_competitor = role == "competitor"
        selection_reason = metadata.get("selection_reason")
        limitations = list(DEFAULT_WEBSITE_LIMITS) if source.type == "website" else []
        if is_competitor:
            limitations += ["Публичная страница альтернативы", "Независимая проверка не выполнялась"]
        confirms = (
            "Публичное предложение альтернативы"
            if is_competitor
            else f"Страницы источника «{source.name}»"
        )
        entry_metadata = {
            "source_type": source.type,
            "external_id": source.external_id,
        }
        if selection_reason:
            entry_metadata["selection_reason"] = selection_reason
        if source.id in existing:
            row = existing[source.id]
            row.role = role
            row.provenance = "secondary" if is_competitor else "primary"
            row.confirms = confirms
            row.entry_metadata = {**(row.entry_metadata or {}), **entry_metadata}
            if is_competitor:
                merged = [str(item) for item in (row.limitations or [])]
                for item in limitations:
                    if item not in merged:
                        merged.append(item)
                row.limitations = merged
            continue
        entry = SourceRegistryEntry(
            scan_id=scan.id,
            code=allocate_code(session, scan.id, "S"),
            source_id=source.id,
            url=metadata.get("entry_url") or (document.url if document else None),
            role=role,
            provenance="secondary" if is_competitor else "primary",
            availability="full" if document else "unavailable",
            currency="current",
            captured_at=source.created_at,
            content_hash=document.content_hash if document else None,
            confirms=confirms,
            limitations=limitations,
            entry_metadata=entry_metadata,
        )
        session.add(entry)
        created.append(entry)
    if created:
        session.flush()
    return list(
        session.scalars(
            select(SourceRegistryEntry)
            .where(SourceRegistryEntry.scan_id == scan.id)
            .order_by(SourceRegistryEntry.code)
        )
    )


def registry_rows(session: Session, scan_id: uuid.UUID) -> list[SourceRegistryEntry]:
    return list(
        session.scalars(
            select(SourceRegistryEntry)
            .where(SourceRegistryEntry.scan_id == scan_id)
            .order_by(SourceRegistryEntry.code)
        )
    )
