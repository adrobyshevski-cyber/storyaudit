"""Wait until the database accepts connections.

CI runners start the application before PostgreSQL is ready; ``alembic upgrade``
would fail on the first attempt. Uses DATABASE_URL (or the first argument).
"""

from __future__ import annotations

import pathlib
import sys
import time

from sqlalchemy import create_engine, text

# Allow `python scripts/wait_for_db.py` from the repository root in CI images.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import get_settings, normalize_database_url  # noqa: E402

TIMEOUT_SECONDS = 90


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else get_settings().database_url
    engine = create_engine(normalize_database_url(url), pool_pre_ping=True)
    deadline = time.monotonic() + TIMEOUT_SECONDS
    attempt = 0
    while True:
        attempt += 1
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            print(f"database is ready after {attempt} attempt(s)")
            return 0
        except Exception as exc:  # noqa: BLE001 - any connection error means "not ready yet"
            if time.monotonic() > deadline:
                print(f"database is not ready after {TIMEOUT_SECONDS}s: {type(exc).__name__}")
                return 1
            time.sleep(min(5.0, 0.5 * attempt))


if __name__ == "__main__":
    raise SystemExit(main())
