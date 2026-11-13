"""Retention CLI: dry-run by default, ``--apply`` to delete.

    python scripts/prune.py                 # what would be deleted
    python scripts/prune.py --apply         # delete
    python scripts/prune.py --apply --vacuum  # delete and reclaim disk (blocks writes)

Run it from a Render cron job, a maintenance window, or by hand. The same rules
run inside the worker when RETENTION_ENABLED=true (see app/retention.py).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.db import get_engine, get_session_factory  # noqa: E402
from app.retention import run_retention  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete aged report artifacts, usage and annotations")
    parser.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="VACUUM FULL the pruned tables; reclaims disk but locks them while running",
    )
    args = parser.parse_args()

    with get_session_factory()() as session:
        report = run_retention(session, dry_run=not args.apply)
    if args.vacuum and args.apply:
        # VACUUM cannot run inside a transaction block, so it needs its own
        # autocommit connection outside the session above.
        with get_engine().connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            for table in ("report_artifacts", "llm_usage", "annotations"):
                connection.execute(text(f"VACUUM FULL {table}"))
        report["vacuum"] = True
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
