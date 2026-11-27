import os
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


class Settings(BaseSettings):
    # ENV_FILE lets the test suite ignore a developer's local .env entirely.
    model_config = SettingsConfigDict(env_file=os.environ.get("ENV_FILE", ".env"), extra="ignore")

    app_env: str = "development"
    database_url: str = "postgresql+psycopg://storyaudit:storyaudit@localhost:5432/storyaudit"
    app_access_token: str = "local-test-token"
    log_level: str = "INFO"
    max_request_body_bytes: int = 2_097_152
    llm_provider: str = "mock"
    llm_base_url: str = ""
    llm_model: str = "mock-v1"
    llm_api_key: str = ""
    # Stage routing: cheap model for the per-proposition fan-out, stronger model for
    # synthesis and the client-facing narrative. Empty values fall back to LLM_MODEL.
    llm_model_fast: str = ""
    llm_model_smart: str = ""
    llm_fast_agents: str = "A1,A2,A3,A4"
    # Analysis mode (see ``run_stage`` in ``app/agents.py``).
    #
    # * ``llm`` — every model stage calls the provider (pre-hybrid behaviour);
    # * ``hybrid`` — a stage takes its deterministic draft unless its agent is
    #   listed in ``ANALYSIS_LLM_AGENTS``, so the provider writes only the
    #   client-facing narrative by default;
    # * ``deterministic`` — no provider calls at all (offline/regression runs).
    #
    # Drafts are always schema-validated and traceability-checked exactly like a
    # provider answer, so the audit trail never depends on the mode; only the
    # wording changes. Reanalysis jobs always call the provider: an analyst who
    # asked for a second opinion must not receive a cached or template answer.
    analysis_mode: Literal["llm", "hybrid", "deterministic"] = "llm"
    analysis_llm_agents: str = "NARRATIVE"
    # Provider-answer cache (app/llm_cache.py). Only used with a real provider:
    # mock answers are free and instant, so caching them buys nothing.
    llm_cache_enabled: bool = True
    llm_schema_repair_attempts: int = 2
    llm_transport_retries: int = 2
    # Gateways throttle by tokens per window and can answer 429 for minutes.
    # The cooldown pauses every subsequent request in this process after the
    # retries on a 429 are exhausted; the min interval spaces requests out.
    llm_cooldown_seconds: float = 10.0
    llm_min_interval_seconds: float = 0.0
    llm_request_timeout_seconds: float = 90.0
    # Some OpenAI-compatible gateways require client headers (e.g. opencode.ai:
    # User-Agent, x-opencode-client, x-opencode-session). "{session_id}" in a value
    # is replaced with a per-process UUID.
    llm_extra_headers: dict[str, str] = {}
    llm_user_agent: str = ""
    llm_price_input_per_mtok: float = 0.0
    llm_price_output_per_mtok: float = 0.0
    worker_id: str = ""
    job_poll_interval_seconds: float = 2.0
    job_lease_seconds: int = 300
    job_heartbeat_seconds: int = 30
    job_max_attempts: int = 3
    website_max_pages: int = 5
    website_fetch_timeout_seconds: float = 15.0
    website_allow_private_hosts: bool = False
    website_fetch_delay_seconds: float = 0.0
    website_respect_robots: bool = True
    landscape_max_competitors: int = 5
    # The public free report is a lead magnet, not a paid landscape study: it
    # accepts at most this many alternatives, one page each, supplied by the
    # requester (we do not guess competitors for someone else's market).
    public_landscape_max_competitors: int = 2
    public_landscape_max_pages: int = 1
    landscape_max_pages: int = 3
    landscape_fetch_delay_seconds: float = 0.5
    report_pdf_timeout_seconds: int = 60
    report_live_check: bool = True
    report_live_check_timeout_seconds: float = 3.0
    # Statement budget of the evidence register. The global number is the real
    # budget; the per-source numbers decide who gets it. Client claims drive the
    # findings and §8, competitor claims mostly feed the §4 landscape, so the
    # client gets the larger share. 8/source (the old hard-coded value) cut the
    # strongest proof on a one-page site: a case study with a $400k result ranked
    # 9th under the old scoring and never reached the register.
    report_max_statements: int = 40
    report_max_statements_per_source: int = 12
    report_max_statements_per_competitor: int = 6
    # How many evidence records the FINDINGS stage sees in its prompt.
    report_evidence_context: int = 14
    report_max_products: int = 10
    annotation_batch_size: int = 20
    # Retention (see app/retention.py). Off by default: pruning is an operator
    # decision, not something a dev run should do silently.
    retention_enabled: bool = False
    retention_interval_seconds: int = 86_400
    retention_report_artifacts_days: int = 90
    retention_report_artifacts_keep: int = 3
    retention_llm_usage_days: int = 180
    # 0 disables annotation pruning entirely.
    retention_annotations_days: int = 90
    # 0 disables provider-cache pruning.
    retention_llm_cache_days: int = 30
    # Rate limiting (app/ratelimit.py). One access token == one budget; counters
    # live in PostgreSQL so several API instances share them.
    rate_limit_enabled: bool = True
    rate_limit_scans_per_day: int = 100
    rate_limit_sources_per_hour: int = 12
    rate_limit_runs_per_hour: int = 10
    rate_limit_reanalysis_per_hour: int = 20
    rate_limit_report_builds_per_hour: int = 10
    rate_limit_report_pdfs_per_hour: int = 30
    # Public self-service audit requests (the free NIA landing page). The
    # per-IP limit protects the queue and the LLM budget; the daily total is the
    # hard ceiling for a single worker. One scan costs roughly 15 minutes.
    public_base_url: str = ""
    public_requests_per_day: int = 20
    public_requests_per_ip_per_day: int = 3
    public_request_token_ttl_days: int = 30
    public_consent_version: str = "2026-11-14"
    # Only a hash of the client IP is stored; the salt keeps the hash from being
    # reversible with a precomputed table of the IPv4 space.
    public_hash_salt: str = "storyaudit-public"
    # Shared secret for the trusted caller (the marketing site's Cloudflare
    # Worker). A request carrying it still counts against the per-IP limit, but
    # skips the global daily ceiling: the caller enforces its own per-event
    # quota and already filters abuse before the request reaches us.
    public_internal_token: str = ""

    @property
    def fast_agents(self) -> set[str]:
        return {name.strip() for name in self.llm_fast_agents.split(",") if name.strip()}

    @property
    def llm_agents(self) -> set[str]:
        return {name.strip() for name in self.analysis_llm_agents.split(",") if name.strip()}

    def uses_llm(self, agent: str) -> bool:
        """Whether a stage is allowed to call the provider in the current mode."""
        if self.analysis_mode == "llm":
            return True
        if self.analysis_mode == "deterministic":
            return False
        return agent in self.llm_agents

    def model_for_agent(self, agent: str) -> tuple[str, str]:
        """Return ``(model, tier)`` for an agent.

        Volume stages (per-proposition fan-out) run on the cheap model; all other
        stages keep the stronger one, because they shape the client report.
        """
        if self.llm_provider == "mock":
            return self.llm_model or "mock-v1", "mock"
        if agent in self.fast_agents:
            return self.llm_model_fast or self.llm_model, "fast"
        return self.llm_model_smart or self.llm_model, "smart"

    def validate_runtime(self) -> None:
        """Fail fast on settings that would silently degrade a production run.

        A production API that falls back to ``mock`` still creates scans, but every
        scan would record ``model_version=mock-v1`` even though the worker may use
        a real provider — the audit trail would be wrong. Refusing to start is
        cheaper than discovering that in a client report.
        """
        if self.app_env == "production" and self.llm_provider == "mock":
            raise RuntimeError("LLM_PROVIDER=mock is forbidden in production")

    def validate_worker(self) -> None:
        self.validate_runtime()
        if self.llm_provider == "openai_compatible":
            missing = [
                name
                for name, value in (
                    ("LLM_BASE_URL", self.llm_base_url),
                    ("LLM_MODEL", self.llm_model),
                    ("LLM_API_KEY", self.llm_api_key),
                )
                if not value
            ]
            if missing:
                raise RuntimeError(f"Missing worker settings: {', '.join(missing)}")
        if self.llm_provider not in {"mock", "openai_compatible"}:
            raise RuntimeError(f"Unsupported LLM_PROVIDER: {self.llm_provider}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
