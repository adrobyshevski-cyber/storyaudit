import os
import subprocess
import sys

# Live HTTP checks are disabled in tests; they are covered by a dedicated unit test.
os.environ.setdefault("REPORT_LIVE_CHECK", "false")

TEST_URL = os.environ.get("TEST_DATABASE_URL")
if TEST_URL:
    os.environ["DATABASE_URL"] = TEST_URL


def pytest_configure(config):
    if not TEST_URL:
        return
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
