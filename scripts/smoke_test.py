import argparse
import http.server
import threading
import time
import uuid

import httpx

FIXTURE_SITE = {
    "/": """<html><head><title>Fixture site</title><style>b{}</style></head><body>
        <nav>menu menu</nav>
        <h1>Мы делаем аудит нарратива</h1>
        <p>Небольшие команды теряют обратную связь от клиентов.</p>
        <p>StoryAudit собирает цитаты и показывает разрывы в истории бренда.</p>
        <a href="/about">О нас</a>
        <script>var x = 1;</script>
        </body></html>""",
    "/about": """<html><head><title>О нас</title></head><body>
        <h2>Команда</h2>
        <p>Мы помогаем маркетингу видеть, какую историю рассказывает сайт.</p>
        </body></html>""",
}


class FixtureHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = FIXTURE_SITE.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # keep the smoke output clean
        return


def start_fixture_site() -> str:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--token", default="local-test-token")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--site-url", default=None, help="Real site to analyze (default: local fixture)")
    args = parser.parse_args()
    client = httpx.Client(base_url=args.base_url, headers={"Authorization": f"Bearer {args.token}"}, timeout=30)
    client.get("/health/live").raise_for_status()
    client.get("/health/ready").raise_for_status()
    site_url = args.site_url or start_fixture_site()
    scan = client.post("/scans", json={"title": f"Smoke {uuid.uuid4()}"})
    scan.raise_for_status()
    scan_id = scan.json()["id"]
    source = client.post(f"/scans/{scan_id}/sources", json={"url": site_url, "max_pages": 3})
    source.raise_for_status()
    client.post(f"/scans/{scan_id}/run").raise_for_status()
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        status = client.get(f"/scans/{scan_id}")
        status.raise_for_status()
        payload = status.json()
        if payload["status"] == "succeeded":
            break
        if payload["status"] in {"failed", "cancelled"}:
            raise RuntimeError(f"Scan failed: {payload}")
        time.sleep(2)
    else:
        raise TimeoutError("Scan did not finish")
    metrics = client.get(f"/scans/{scan_id}/metrics")
    metrics.raise_for_status()
    assert len(metrics.json()) == 4, metrics.json()
    detail = client.get(f"/scans/{scan_id}/metrics/{metrics.json()[0]['id']}")
    detail.raise_for_status()
    annotations = client.get(f"/scans/{scan_id}/annotations").json()
    proposition_id = next(item["object_id"] for item in annotations if item["agent"] == "A2")
    reanalysis = client.post(f"/scans/{scan_id}/reanalysis", json={
        "agent": "A3", "object_type": "proposition", "object_id": proposition_id,
        "issue_id": str(uuid.uuid4()), "client_request_id": str(uuid.uuid4()),
    })
    reanalysis.raise_for_status()
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        status = client.get(f"/scans/{scan_id}")
        status.raise_for_status()
        if status.json()["status"] == "succeeded":
            break
        if status.json()["status"] in {"failed", "cancelled"}:
            raise RuntimeError(f"Reanalysis failed: {status.json()}")
        time.sleep(2)
    else:
        raise TimeoutError("Reanalysis did not finish")
    print(
        f"Smoke test passed: scan={scan_id}, site={site_url}, pages={source.json()['pages']}, "
        f"metrics={len(metrics.json())}, reanalysis={reanalysis.json()['request_id']}"
    )


if __name__ == "__main__":
    main()
