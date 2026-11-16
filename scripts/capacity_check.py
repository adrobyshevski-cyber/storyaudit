#!/usr/bin/env python
"""Capacity check: how many real scans in a row can the provider gateway take?

The gateway (opencode.ai) throttles by spend/tokens per window and answers 429 for
tens of minutes once the window is exhausted. This script runs N scans back to back
in-process, counts every 429 the transport returns (including ones that are retried
and hidden), and prints the per-scan cost/time plus a projection for the public
form's daily limit.

    DATABASE_URL=postgresql+psycopg://storyaudit:storyaudit@localhost:5432/storyaudit_test \
    .venv\\Scripts\\python.exe scripts/capacity_check.py --scans 10 --confirm

Run it against a database no worker is polling: the script drains its own jobs.
The provider cache is disabled so every scan really pays for its calls — that is the
load the gateway sees when ten different clients submit ten different sites.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from sqlalchemy import select  # noqa: E402

import app.llm as llm_module  # noqa: E402
import benchmark_analysis as bench  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_session_factory  # noqa: E402
from app.models import Job  # noqa: E402

DEFAULT_SITES = ["https://www.issoft.by", "https://leverx.com", "https://exadel.com"]
USD_TO_RUB = 92.0


def count_429s() -> int:
    return _LIMITED["count"]


_LIMITED = {"count": 0}


def install_429_counter() -> None:
    real_post = llm_module.httpx.post

    def counting_post(url, **kwargs):
        response = real_post(url, **kwargs)
        if getattr(response, "status_code", 200) == 429:
            _LIMITED["count"] += 1
        return response

    llm_module.httpx.post = counting_post


def scan_errors(session, scan_id) -> list[str]:
    rows = session.execute(
        select(Job.agent, Job.last_error_message)
        .where(Job.scan_id == scan_id, Job.status == "failed")
        .order_by(Job.finished_at)
    ).all()
    return [f"{agent}: {message}" for agent, message in rows[:3]]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--site", action="append", default=[], help="site URL (repeatable)")
    parser.add_argument("--scans", type=int, default=10)
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("--min-interval", type=float, default=2.0)
    parser.add_argument("--cooldown", type=float, default=60.0)
    parser.add_argument("--transport-retries", type=int, default=4)
    parser.add_argument("--max-consecutive-failures", type=int, default=3)
    parser.add_argument("--out", default="logs/capacity.json")
    parser.add_argument("--confirm", action="store_true", help="required: this spends money")
    args = parser.parse_args()

    settings = get_settings()
    sites = args.site or DEFAULT_SITES
    if settings.llm_provider != "mock" and not args.confirm:
        parser.error(
            f"LLM_PROVIDER={settings.llm_provider}: this run spends real money. "
            "Re-run with --confirm."
        )
    bench.CACHE_ENABLED = False
    bench.LLM_AGENTS = ""
    settings.llm_cache_enabled = False
    settings.llm_min_interval_seconds = max(0.0, args.min_interval)
    settings.llm_cooldown_seconds = max(0.0, args.cooldown)
    settings.llm_transport_retries = max(settings.llm_transport_retries, args.transport_retries)

    print(
        f"provider={settings.llm_provider} mode=hybrid agents={settings.analysis_llm_agents} "
        f"scans={args.scans} sites={len(sites)} cache=off "
        f"min_interval={settings.llm_min_interval_seconds}s cooldown={settings.llm_cooldown_seconds}s"
    )
    install_429_counter()

    results: list[dict] = []
    consecutive = 0
    started_all = time.monotonic()
    for index in range(1, args.scans + 1):
        site = sites[(index - 1) % len(sites)]
        before = count_429s()
        print(f"#{index} {site}", flush=True)
        try:
            with get_session_factory()() as session:
                metrics = bench.run_one(
                    session, site, "hybrid",
                    max_pages=args.max_pages, timeout=args.timeout,
                    title=f"capacity #{index} {site[:60]}",
                )
                errors = scan_errors(session, uuid.UUID(metrics["scan_id"]))
        except Exception as exc:  # noqa: BLE001 - a failed scan is a data point here
            metrics = {"status": "error", "scan_id": "", "findings": 0, "cost_usd": None,
                       "seconds": round(time.monotonic() - started_all, 1)}
            errors = [f"{type(exc).__name__}: {exc}"]
        metrics["limited_429"] = count_429s() - before
        metrics["errors"] = errors
        metrics["site"] = site
        results.append(metrics)
        if metrics["status"] == "succeeded":
            consecutive = 0
        else:
            consecutive += 1
        print(
            f"  -> {metrics['status']} time={metrics['seconds']}s cost={metrics.get('cost_usd')} "
            f"calls={metrics.get('calls')} findings={metrics.get('findings')} "
            f"429={metrics['limited_429']}"
        )
        for error in errors:
            print(f"     ! {error[:200]}")
        if consecutive >= args.max_consecutive_failures:
            print(f"stopping after {consecutive} consecutive failures")
            break

    succeeded = [item for item in results if item["status"] == "succeeded"]
    total_cost = sum(float(item.get("cost_usd") or 0) for item in results)
    total_limited = sum(item["limited_429"] for item in results)
    total_time = time.monotonic() - started_all
    per_scan_cost = total_cost / len(succeeded) if succeeded else 0.0
    per_scan_time = total_time / len(results) if results else 0.0

    print("\n=== summary ===")
    print(f"scans: {len(results)}  succeeded: {len(succeeded)}  failed: {len(results) - len(succeeded)}")
    print(f"429 responses (including retried): {total_limited}")
    print(f"cost: ${total_cost:.4f} total, ${per_scan_cost:.4f} per successful scan")
    print(f"time: {total_time / 60:.1f} min total, {per_scan_time:.0f} s per scan")
    for limit in (20, 30):
        daily = per_scan_cost * limit
        print(
            f"at {limit} scans/day: ${daily:.2f}/day = ${daily * 30:.2f}/month "
            f"(~{daily * 30 * USD_TO_RUB:,.0f} RUB), "
            f"{per_scan_time * limit / 3600:.1f} h/day of worker time"
        )

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nraw results: {out}")
    return 0 if len(succeeded) == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
