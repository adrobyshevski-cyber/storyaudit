"""Record the cost reported by the gateway instead of estimating it.

Revision ID: 0009_llm_provider_cost
Revises: 0008_validated_mode
"""

from alembic import op
import sqlalchemy as sa

revision = "0009_llm_provider_cost"
down_revision = "0008_validated_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_usage", sa.Column("provider_cost_usd", sa.Numeric(14, 8), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_usage", "provider_cost_usd")
