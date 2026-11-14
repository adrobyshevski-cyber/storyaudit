"""Public free-audit requests from the landing page.

Revision ID: 0010_public_requests
Revises: 0009_llm_provider_cost
"""

from alembic import op

from app.db import Base
from app import models  # noqa: F401

revision = "0010_public_requests"
down_revision = "0009_llm_provider_cost"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["audit_requests"].create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.tables["audit_requests"].drop(bind=op.get_bind(), checkfirst=True)
