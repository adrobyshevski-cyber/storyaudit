"""Stable register codes (S01, C01, E01, G01, M01, W01, R01, KPI01).

Codes are allocated once per scan and never reused: a rejected finding leaves a
gap in the numbering, which keeps every quote in the report valid forever.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.registers.models import CodeSequence

CODE_PREFIXES = {
    "source": "S",
    "competitor": "C",
    "product": "P",
    "statement_fact": "F",
    "statement_interpretation": "I",
    "evidence": "E",
    "gap": "G",
    "missing_link": "M",
    "weak_evidence": "W",
    "language": "L",
    "recommendation": "R",
    "kpi": "KPI",
    "hypothesis": "H",
    "layer": "T",
    "actant_map": "A",
}


def _format(prefix: str, value: int) -> str:
    width = 2 if len(prefix) <= 2 else 3
    return f"{prefix}{value:0{width}d}" if prefix != "KPI" else f"KPI{value:02d}"


def allocate_code(session: Session, scan_id: uuid.UUID, prefix: str) -> str:
    """Allocate the next code for one scan under an advisory lock."""
    if not prefix:
        raise ValueError("prefix is required")
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"code:{scan_id}:{prefix}"},
    )
    sequence = session.get(CodeSequence, (scan_id, prefix))
    if sequence is None:
        sequence = CodeSequence(scan_id=scan_id, prefix=prefix, next_value=2)
        session.add(sequence)
        session.flush()
        return _format(prefix, 1)
    value = sequence.next_value
    sequence.next_value = value + 1
    session.flush()
    return _format(prefix, value)


def ensure_codes(session: Session, scan_id: uuid.UUID, prefix: str, count: int) -> list[str]:
    return [allocate_code(session, scan_id, prefix) for _ in range(count)]
