"""Record the cost reported by the gateway instead of estimating it.

Revision ID: 0009_llm_provider_cost
Revises: 0008_validated_mode

Note: 0006 creates ``llm_usage`` from ``Base.metadata``, i.e. from the *current*
model. On a fresh database the table therefore already contains
``provider_cost_usd``, so this migration must not add it twice.
"""

from alembic import op
import sqlalchemy as sa

revision = "0009_llm_provider_cost"
down_revision = "0008_validated_mode"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(item["name"] == column for item in inspector.get_columns(table))


def upgrade() -> None:
    if not _column_exists("llm_usage", "provider_cost_usd"):
        op.add_column("llm_usage", sa.Column("provider_cost_usd", sa.Numeric(14, 8), nullable=True))


def downgrade() -> None:
    if _column_exists("llm_usage", "provider_cost_usd"):
        op.drop_column("llm_usage", "provider_cost_usd")
