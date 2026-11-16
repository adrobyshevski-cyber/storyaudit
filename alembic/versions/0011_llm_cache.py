"""Cache provider answers across scans and generations.

Revision ID: 0011_llm_cache
Revises: 0010_public_requests
"""

from alembic import op

from app.db import Base
from app import models  # noqa: F401

revision = "0011_llm_cache"
down_revision = "0010_public_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["llm_cache"].create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.tables["llm_cache"].drop(bind=op.get_bind(), checkfirst=True)
