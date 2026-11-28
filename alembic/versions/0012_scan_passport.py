"""Passport of the report: derived category and detection metadata.

Revision ID: 0012_scan_passport
Revises: 0011_llm_cache

``scans.geography`` already existed but was never filled; the category name had
nowhere to live at all (only its status). Both are derived from the corpus in
``app.analysis.passport`` and confirmed by the analyst.
"""

from alembic import op
import sqlalchemy as sa

revision = "0012_scan_passport"
down_revision = "0011_llm_cache"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(item["name"] == column for item in inspector.get_columns(table))


def upgrade() -> None:
    if not _column_exists("scans", "category"):
        op.add_column("scans", sa.Column("category", sa.String(200), nullable=True))
    if not _column_exists("scans", "passport"):
        op.add_column(
            "scans",
            sa.Column("passport", sa.JSON(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        )


def downgrade() -> None:
    if _column_exists("scans", "passport"):
        op.drop_column("scans", "passport")
    if _column_exists("scans", "category"):
        op.drop_column("scans", "category")
