# StoryAudit

> Клиентский отчёт переводится на `report_v2` (evidence-first, SoCode Narrative Audit v1.1):
> см. `docs/architecture-report-v2.md`. Отчёты FREE/FULL — устаревший формат.

FastAPI + PostgreSQL implementation of the StoryAudit runtime. It includes an immutable corpus, versioned annotations, a PostgreSQL job queue, parallel fan-out, targeted reanalysis, deterministic profiles/calculations, evidence provenance and a minimal UI.

## Local start

This machine already has everything installed:

| Component | Location | Version |
|---|---|---|
| Python | `%LOCALAPPDATA%\Programs\Python\Python312` (on PATH) | 3.12.10 |
| PostgreSQL (portable, no admin needed) | `C:\work\pgsql` | 16.9 |
| Database cluster | `C:\work\pgdata` | port 5432 |
| Virtualenv | `.venv` | Python 3.12.10 |
| Databases | `storyaudit`, `storyaudit_test` | user `storyaudit`, trust auth |

### Start / stop everything

```powershell
cd C:\work\narrative_audit
powershell -ExecutionPolicy Bypass -File scripts\start-dev.ps1
powershell -ExecutionPolicy Bypass -File scripts\stop-dev.ps1
```

`start-dev.ps1` starts PostgreSQL, the API on <http://127.0.0.1:8000> and the worker, then prints health checks.

`start-dev.ps1 -NoBrowser` skips opening the browser. To open the UI separately (token is read from `.env`):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\open-ui.ps1
```

UI: <http://127.0.0.1:8000/ui/scans>


```text
Username: audit
Password: local-test-token
```

Logs: `logs/api.err.log`, `logs/worker.err.log`, PostgreSQL: `C:\work\pg.log`.

### Product scope (MVP)

This version analyses **websites only**. The input is a site URL; the server fetches the pages
and extracts readable text into the immutable corpus. File upload and pasted text are not part
of the product flow.

Site fetching settings (`.env`):

```text
WEBSITE_MAX_PAGES=5               # default crawl budget per scan (1..20 per request)
WEBSITE_FETCH_TIMEOUT_SECONDS=15  # per-request HTTP timeout
WEBSITE_ALLOW_PRIVATE_HOSTS=false # keep false in production: blocks SSRF to internal hosts
```

The local `.env` enables private hosts because `scripts/smoke_test.py` serves a fixture site on
`127.0.0.1`. Keep the Render value at `false`.


### LLM routing and cost accounting

Stages are routed by model: `LLM_FAST_AGENTS` (default `A1,A2,A3,A4`, one call per proposition)
use `LLM_MODEL_FAST`; synthesis and the client narrative use `LLM_MODEL_SMART`. Empty values fall
back to `LLM_MODEL`.

A provider answer that is not valid JSON, or does not match the stage schema, is sent back to the
provider with the validation error (`LLM_SCHEMA_REPAIR_ATTEMPTS`, default 2 rounds). Retryable HTTP
statuses (429/5xx) are retried with backoff and `Retry-After`; 4xx auth/validation errors fail fast.

Every request is recorded in `llm_usage` (migration `0006`), including mock runs and repair rounds.

```text
GET /scans/{id}/usage   tokens, cached tokens, calls, repairs, cost estimate per agent/model/generation
```

Set `LLM_PRICE_INPUT_PER_MTOK` / `LLM_PRICE_OUTPUT_PER_MTOK` (USD) to get `cost_usd`;
with the defaults (0) the cost field is `null` instead of a made-up number.

Annotation stages `A2/A3/A4` are batched: `ANNOTATION_BATCH_SIZE` propositions per provider call
and per job (default 20, `1` disables batching). Annotations are still stored one row per
proposition, so profiles, metrics and the report are unchanged. A batch answer that misses,
repeats or invents a `proposition_id` is sent back to the provider as a repair round.


### Retention

Derived data grows without bound: PDF snapshots live in PostgreSQL (1-3 MB each), one
`llm_usage` row is written per provider request, and every reanalysis adds a superseded
annotation generation. Nothing in retention touches the corpus, current annotations,
findings or registers.

```bash
python scripts/prune.py                  # dry run: what would be deleted, plus table sizes
python scripts/prune.py --apply          # delete
python scripts/prune.py --apply --vacuum # delete and VACUUM FULL (reclaims disk, locks tables)
```

Rules (all configurable):

| Setting | Default | What it prunes |
|---|---|---|
| `RETENTION_REPORT_ARTIFACTS_DAYS` / `_KEEP` | 90 / 3 | PDF snapshots older than N days, but the newest 3 per scan and always at least one |
| `RETENTION_LLM_USAGE_DAYS` | 180 | token/cost accounting rows |
| `RETENTION_ANNOTATIONS_DAYS` | 90 | superseded (`is_current = false`) annotation versions, with dangling `supersedes_annotation_id` links cleared first |

`RETENTION_ENABLED=true` makes the worker apply the same rules every
`RETENTION_INTERVAL_SECONDS` (and once at boot). It is off by default so local dev keeps
its data. A dry run never writes anything.

### Reports (HTML / PDF)

A finished scan exposes the single client report — `report_v2` (evidence-first, SoCode Narrative
Audit v1.1). The legacy FREE/FULL format is retired; `?type=` is accepted and ignored with a
`Deprecation: true` header.

```text
GET  /scans/{id}/report            HTML (browser preview)
GET  /scans/{id}/report.pdf        PDF (Chromium via Playwright, appendices in landscape)
GET  /scans/{id}/report.json       report_v2 as a machine-readable contract
POST /scans/{id}/report/build      rebuild and store a snapshot (content-hash keyed)
GET  /ui/scans/{id}/report         UI preview
GET  /ui/scans/{id}/report.pdf     UI download
```

In the UI open a `succeeded` scan and use «Нарративный аудит» / «Скачать PDF».

The report always has the full 17-section structure from the methodology plus four appendices.
Sections the pipeline cannot fill yet are rendered as explicit «Ограничения данных» with reason
and consequence; the scorecard uses N/A where data is missing, and no aggregate index is
computed. `app/reports/build_v2.py` builds the document from the registers in
`app/registers/models.py`; `docs/report-v2-schema.md` documents the schema and invariants.

PDF rendering needs Chromium:

```powershell
# uses the locally installed Chrome by default (PLAYWRIGHT_CHANNEL=chrome)
powershell -ExecutionPolicy Bypass -File scripts\start-dev.ps1
```

On a host without Chrome install the bundled browser once: `playwright install chromium`
(the renderer falls back to it automatically).

Notes:
- PDF rendering happens inside the API process (`report_pdf_timeout_seconds` default 60 s).
  Rendered artifacts are cached in PostgreSQL by `scan_id + content_hash` (`report_artifacts`),
  so unchanged reports are rendered once. Responses expose `X-Report-Cache: HIT|MISS` and an ETag.
  Table of contents page numbers are resolved in a two-pass Chromium render.
- On Windows uvicorn uses the selector event loop, which cannot spawn subprocesses; the renderer
  temporarily switches the policy to the proactor loop for the duration of the render.

### Registers (phase B)

After `A1` the pipeline fans out into the evidence-first roles, in parallel with the legacy
metric chain (which now only feeds `report_v1` behind `scans.internal_metrics`):

```text
A1 ──> VERIFY   (per source)   live-check + availability/currency in source_registry
   └─> PRODUCT  (per scan)     products with verbatim price/duration/audience
