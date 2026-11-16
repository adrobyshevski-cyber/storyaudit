#!/usr/bin/env python
"""Delete scans with every row that belongs to them.

A scan drags ~45 tables with it (corpus, jobs, annotations, registers, narratives,
PDF artifacts, usage rows). Every foreign key is ``NO ACTION``, so a plain
``DELETE FROM scans`` fails; this script deletes children first, in the order the
foreign-key graph requires.

Dry run by default: it prints a per-table row count and changes nothing.

    python scripts/delete_scan.py --title-like 'dbg%'      # what would go
    python scripts/delete_scan.py --title-like 'dbg%' --apply
    python scripts/delete_scan.py --scan <uuid> --apply

This is an operator tool for test/duplicate scans. Retention pruning
(``scripts/prune.py``) is the normal way to keep the database small; deleting a
scan removes evidence, so there is no undo.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import uuid
from collections import defaultdict
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from app.db import get_engine, get_session_factory  # noqa: E402


class ScanDeleter:
    """FK-aware cascade for ``scans`` (the schema deliberately has none)."""

    def __init__(self, engine: Engine) -> None:
        inspector = inspect(engine)
        self.tables = inspector.get_table_names()
        self.columns = {
            table: {column["name"] for column in inspector.get_columns(table)}
            for table in self.tables
        }
        self.incoming: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for child in self.tables:
            for fk in inspector.get_foreign_keys(child):
                parent = fk["referred_table"]
                if parent == child or parent not in self.columns:
                    continue
                self.incoming[parent].append((child, fk["constrained_columns"][0]))
        self.parents: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for parent, edges in self.incoming.items():
            for child, column in edges:
                self.parents[child].append((parent, column))
        # Depth from ``scans`` = longest FK path, so a table is deleted only after
        # every table that references it (e.g. finding_evidence cites propositions).
        self.depth: dict[str, int] = {"scans": 0}
        for _ in range(len(self.tables) + 1):
            changed = False
            for parent, edges in self.incoming.items():
                if parent not in self.depth:
                    continue
                for child, _ in edges:
                    candidate = self.depth[parent] + 1
                    if self.depth.get(child, -1) < candidate:
                        self.depth[child] = candidate
                        changed = True
            if not changed:
                break
        else:  # a cycle in the FK graph would make deletion order undefined
            raise RuntimeError("foreign-key graph from 'scans' is not acyclic")

    def _where(
        self, table: str, scan_ids: list[str], ids: dict[str, list[Any]]
    ) -> tuple[str, dict[str, Any]] | None:
        if table == "scans":
            return "id = ANY(:scan_ids)", {"scan_ids": scan_ids}
        conditions: list[str] = []
        params: dict[str, Any] = {}
        if "scan_id" in self.columns[table]:
            conditions.append("scan_id = ANY(:scan_ids)")
            params["scan_ids"] = scan_ids
        for parent, column in self.parents.get(table, []):
            parent_ids = ids.get(parent) or []
            if not parent_ids:
                continue
            key = f"ids{len(params)}"
            conditions.append(f"{column} = ANY(:{key})")
            params[key] = parent_ids
        if not conditions:
            return None
        return " OR ".join(conditions), params

    def _plan(self, session: Any, scan_ids: list[str]) -> dict[str, list[Any]]:
        """Collect row ids top-down so deeper tables can match their parents."""
        ids: dict[str, list[Any]] = {"scans": scan_ids}
        for table in sorted(self.depth, key=self.depth.get):
            if table == "scans" or "id" not in self.columns[table]:
                continue  # junction tables are matched through their FK instead
            where = self._where(table, scan_ids, ids)
            if where is None:
                continue
            clause, params = where
            ids[table] = [
                row[0] for row in session.execute(text(f"SELECT id FROM {table} WHERE {clause}"), params)
            ]
        return ids

    def execute(
        self, session: Any, scan_ids: list[str], *, dry_run: bool = True
    ) -> dict[str, int]:
        ids = self._plan(session, scan_ids)
        counts: dict[str, int] = {}
        targets: list[tuple[str, str, dict[str, Any]]] = []
        for table in sorted(self.depth, key=self.depth.get, reverse=True):
            where = self._where(table, scan_ids, ids)
            if where is None:
                continue
            targets.append((table, *where))
        if dry_run:
            for table, clause, params in targets:
                value = session.scalar(text(f"SELECT count(*) FROM {table} WHERE {clause}"), params)
                counts[table] = int(value or 0)
            session.rollback()
            return counts

        # The corpus is immutable on purpose: triggers reject DELETE on sources,
        # documents, blocks and propositions. Administrative deletion of a whole
        # scan is the one legitimate exception, so the triggers are disabled for
        # the duration of this transaction (DDL is transactional in PostgreSQL and
        # is rolled back together with a failed delete).
        touched = [table for table, _, _ in targets]
        for table in touched:
            session.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER USER"))
        for table, clause, params in targets:
            result = session.execute(text(f"DELETE FROM {table} WHERE {clause}"), params)
            counts[table] = int(result.rowcount or 0)
        for table in touched:
            session.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER USER"))
        session.commit()
        return counts


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--scan", action="append", default=[], help="scan id (repeatable)")
    parser.add_argument("--title-like", default="", help="SQL LIKE pattern on scans.title")
    parser.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    args = parser.parse_args()

    deleter = ScanDeleter(get_engine())
    with get_session_factory()() as session:
        ids = list(args.scan)
        if args.title_like:
            rows = session.execute(
                text("SELECT id, title FROM scans WHERE title LIKE :pattern ORDER BY created_at"),
                {"pattern": args.title_like},
            ).all()
            for row in rows:
                print(f"  match: {row.id}  {row.title[:70]}")
            ids += [str(row.id) for row in rows]
        ids = sorted({str(uuid.UUID(item)) for item in ids})
        if not ids:
            parser.error("nothing selected: pass --scan or --title-like")
        print(f"scans to delete: {len(ids)}")
        counts = deleter.execute(session, ids, dry_run=not args.apply)

    for table, count in sorted(counts.items(), key=lambda item: -item[1]):
        if count:
            print(f"  {table:<28} {count}")
    total = sum(counts.values())
    touched = sum(1 for count in counts.values() if count)
    verb = "deleted" if args.apply else "would delete"
    print(f"{verb}: {total} row(s) across {touched} table(s)")
    if not args.apply:
        print("dry run: nothing was changed. Re-run with --apply to delete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
