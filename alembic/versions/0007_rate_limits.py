"""Store rate-limit counters in PostgreSQL.

Revision ID: 0007_rate_limits
Revises: 0006_llm_usage
"""

from alembic import op

from app.db import Base
from app import models  # noqa: F401

revision = "0007_rate_limits"
down_revision = "0006_llm_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["rate_limit_counters"].create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.tables["rate_limit_counters"].drop(bind=op.get_bind(), checkfirst=True)
