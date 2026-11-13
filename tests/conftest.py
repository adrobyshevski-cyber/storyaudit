import os
import subprocess
import sys

# Live HTTP checks are disabled in tests; they are covered by a dedicated unit test.
os.environ.setdefault("REPORT_LIVE_CHECK", "false")
# Ignore the developer's .env: it may carry a real provider key, prices or
# feature flags that would make the suite slow, costly or order-dependent.
os.environ.setdefault("ENV_FILE", ".env.tests-ignore")
# Never spend real provider credits in the test suite. A local .env may carry a
# real LLM_PROVIDER; tests always run on mock unless the caller exports a provider
# explicitly (LLM_PROVIDER=openai_compatible pytest ...).
os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("LLM_MODEL", "mock-v1")

TEST_URL = os.environ.get("TEST_DATABASE_URL")
if TEST_URL:
    os.environ["DATABASE_URL"] = TEST_URL


def pytest_configure(config):
    if not TEST_URL:
        return
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    _reset_rate_limit_counters()


def _reset_rate_limit_counters():
    """Rate-limit windows are wall-clock based, so counters would accumulate
    between local runs and eventually trip the daily scan limit."""
    try:
        from sqlalchemy import text

        from app.db import get_session_factory

        with get_session_factory()() as session:
            session.execute(text("DELETE FROM rate_limit_counters"))
            session.commit()
    except Exception:  # noqa: BLE001 - the table may not exist yet on a broken run
        pass
