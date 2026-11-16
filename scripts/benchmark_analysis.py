#!/usr/bin/env python
"""A/B the analysis modes on the same sites and compare report invariants.

The question this answers: does giving up the provider for everything but the
narrative change the *numbers* the report is built on? The prose is expected to
differ (that is the point); findings, scored/N-A criteria, data gaps, registers
and job failures are not.

    # sanity check without a provider or real sites (local fixture, mock mode)
    python scripts/benchmark_analysis.py --fixture --modes llm,hybrid

    # real sites, same industry, real money
    python scripts/benchmark_analysis.py \
        --site https://a.ru --site https://b.ru --site https://c.ru \
        --modes llm,hybrid --confirm

Runs in-process: creates a scan, crawls the site, drains the queue, reads the
registers and builds the report document (no PDF). Nothing is deleted, so every
run stays reviewable in the UI. The provider cache is disabled by default so the
second mode cannot inherit the first mode's answers — enable it with --use-cache
only when you are testing the cache itself.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select, text  # noqa: E402

from app.agents import execute_job  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_session_factory  # noqa: E402
from app.llm_usage import usage_summary  # noqa: E402
from app.models import Job, Scan  # noqa: E402
from app.pipeline import schedule_after_success  # noqa: E402
from app.queue import claim_next_job, complete_job, fail_job  # noqa: E402
from app.registers.models import (  # noqa: E402
    FindingV2,
    ReportNarrative,
    ScorecardEntry,
)
from app.reports.build_v2 import build_report_v2  # noqa: E402
from app.scans import add_website_source, create_scan, run_scan  # noqa: E402
from app.schemas import ScanCreate, WebsiteSourceCreate  # noqa: E402

INVARIANTS = ("findings", "finding_kinds", "scored", "na", "gaps", "sections", "failed_jobs")
# Set from --use-cache / --llm-agents; module-level switches keep run_one small.
CACHE_ENABLED = False
LLM_AGENTS = ""
LABELS = {
    "findings": "находки",
    "finding_kinds": "типы находок",
    "scored": "оценено критериев",
    "na": "N/A критериев",
    "gaps": "пробелы данных",
    "sections": "разделов",
    "failed_jobs": "упавших job",
    "narrative": "нарратив",
    "cost_usd": "стоимость, $",
    "calls": "LLM-вызовов",
    "seconds": "время, с",
}


def drain(
    session, worker_id: str, scan_id: uuid.UUID, timeout: float, idle_timeout: float = 60.0
) -> str:
    """Process jobs until the scan is terminal. Mirrors app/worker.py.

    The benchmark must be the only consumer of its own scan. If a running worker
    (the dev or public stack) picks the jobs up, our drain would sit idle until
    that worker's lease expires and the comparison would be made of that worker's
    answers, not ours. That case is reported instead of waited out.
    """
    settings = get_settings()
    deadline = time.monotonic() + timeout
    stalled_since: float | None = None
    while time.monotonic() < deadline:
        session.execute(
            text("SELECT set_config('app.job_lease_seconds', :lease, true)"),
            {"lease": str(settings.job_lease_seconds)},
        )
        job = claim_next_job(session, worker_id)
        if job is not None:
            stalled_since = None
        if job is None:
            session.commit()
            scan = session.get(Scan, scan_id)
            outstanding = session.scalar(
                select(func.count(Job.id)).where(
                    Job.scan_id == scan_id, Job.status.in_(("pending", "running"))
                )
            )
            if scan is not None and scan.status in {"succeeded", "failed", "cancelled"} and not outstanding:
                return scan.status
            foreign = session.scalar(
                select(func.count(Job.id)).where(
                    Job.scan_id == scan_id,
                    Job.status == "running",
                    Job.claimed_by.is_not(None),
                    Job.claimed_by != worker_id,
                )
            ) or 0
            if foreign:
                stalled_since = stalled_since or time.monotonic()
                if time.monotonic() - stalled_since > idle_timeout:
                    raise RuntimeError(
                        f"scan {scan_id}: {foreign} job(s) are being processed by another "
                        "worker. Stop the running stack (scripts/stop-dev.ps1 / stop-public.ps1) "
                        "or point DATABASE_URL at a dedicated database before benchmarking."
                    )
            time.sleep(1.0)
            continue
        try:
            execute_job(session, job)
            if complete_job(session, job):
                schedule_after_success(session, job)
                session.commit()
            else:
                session.rollback()
        except Exception as exc:  # noqa: BLE001 - the queue owns retry policy
            session.rollback()
            session.refresh(job)
            fail_job(session, job, exc)
            session.commit()
    raise TimeoutError(f"scan {scan_id} did not finish in {timeout:.0f}s")


def collect(session, scan: Scan) -> dict:
    finding_rows = session.execute(
        select(FindingV2.code, FindingV2.kind)
        .where(FindingV2.scan_id == scan.id)
        .order_by(FindingV2.code)
    ).all()
    kinds: dict[str, int] = {}
    for _, kind in finding_rows:
        kinds[kind] = kinds.get(kind, 0) + 1
    findings = len(finding_rows)
    scores = list(session.scalars(
        select(ScorecardEntry.score).where(ScorecardEntry.scan_id == scan.id)
    ))
    scored = sum(1 for score in scores if score is not None)
    job_statuses = dict(session.execute(
        select(Job.status, func.count(Job.id)).where(Job.scan_id == scan.id).group_by(Job.status)
    ).all())
    narrative = dict(session.execute(
        select(ReportNarrative.status, func.count(ReportNarrative.id))
        .where(ReportNarrative.scan_id == scan.id)
        .group_by(ReportNarrative.status)
    ).all())
    usage = usage_summary(session, scan.id)["totals"]

    gaps = sections = None
    if scan.status == "succeeded":
        try:
            document = build_report_v2(session, scan.id)
            sections = len(document.sections)
            gaps = sum(len(section.data_gaps) for section in document.sections)
        except Exception as exc:  # noqa: BLE001 - a broken report is a finding itself
            print(f"  ! report build failed: {type(exc).__name__}: {exc}")
    else:
        failures = session.execute(
            select(Job.agent, Job.last_error_message)
            .where(Job.scan_id == scan.id, Job.status == "failed")
            .order_by(Job.finished_at)
        ).all()
        for agent, message in failures[:3]:
            print(f"  ! {agent} failed: {message}")

    return {
        "scan_id": str(scan.id),
        "status": scan.status,
        "findings": findings,
        "finding_kinds": ",".join(f"{kind}x{count}" for kind, count in sorted(kinds.items())) or "-",
        "finding_codes": ",".join(f"{code}:{kind}" for code, kind in finding_rows) or "-",
        "scored": scored,
        "na": len(scores) - scored,
        "gaps": gaps,
        "sections": sections,
        "failed_jobs": int(job_statuses.get("failed", 0)),
        "narrative": ",".join(f"{key}:{value}" for key, value in sorted(narrative.items())) or "none",
        "llm_agents": scan.config_snapshot.get("analysis_llm_agents") or "all",
        "calls": int(usage["calls"]),
        "cost_usd": usage["cost_usd"],
        "config": {
            "analysis_mode": scan.config_snapshot.get("analysis_mode"),
            "analysis_llm_agents": scan.config_snapshot.get("analysis_llm_agents"),
            "model": scan.model_version,
        },
    }


def run_one(session, site_url: str, mode: str, *, max_pages: int, timeout: float, title: str) -> dict:
    settings = get_settings()
    settings.analysis_mode = mode  # noqa: SLF001 - the benchmark owns this process
    if mode == "hybrid" and LLM_AGENTS:
        settings.analysis_llm_agents = LLM_AGENTS
    settings.llm_cache_enabled = CACHE_ENABLED
    scan = create_scan(session, ScanCreate(title=f"AB {mode} {title}"))
    add_website_source(
        session, scan.id,
        WebsiteSourceCreate(url=site_url, max_pages=max_pages),
    )
    run_scan(session, scan.id)
    session.commit()
    started = time.monotonic()
    status = drain(session, f"ab-{uuid.uuid4().hex[:8]}", scan.id, timeout)
    session.expire_all()
    scan = session.get(Scan, scan.id)
    metrics = collect(session, scan)
    metrics["seconds"] = round(time.monotonic() - started, 1)
    metrics["mode"] = mode
    metrics["site"] = site_url
    if status != "succeeded":
        print(f"  ! scan {scan.id} ended as {status}: {scan.error_summary}")
    return metrics


def compare(site: str, base: dict, other: dict) -> tuple[bool, list[str]]:
    lines: list[str] = []
    same = True
    for field in INVARIANTS:
        before, after = base[field], other[field]
        marker = "=" if before == after else "!="
        if before != after:
            same = False
        lines.append(f"  {LABELS[field]:<20} {before} {marker} {after}")
    lines.append(f"  {LABELS['narrative']:<20} {base['narrative']} -> {other['narrative']} (ожидаемо)")
    lines.append(f"  {LABELS['calls']:<20} {base['calls']} -> {other['calls']}")
    lines.append(f"  {LABELS['cost_usd']:<20} {base['cost_usd']} -> {other['cost_usd']}")
    lines.append(f"  {LABELS['seconds']:<20} {base.get('seconds')} -> {other.get('seconds')}")
    verdict = "инварианты совпадают" if same else "ИНВАРИАНТЫ РАСХОДЯТСЯ"
    lines.append(f"  вердикт: {verdict}")
    return same, lines


def _load_baseline(*, session_scan_id: str, parser) -> dict:
    import uuid as _uuid

    with get_session_factory()() as session:
        scan = session.get(Scan, _uuid.UUID(session_scan_id))
        if scan is None:
            parser.error(f"baseline scan {session_scan_id} not found")
        return collect(session, scan)


def main() -> int:
    global CACHE_ENABLED, LLM_AGENTS  # noqa: PLW0603 - single-run CLI switch
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--site", action="append", default=[], help="site URL (repeatable)")
    parser.add_argument("--sites", default="", help="comma-separated site URLs")
    parser.add_argument("--fixture", action="store_true", help="use the local smoke-test fixture site")
    parser.add_argument("--modes", default="llm,hybrid", help="comma-separated modes (default: llm,hybrid)")
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=3600.0, help="seconds per scan")
    parser.add_argument("--out", default="logs/analysis-ab.json")
    parser.add_argument("--use-cache", action="store_true", help="keep the provider cache on between runs")
    parser.add_argument("--min-interval", type=float, default=1.0,
                        help="seconds between provider requests (rate-limited gateways)")
    parser.add_argument("--cooldown", type=float, default=30.0,
                        help="pause after exhausted 429 retries (seconds)")
    parser.add_argument("--transport-retries", type=int, default=3)
    parser.add_argument("--max-failures", type=int, default=1,
                        help="stop the run after N failed scans (0 = never)")
    parser.add_argument("--llm-agents", default="",
                        help="override ANALYSIS_LLM_AGENTS for hybrid runs "
                             "(e.g. NARRATIVE,EVIDENCE,FINDINGS)")
    parser.add_argument("--baseline-scan", default="",
                        help="compare every run to this existing scan instead of mode-vs-mode")
    parser.add_argument("--confirm", action="store_true", help="required for a real provider")
    args = parser.parse_args()

    CACHE_ENABLED = args.use_cache
    LLM_AGENTS = args.llm_agents.strip()
    settings = get_settings()
    sites = list(args.site) + [item.strip() for item in args.sites.split(",") if item.strip()]
    modes = [item.strip() for item in args.modes.split(",") if item.strip()]
    unsupported = [mode for mode in modes if mode not in {"llm", "hybrid", "deterministic"}]
    if unsupported:
        parser.error(f"unknown mode(s): {', '.join(unsupported)}")

    if args.fixture:
        from smoke_test import start_fixture_site

        sites.append(start_fixture_site())
        if settings.llm_provider != "mock":
            print("warning: --fixture is meant for LLM_PROVIDER=mock; a real provider will be charged")
    if not sites:
        parser.error("pass --site/--sites or --fixture")
    if settings.llm_provider != "mock" and not args.confirm:
        parser.error(
            "LLM_PROVIDER is not mock: this run spends real money. "
            "Re-run with --confirm to proceed."
        )
    if settings.llm_provider != "mock" and not CACHE_ENABLED:
        print("note: provider cache disabled for the A/B (will be re-enabled afterwards)")
    settings.llm_min_interval_seconds = max(0.0, args.min_interval)
    settings.llm_cooldown_seconds = max(0.0, args.cooldown)
    settings.llm_transport_retries = max(settings.llm_transport_retries, args.transport_retries)
    print(f"pacing: min_interval={settings.llm_min_interval_seconds}s "
          f"cooldown={settings.llm_cooldown_seconds}s retries={settings.llm_transport_retries}")

    print(f"provider={settings.llm_provider} model={settings.llm_model} "
          f"modes={modes} sites={len(sites)} cache={CACHE_ENABLED}")
    results: list[dict] = []
    same_count = 0
    failures = 0
    for site in sites:
        runs: dict[str, dict] = {}
        for mode in modes:
            print(f"> {site} [{mode}]")
            with get_session_factory()() as session:
                metrics = run_one(
                    session, site, mode,
                    max_pages=args.max_pages, timeout=args.timeout, title=site[:80],
                )
            runs[mode] = metrics
            results.append(metrics)
            if metrics["status"] != "succeeded":
                failures += 1
            print(
                f"  scan={metrics['scan_id']} status={metrics['status']} "
                f"time={metrics['seconds']}s findings={metrics['findings']} "
                f"scored={metrics['scored']} na={metrics['na']} gaps={metrics['gaps']} "
                f"narrative={metrics['narrative']} cost={metrics['cost_usd']}"
            )
            if args.max_failures and failures >= args.max_failures:
                print(
                    f"\nstopping: {failures} scan(s) failed. If the provider answered 429, "
                    "wait for its window to reset (or lower --max-pages) and retry; "
                    "the report-build invariants need a succeeded scan."
                )
                break
        if args.max_failures and failures >= args.max_failures:
            break
        base = runs[modes[0]]
        for mode in modes[1:]:
            same, lines = compare(site, base, runs[mode])
            same_count += 1 if same else 0
            print(f"\n=== {site}: {mode} vs {modes[0]} ===")
            print("\n".join(lines))

    if args.baseline_scan:
        baseline = _load_baseline(session_scan_id=args.baseline_scan, parser=parser)
        print(f"\n=== baseline {args.baseline_scan}: {baseline['finding_codes']} ===")
        healthy = 0
        for result in results:
            if result["status"] != "succeeded":
                continue
            same, lines = compare(result["site"], baseline, result)
            healthy += 1 if same else 0
            print(f"\n--- scan {result['scan_id']} ({result['llm_agents']}) vs baseline ---")
            print("\n".join(lines))
        print(f"\ninvariants matched against the baseline in {healthy}/{len(results)} runs")

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nraw results: {out}")
    comparisons = len(sites) * max(0, len(modes) - 1)
    print(f"invariants matched in {same_count}/{comparisons} comparisons")
    print("Next: open the scans in /ui/scans and read §1/§17 side by side; a matching "
          "invariant set with readable prose is the green light for ANALYSIS_MODE=hybrid.")
    return 0 if same_count == comparisons else 1


if __name__ == "__main__":
    raise SystemExit(main())
