"""End-to-end check of the public landing -> free audit -> Gate -> report flow.

Run it after deploying to Render (it spends one real LLM scan):

    python scripts/public_smoke.py \
        --base-url https://audit.example.com \
        --token "$APP_ACCESS_TOKEN" \
        --site-url https://www.example.com

The public side is exercised without the access token; the token is used only to
approve the Stage Gate, because publication is deliberately gated by an analyst.
"""

from __future__ import annotations

import argparse
import sys
import time
from urllib.parse import urlsplit

import httpx


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True, help="Public HTTPS origin of the service")
    parser.add_argument("--token", required=True, help="APP_ACCESS_TOKEN for the Gate decision")
    parser.add_argument("--site-url", required=True, help="Real public site to audit")
    parser.add_argument("--actor", default="public-smoke")
    parser.add_argument("--scan-id", default=None, help="Skip scan lookup by title")
    parser.add_argument("--timeout", type=int, default=1800, help="Seconds to wait for review")
    parser.add_argument("--report-timeout", type=int, default=120)
    parser.add_argument("--no-approve", action="store_true", help="Stop at the review state")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    origin = f"{urlsplit(base).scheme}://{urlsplit(base).netloc}"
    public = httpx.Client(base_url=base, timeout=60, follow_redirects=False)
    admin = httpx.Client(
        base_url=base,
        timeout=60,
        headers={"Authorization": f"Bearer {args.token}"},
    )

    landing = public.get("/")
    if landing.status_code != 200 or "audit-form" not in landing.text:
        fail(f"landing page is not the public form (HTTP {landing.status_code})")
    print("ok: landing page")

    response = public.post(
        "/audit",
        data={"website": args.site_url, "consent": "on"},
        headers={"origin": origin},
    )
    if response.status_code != 303 or not response.headers.get("location", "").startswith("/audit/"):
        fail(f"request form rejected: HTTP {response.status_code} {response.text[:300]}")
    token = response.headers["location"].rsplit("/", 1)[-1]
    print(f"ok: request accepted, status page /audit/{token[:8]}…")

    state_url = f"/audit/{token}/state"
    deadline = time.monotonic() + args.timeout
    state = {}
    while time.monotonic() < deadline:
        payload = public.get(state_url)
        if payload.status_code != 200:
            fail(f"status endpoint returned HTTP {payload.status_code}")
        state = payload.json()
        print(f"   state: {state['state']}")
        if state["state"] in {"review", "ready", "failed", "expired"}:
            break
        time.sleep(10)
    else:
        fail("audit did not reach review within the timeout")

    if state["state"] == "failed":
        fail(f"scan failed: {state.get('error')}")
    if state["state"] == "expired":
        fail("the capability link expired")
    if args.no_approve:
        print("ok: scan finished; Gate left for a human (--no-approve)")
        return

    scan_id = args.scan_id
    if scan_id is None:
        scans = admin.get("/scans", params={"limit": 200})
        if scans.status_code != 200:
            fail(f"cannot list scans: HTTP {scans.status_code}")
        host = urlsplit(args.site_url).hostname or args.site_url
        matches = [item for item in scans.json() if item["title"] == f"Narrative Audit · {host}"]
        if not matches:
            fail("scan for this site was not found; pass --scan-id explicitly")
        scan_id = matches[0]["id"]
    print(f"ok: scan {scan_id}")

    if state["state"] != "ready":
        gate = admin.post(
            f"/scans/{scan_id}/stage-gate",
            json={
                "actor": args.actor,
                "decision": "ready",
                "rationale": "Public smoke test: report structure reviewed automatically.",
                "blockers": [],
                "required_validations": [],
            },
        )
        if gate.status_code != 200:
            fail(f"Gate decision failed: HTTP {gate.status_code} {gate.text[:300]}")
        print("ok: Stage Gate approved")

    deadline = time.monotonic() + args.report_timeout
    while time.monotonic() < deadline:
        state = public.get(state_url).json()
        if state["state"] == "ready":
            break
        time.sleep(3)
    else:
        fail("public state did not become ready after Gate approval")

    report = public.get(f"/audit/{token}/report")
    if report.status_code != 200 or "NARRATIVE" not in report.text:
        fail(f"report page failed: HTTP {report.status_code}")
    print("ok: public report HTML")

    pdf = public.get(f"/audit/{token}/report.pdf")
    if pdf.status_code != 200 or not pdf.content.startswith(b"%PDF"):
        fail(f"report PDF failed: HTTP {pdf.status_code}")
    print(f"ok: public report PDF ({len(pdf.content) // 1024} KiB)")
    print("Public smoke test passed.")


if __name__ == "__main__":
    main()
