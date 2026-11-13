"""Store validated narrative paragraphs per report section.

Revision ID: 0004_report_narratives
Revises: 0003_annotation_product
"""

from alembic import op

from app.db import Base
from app import models  # noqa: F401

revision = "0004_report_narratives"
down_revision = "0003_annotation_product"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["report_narratives"].create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.tables["report_narratives"].drop(bind=op.get_bind(), checkfirst=True)