VERIFY ──> EVIDENCE (per source)  F statements + E records (publication_only)
EVIDENCE ──> LANGUAGE (per scan)  brand vocabulary; lived language stays unknown (Desk)
```

Rules enforced in code, not in prompts:

- live-check failure is recorded as a limitation, never as "the page is gone";
- every evidence record says `verification=publication_only` and
  `allowed_conclusion="Бренд публично заявляет: ..."`;
- language map leaves `lived` empty and `match_status=no_data` without interviews;
- a scan becomes `succeeded` only when **no** initial job of the generation is unfinished,
  so the report never reads a half-built register set.

Settings: `REPORT_LIVE_CHECK`, `REPORT_LIVE_CHECK_TIMEOUT_SECONDS`, `REPORT_MAX_STATEMENTS`,
`REPORT_MAX_PRODUCTS`. Tests set `REPORT_LIVE_CHECK=false`; the checker itself is covered by
a dedicated unit test with a stubbed probe.

### Diagnosis (phase C)

```text
EVIDENCE ──> LANGUAGE + ACTANT (per product) + JOURNEY
ACTANT + JOURNEY + EVIDENCE ──> CONSISTENCY ──> FINDINGS ──> CRITIC
```

- `ACTANT`: six Greimas roles plus three Object levels; emotional/transformational levels stay
  `U` without interviews, and each product gets a `LayerClaim` where Experienced/Perceived are
  `unavailable` (Desk).
- `JOURNEY`: eight steps, `is_observed=false`; `experience`/`referral` stay "нет данных".
- `CONSISTENCY`: 4×4 matrix; the `experience` dimension is forced to `no_data` in Desk mode.
- `FINDINGS`: `G` needs two incompatible observable sides (two prices for the *same* offer is
  a contradiction, two different offers are not), `M` is a missing link, `W` is a claim without
  support. Zero findings is a valid result and is rendered as "Находок не обнаружено", not as
  a data gap.
- `CRITIC`: fills alternative explanations and flags W / historical / low-confidence findings
  as `needs_review`; it never re-classifies silently.

### Scorecard, plan and narrative (phase D)

```text
CRITIC -> SCORECARD -> RECOMMEND -> NARRATIVE
```

- `scorecard.py`: 15 criteria across clarity/consistency/credibility/resonance; a score is 0–10
  with confidence, or `N/A` with a reason (`confidence=null`). Experience and real recognition are
  never scored in Desk mode. No average and no index exist in the model.
- `recommendations.py`: each R links to a finding, owns a KPI (`baseline=unknown`) and qualitative
  H/M/L impact/confidence/urgency/effort. `expected_effect_status` is always `H`. Decision classes
  are keep/fix/test/stop; roadmap has five phases; hypotheses and the Stage 02 gate are generated.
- `narrative.py`: the LLM editor writes sections 1 and 17. A deterministic validator rejects
  unknown references, numbers missing from the registers, guaranteed-result promises, numeric
  forecasts, "unique" claims and pseudo-index language. On rejection the section falls back to a
  register-based template with status `fallback`.
- `audit_phase` becomes `narrative_draft`; the report stays `internal_draft` until a human approves
  the gate (`approved_by`).

### Competitor landscape (phase E)

```text
sources (role=competitor) -> crawl (robots.txt + delay) -> LANDSCAPE -> §4 + resonance
```

- `POST /scans/{id}/sources` accepts `role="competitor"` and requires `selection_reason`;
  at most `LANDSCAPE_MAX_COMPETITORS=5` alternatives, at most `LANDSCAPE_MAX_PAGES` pages each.
- `app/website.py` honours `robots.txt` (`urllib.robotparser`; an unreachable robots.txt allows
  crawling) and waits `WEBSITE_FETCH_DELAY_SECONDS` / `LANDSCAPE_FETCH_DELAY_SECONDS` between
  requests.
- `app/analysis/landscape.py` extracts promise/opponent/object/helper/proof/price verbatim and
  keeps the inclusion reason from the source metadata.
- `overlap_with_competitors` compares key promise terms; the scorecard uses it to replace the
  `N/A` for relative uniqueness (3/5/7), always citing the C codes.
- A weak-evidence finding appears only when the promise actually overlaps an alternative; a
  higher own price makes it P1. The landscape never turns into a market median.

### Human review (phase F)

Open **«Реестры и ревью»** on a succeeded scan, or use `/ui/scans/{id}/review`. The review page
contains Client Brief v2.0, open questions, Stage Gate, editable registers and a before/after
event history. Register edits do not mutate the immutable corpus and do not rerun LLM stages.
They mark persisted snapshots `needs_rebuild`; any new edit after approval returns the audit to
`analyst_review` and clears the gate approval.

```text
GET    /scans/{id}/registers                           editable register contracts
GET    /scans/{id}/registers/{register}                entries
PATCH  /scans/{id}/registers/{register}/{entity_id}    {actor, note, changes}
GET    /scans/{id}/review-events
GET    /scans/{id}/client-brief
PUT    /scans/{id}/client-brief                        {actor, note, brief}
GET    /scans/{id}/open-questions
POST   /scans/{id}/open-questions
PATCH  /scans/{id}/open-questions/{question_id}
POST   /scans/{id}/review                              start/approve/reject/reopen/needs_review
POST   /scans/{id}/stage-gate                          analyst decision and blockers
```

Registers: `sources`, `products`, `evidence`, `findings`, `scorecard`, `recommendations`,
`journey`, `language`, `hypotheses`. Every write requires `actor` and creates `review_events`.
A `ready` gate with open blockers is rejected; only a blocker-free `ready` decision produces
`client_ready`. Conditional and not-ready decisions remain `internal_draft`. In Desk mode,
observed experience and lived language cannot be asserted. Marking a source disputed flags
dependent statements, evidence, findings, recommendations and scorecard rows for review.

### Manual commands

```powershell
cd C:\work\narrative_audit
.venv\Scripts\Activate.ps1
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
# second terminal
python -m app.worker
```

The default `LLM_PROVIDER=mock` requires no external LLM and runs the complete pipeline deterministically. It is intended for local development and CI, and is forbidden when `APP_ENV=production`.

### Optional: Docker instead of portable PostgreSQL

Docker Desktop is not installed (it needs admin rights and a reboot). If you install it later, `docker compose up -d --wait` replaces `C:\work\pgsql`; keep the same `DATABASE_URL` from `.env.example` (`storyaudit:storyaudit@localhost:5432/storyaudit`).

## Smoke test

With API and worker running:

```powershell
.venv\Scripts\python.exe scripts\smoke_test.py
```

It creates a scan, runs the whole pipeline, checks 4 current metrics and performs a targeted A3 reanalysis.

## Tests

```powershell
$env:TEST_DATABASE_URL = "postgresql+psycopg://storyaudit:storyaudit@localhost:5432/storyaudit_test"
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest -q
```

Expected result: `78 passed`. PostgreSQL end-to-end tests run when `TEST_DATABASE_URL` is set; otherwise they are skipped. Never run migrations or tests against production data.

## Real LLM

Set in `.env` or Render worker secrets:

```text
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://provider.example/v1
LLM_MODEL=exact-model-name
LLM_API_KEY=secret
LLM_MODEL_FAST=cheap-model-for-volume-stages
LLM_MODEL_SMART=stronger-model-for-synthesis
LLM_PRICE_INPUT_PER_MTOK=0.10
LLM_PRICE_OUTPUT_PER_MTOK=0.40
```

`LLM_BASE_URL` can either end with `/v1` or contain the full `/chat/completions` path. The provider
must support the Chat Completions JSON-object response format. `LLM_MODEL_FAST` / `LLM_MODEL_SMART`
are optional (empty = `LLM_MODEL`); prices are USD per 1M tokens and only drive the cost estimate
in `GET /scans/{id}/usage`.

## GitLab

The repository is forge-neutral: GitHub Actions live in `.github/workflows/test.yml`, the equivalent
GitLab pipeline in `.gitlab-ci.yml` (python:3.12-slim + `postgres:16` service, migrations, ruff,
Chromium install, pytest). Both are green on the same commit.

Create an empty project in GitLab, then:

```bash
git remote add origin git@gitlab.com:<group>/storyaudit.git   # or https://gitlab.com/<group>/storyaudit.git
git push -u origin main
```

HTTPS push asks for a Personal Access Token with `write_repository` scope (GitLab no longer accepts
an account password). SSH requires the public key in GitLab → Preferences → SSH Keys.
The pipeline starts on the first push; no CI variables are required (`.gitlab-ci.yml` defines
`APP_ENV`, `APP_ACCESS_TOKEN`, `LLM_PROVIDER=mock`, `DATABASE_URL`, `TEST_DATABASE_URL`).

`render.yaml` works with a GitLab repository as well — Render connects to GitLab and reads
`render.yaml` from the default branch the same way.

## Render

1. Push this directory as a repository root (GitHub or GitLab).
2. Select **New → Blueprint** in Render.
3. Apply `render.yaml` — it defines the web service, the worker and PostgreSQL.
4. Enter the `sync: false` secrets when prompted:
   `APP_ACCESS_TOKEN`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_MODEL_FAST`, `LLM_MODEL_SMART`,
   `LLM_API_KEY`, `LLM_PRICE_INPUT_PER_MTOK`, `LLM_PRICE_OUTPUT_PER_MTOK`.

