"""Narrative Consistency Matrix.

Sixteen cells (four actant roles × four dimensions). `No data` is a first-class
status: a missing observation is not a contradiction and must never be shown as
one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.registers.models import (
    ActantRole,
    ConsistencyCell,
    EvidenceRecord,
    JourneyStep,
    Product,
)

ROLES = ("subject", "opponent", "object", "helper")
DIMENSIONS = ("communication", "service", "experience", "evidence")
ROLE_LABELS = {
    "subject": "Hero — аудитория",
    "opponent": "Opponent — препятствие",
    "object": "Object — обещанный результат",
    "helper": "Helper — роль эксперта",
}


@dataclass
class CellDraft:
    actant_role: str
    dimension: str
    status: str
    note: str | None = None
    source_codes: list[str] = field(default_factory=list)
    counterexample: str | None = None

    def as_payload(self) -> dict:
        return {
            "actant_role": self.actant_role,
            "dimension": self.dimension,
            "status": self.status,
            "note": self.note,
            "source_codes": self.source_codes,
            "counterexample": self.counterexample,
        }


def _roles_of(roles: list[ActantRole]) -> dict[str, ActantRole]:
    return {role.role: role for role in roles}


def build_consistency_draft(
    products: list[Product],
    evidence: list[EvidenceRecord],
    actant_roles: list[ActantRole],
    journey: list[JourneyStep],
    *,
    audit_mode: str,
) -> list[CellDraft]:
    role_map = _roles_of(actant_roles)
    product = next((item for item in products if item.is_primary), products[0] if products else None)
    source_codes = [str(code) for code in (product.source_codes or [])] if product else []
    published = [item for item in evidence if item.verification == "publication_only"]
    independent = [item for item in evidence if item.verification == "independently_confirmed"]
    observed_journey = [step for step in journey if step.is_observed]

    cells: list[CellDraft] = []
    for role in ROLES:
        record = role_map.get(role)
        reconstruction = (record.reconstruction if record else "") or ""
        has_role = bool(reconstruction) and record is not None and record.status != "U"
        evidence_for_role = [str(code) for code in ((record.evidence_codes if record else []) or [])]

        if has_role:
            communication = ("confirmed", f"Роль заявлена в публичном тексте: {reconstruction[:120]}")
        elif record is not None:
            communication = ("partial", "Роль упомянута, но не раскрыта в корпусе")
        else:
            communication = ("no_data", "Роль не реконструирована")
        cells.append(CellDraft(
            actant_role=role, dimension="communication", status=communication[0],
            note=communication[1], source_codes=source_codes if has_role else [],
        ))

        if product and product.promise:
            if product.boundary and product.procedure:
                service = ("confirmed", "Обещание подкреплено границами и процедурой")
            else:
                service = ("partial", "Обещание есть, но границы/процедура не описаны")
        elif product:
            service = ("partial", "Услуга описана без обещания результата")
        else:
            service = ("no_data", "Продуктовая карта отсутствует")
        cells.append(CellDraft(
            actant_role=role, dimension="service", status=service[0],
            note=service[1], source_codes=source_codes if product else [],
        ))

        if audit_mode == "desk" or not observed_journey:
            experience = ("no_data", "Desk Research: опыт взаимодействия не наблюдался")
        else:
            experience = ("partial", "Есть наблюдения пути, но нет данных о результате")
        cells.append(CellDraft(
            actant_role=role, dimension="experience", status=experience[0],
            note=experience[1], source_codes=[],
        ))

        if independent:
            evidence_status = ("confirmed", "Есть независимо подтверждённые доказательства")
        elif evidence_for_role or (published and role in {"object", "helper"}):
            evidence_status = ("partial", "Опора ограничена публикацией утверждения")
        else:
            evidence_status = ("no_data", "Релевантных доказательств не найдено")
        cells.append(CellDraft(
            actant_role=role, dimension="evidence", status=evidence_status[0],
            note=evidence_status[1],
            source_codes=evidence_for_role if evidence_status[0] != "no_data" else [],
            counterexample=(
                "Возможен иной источник доверия, не попавший в публичный корпус"
                if evidence_status[0] == "conflict" else None
            ),
        ))
    return cells


def existing_cells(session, scan_id) -> list[ConsistencyCell]:
    from sqlalchemy import select

    return list(session.scalars(select(ConsistencyCell).where(ConsistencyCell.scan_id == scan_id)))


__all__ = ["CellDraft", "ROLES", "DIMENSIONS", "ROLE_LABELS", "build_consistency_draft", "existing_cells"]


def persist_consistency(session, scan, drafts: list[CellDraft]) -> list[str]:
    keys: list[str] = []
    for draft in drafts:
        session.add(ConsistencyCell(
            scan_id=scan.id,
            actant_role=draft.actant_role,
            dimension=draft.dimension,
            status=draft.status,
            note=draft.note,
            source_codes=draft.source_codes,
            counterexample=draft.counterexample,
        ))
        keys.append(f"{draft.actant_role}:{draft.dimension}")
    session.flush()
    return keys
