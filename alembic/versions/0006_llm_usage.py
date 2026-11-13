"""Account LLM tokens and cost per scan.

Revision ID: 0006_llm_usage
Revises: 0005_report_artifacts
"""

from alembic import op

from app.db import Base
from app import models  # noqa: F401

revision = "0006_llm_usage"
down_revision = "0005_report_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["llm_usage"].create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.tables["llm_usage"].drop(bind=op.get_bind(), checkfirst=True)
