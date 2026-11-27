"""S-register: mutable source metadata on top of the immutable corpus."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Document, Scan, Source
from app.registers.codes import allocate_code
from app.registers.models import (
    ActantMap,
    ActantRole,
    ConsistencyCell,
    EvidenceRecord,
    FindingV2,
    JourneyStep,
    LanguageCode,
    Product,
    ScorecardEntry,
    SourceRegistryEntry,
    Statement,
)
from app.utils import canonical_host

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


# Все регистры, где на источники ссылаются строковыми кодами S01… — при
# схлопывании дублей эти ссылки нужно перенаправить на оставшийся код.
CODE_CARRIERS = (
    (Product, "source_codes"),
    (Statement, "source_codes"),
    (EvidenceRecord, "source_codes"),
    (ActantRole, "source_codes"),
    (JourneyStep, "source_codes"),
    (ConsistencyCell, "source_codes"),
    (FindingV2, "basis_codes"),
    (LanguageCode, "source_codes"),
    (ScorecardEntry, "source_codes"),
)


def registry_duplicates(session: Session, scan_id: uuid.UUID) -> dict[str, list[SourceRegistryEntry]]:
    """Группы записей S-реестра, у которых один и тот же домен."""
    groups: dict[str, list[SourceRegistryEntry]] = {}
    for row in registry_rows(session, scan_id):
        host = canonical_host(row.url)
        if host:
            groups.setdefault(host, []).append(row)
    return {host: entries for host, entries in groups.items() if len(entries) > 1}


def _scan_query(model, scan_id: uuid.UUID):
    """У части регистров (актанты) `scan_id` живёт в родительской таблице."""
    if model is ActantRole:
        return select(ActantRole).where(
            ActantRole.map_id.in_(select(ActantMap.id).where(ActantMap.scan_id == scan_id))
        )
    return select(model).where(model.scan_id == scan_id)


def _repoint_codes(
    session: Session, scan_id: uuid.UUID, mapping: dict[str, str], *, dry_run: bool
) -> dict[str, int]:
    changed: dict[str, int] = {}
    if not mapping:
        return changed
    for model, field in CODE_CARRIERS:
        for obj in session.scalars(_scan_query(model, scan_id)):
            codes = list(getattr(obj, field) or [])
            if not codes:
                continue
            replaced = [mapping.get(str(code), code) for code in codes]
            deduped = list(dict.fromkeys(replaced))
            if deduped != codes:
                changed[model.__name__] = changed.get(model.__name__, 0) + 1
                if not dry_run:
                    setattr(obj, field, deduped)
    return changed


def merge_duplicate_sources(session: Session, scan_id: uuid.UUID, *, dry_run: bool = True) -> list[dict]:
    """Схлопнуть дубли S-реестра: один домен — одна запись S-кода.

    Оставляем запись с меньшим кодом (S01 раньше S02), перенаправляем на неё
    ссылки во всех регистрах, объединяем ограничения и метаданные, лишние строки
    реестра удаляем. Корпус (`sources`/`documents`) не трогаем: это неизменяемые
    данные, и на них ссылаются блоки-цитаты.
    """
    report: list[dict] = []
    for host, entries in sorted(registry_duplicates(session, scan_id).items()):
        entries = sorted(entries, key=lambda item: item.code)
        keep, extras = entries[0], entries[1:]
        mapping = {row.code: keep.code for row in extras}

        changed = _repoint_codes(session, scan_id, mapping, dry_run=dry_run)

        limitations = [str(item) for item in (keep.limitations or [])]
        for row in extras:
            for item in row.limitations or []:
                if str(item) not in limitations:
                    limitations.append(str(item))

        metadata = dict(keep.entry_metadata or {})
        metadata["merged_codes"] = sorted(
            set(metadata.get("merged_codes") or []) | {row.code for row in extras}
        )
        metadata["merged_urls"] = sorted(
            set(metadata.get("merged_urls") or [])
            | {row.url for row in extras if row.url}
        )

        if not dry_run:
            keep.limitations = limitations
            keep.entry_metadata = metadata
            keep.content_hash = keep.content_hash or next(
                (row.content_hash for row in extras if row.content_hash), None
            )
            if any(row.availability == "full" for row in extras) or keep.availability == "full":
                keep.availability = "full"
            for row in extras:
                session.delete(row)

        report.append(
            {
                "host": host,
                "kept": keep.code,
                "merged": [row.code for row in extras],
                "repointed": changed,
            }
        )

    if not dry_run and report:
        session.commit()
    return report
