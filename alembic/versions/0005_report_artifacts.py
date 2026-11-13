"""Cache rendered PDF artifacts in PostgreSQL by report content hash.

Revision ID: 0005_report_artifacts
Revises: 0004_report_narratives
"""

from alembic import op

from app.db import Base
from app import models  # noqa: F401

revision = "0005_report_artifacts"
down_revision = "0004_report_narratives"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["report_artifacts"].create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.tables["report_artifacts"].drop(bind=op.get_bind(), checkfirst=True)
