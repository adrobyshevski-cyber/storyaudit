from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

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
    llm_schema_repair_attempts: int = 2
    llm_transport_retries: int = 2
    llm_request_timeout_seconds: float = 90.0
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
    landscape_max_pages: int = 3
    landscape_fetch_delay_seconds: float = 0.5
    report_pdf_timeout_seconds: int = 60
    report_live_check: bool = True
    report_live_check_timeout_seconds: float = 3.0
    report_max_statements: int = 25
    report_max_products: int = 10
    annotation_batch_size: int = 20

    @property
    def fast_agents(self) -> set[str]:
        return {name.strip() for name in self.llm_fast_agents.split(",") if name.strip()}

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

    def validate_worker(self) -> None:
        if self.app_env == "production" and self.llm_provider == "mock":
            raise RuntimeError("LLM_PROVIDER=mock is forbidden in production")
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