Notes on the blueprint:

- Only the **web** service installs Chromium and runs `alembic upgrade head`
  (`preDeployCommand`); the worker never renders PDF.
- Both services get `LLM_PROVIDER` / `LLM_MODEL*`, because a scan records its
  `model_version` and `config_snapshot` at creation time on the web service.
  No `LLM_API_KEY` is given to the web service: it never calls the provider.
- `PYTHON_VERSION` is a floating `3.12`, so Render can pick a patched runtime.
- A production service refuses to start with `LLM_PROVIDER=mock` (the audit trail
  would be wrong); local `.env` keeps `mock`.
- PDF snapshots live in PostgreSQL (`report_artifacts`), so watch database disk
  usage and raise the plan when it grows.
5. Confirm the pre-deploy migration, API health, and worker startup.
6. Run `python scripts/smoke_test.py --base-url https://YOUR_SERVICE.onrender.com --token YOUR_TOKEN` from a machine with Python.

The Web Service runs migrations once with `preDeployCommand`. Neither web nor worker runs migrations at startup.

## API

- `POST /scans` — `{"title": "..."}`
- `POST /scans/{id}/sources` — `{"url": "https://example.com", "max_pages": 5}`; fetches the site and stores its pages as documents (`{"name": "..."}` optionally overrides the source name)
- `POST /scans/{id}/run`
- `POST /scans/{id}/retry`
- `GET /scans/{id}/jobs`
- `POST /scans/{id}/reanalysis`
- `GET /scans/{id}/profile`
- `GET /scans/{id}/metrics`
- `GET /scans/{id}/metrics/{metricId}`
- `GET /scans/{id}/annotations`
- `GET /scans/{id}/report?type=free|full` — HTML report
- `GET /scans/{id}/report.pdf?type=free|full` — PDF report

Use `Authorization: Bearer TOKEN`. `/health/live` and `/health/ready` are public.

See `implementation-plan.md` for methodology, state machines, formulas, and operational requirements.
