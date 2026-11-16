#!/usr/bin/env python
"""Show what the current evidence selector would register for an already analysed scan.

Read-only and offline: it re-runs `select_claims` over the stored corpus and diffs the
result against the statements that scan actually persisted. No provider calls, no
writes — this is how the evidence budget and ranking are compared on real sites
before paying for a new scan.

    python scripts/compare_evidence.py --scan <id>                 # defaults 12 / 6
    python scripts/compare_evidence.py --scan <id> --limit 20
    python scripts/compare_evidence.py --scan <id> --show 0        # counts only
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.analysis.common import registry_map  # noqa: E402
from app.analysis.evidence import _score, select_claims  # noqa: E402
from app.db import get_session_factory  # noqa: E402
from app.models import Scan  # noqa: E402
from app.agents import _propositions_for_source  # noqa: E402
from app.registers.models import Statement  # noqa: E402


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def compare(session, scan: Scan, limit: int, competitor_limit: int) -> dict:
    registry = registry_map(session, scan)
    stored = {
        _normalize(item): item
        for item in session.scalars(select(Statement.text).where(Statement.scan_id == scan.id))
    }
    report = {"scan": str(scan.id), "sources": [], "added": 0, "dropped": 0, "kept": 0}
    for entry in registry.values():
        if entry.role not in {"client", "competitor"}:
            continue
        per_source = competitor_limit if entry.role == "competitor" else limit
        propositions = _propositions_for_source(session, entry.source_id)
        claims = select_claims(propositions, limit=per_source, source_code=entry.code)
        new = {_normalize(claim.statement): claim for claim in claims}
        added = [claim for key, claim in new.items() if key not in stored]
        kept = [claim for key, claim in new.items() if key in stored]
        dropped = [text for key, text in stored.items() if key not in new]
        report["sources"].append({
            "code": entry.code,
            "role": entry.role,
            "limit": per_source,
            "propositions": len(propositions),
            "selected": len(claims),
            "claims": claims,
            "kept": len(kept),
            "added": added,
            "dropped": dropped,
        })
        report["added"] += len(added)
        report["dropped"] += len(dropped)
        report["kept"] += len(kept)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--scan", action="append", required=True, help="scan id (repeatable)")
    parser.add_argument("--limit", type=int, default=12, help="per client source")
    parser.add_argument("--competitor-limit", type=int, default=6)
    parser.add_argument("--show", type=int, default=12, help="examples per list (0 = counts only)")
    parser.add_argument("--selected", action="store_true", help="print the whole new selection")
    args = parser.parse_args()

    with get_session_factory()() as session:
        for raw in args.scan:
            scan = session.get(Scan, uuid.UUID(raw))
            if scan is None:
                parser.error(f"scan {raw} not found")
            data = compare(session, scan, args.limit, args.competitor_limit)
            title = (scan.title or "")[:60]
            print(f"\n=== {raw[:8]} {title} ===")
            for source in data["sources"]:
                print(
                    f"  {source['code']} ({source['role']}): {source['propositions']} propositions, "
                    f"limit {source['limit']}, selected {source['selected']} "
                    f"(kept {source['kept']}, added {len(source['added'])}, "
                    f"dropped {len(source['dropped'])})"
                )
                if args.selected:
                    for claim in source["claims"]:
                        print(f"     = {_score(claim.statement):6.2f} {claim.statement[:110]}")
                if not args.show:
                    continue
                for claim in source["added"][: args.show]:
                    print(f"     + {_score(claim.statement):6.2f} {claim.statement[:110]}")
                for text in source["dropped"][: args.show]:
                    print(f"     -        {text[:110]}")
            print(
                f"  total: kept {data['kept']}, added {data['added']}, dropped {data['dropped']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
