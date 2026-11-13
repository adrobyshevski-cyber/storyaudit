"""Validated mode: consented interview sessions and redacted segments.

Revision ID: 0008_validated_mode
Revises: 0007_rate_limits
"""

from alembic import op

from app.db import Base
from app import models  # noqa: F401

revision = "0008_validated_mode"
down_revision = "0007_rate_limits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["interview_sessions"].create(bind=op.get_bind(), checkfirst=True)
    Base.metadata.tables["interview_segments"].create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.tables["interview_segments"].drop(bind=op.get_bind(), checkfirst=True)
    Base.metadata.tables["interview_sessions"].drop(bind=op.get_bind(), checkfirst=True)
