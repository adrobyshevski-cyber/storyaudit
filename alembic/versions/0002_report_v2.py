"""report_v2: evidence-first registers and report snapshots.

Revision ID: 0002_report_v2
Revises: 0001_initial

The migration is deliberately tolerant: `0001_initial` uses
`Base.metadata.create_all`, so on a brand-new database the new tables and
columns already exist by the time this revision runs. On databases created
before report_v2 they are missing. Both cases must converge.
"""

from alembic import op

from app.db import Base
from app import models  # noqa: F401  (registers tables are registered by this import)

revision = "0002_report_v2"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

SCAN_COLUMNS = (
    ("brand", "text"),
    ("audit_mode", "text NOT NULL DEFAULT 'desk'"),
    ("snapshot_date", "date"),
    ("geography", "text"),
    ("category_status", "text NOT NULL DEFAULT 'unknown'"),
    ("missing_materials", "jsonb NOT NULL DEFAULT '[]'::jsonb"),
    ("client_brief", "jsonb NOT NULL DEFAULT '{}'::jsonb"),
    ("audit_phase", "text NOT NULL DEFAULT 'draft'"),
    ("internal_metrics", "boolean NOT NULL DEFAULT false"),
)

NEW_TABLES = (
    "code_sequences",
    "source_registry",
    "products",
    "landscape_entries",
    "statements",
    "evidence_records",
    "layer_claims",
    "actant_maps",
    "actant_roles",
    "journey_steps",
    "consistency_cells",
    "findings_v2",
    "language_codes",
    "scorecard_entries",
    "recommendations_v2",
    "recommendation_findings",
    "kpis",
    "roadmap_phases",
    "stage_gate",
    "audit_hypotheses",
    "open_questions",
    "trace_links",
    "review_events",
    "report_snapshots",
)


def upgrade() -> None:
    bind = op.get_bind()
    for name, ddl_type in SCAN_COLUMNS:
        op.execute(f"ALTER TABLE scans ADD COLUMN IF NOT EXISTS {name} {ddl_type}")
    op.execute("ALTER TABLE scans ALTER COLUMN methodology_version SET DEFAULT 'socode_v1.1'")
    op.execute("UPDATE scans SET brand = title WHERE brand IS NULL")

    for table in NEW_TABLES:
        Base.metadata.tables[table].create(bind=bind, checkfirst=True)

    op.execute("""
        CREATE OR REPLACE FUNCTION validate_finding() RETURNS trigger AS $$
        BEGIN
            IF NEW.kind = 'gap' AND jsonb_array_length(NEW.basis_codes) < 2 THEN
                RAISE EXCEPTION 'Gap finding requires at least two observable sides';
            END IF;
            IF NEW.kind = 'weak_evidence' AND jsonb_array_length(NEW.basis_codes) = 0 THEN
                RAISE EXCEPTION 'Weak evidence finding requires a claim reference';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'validate_finding_trigger') THEN
                CREATE TRIGGER validate_finding_trigger
                BEFORE INSERT OR UPDATE ON findings_v2
                FOR EACH ROW EXECUTE FUNCTION validate_finding();
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS validate_finding_trigger ON findings_v2")
    op.execute("DROP FUNCTION IF EXISTS validate_finding()")
    bind = op.get_bind()
    for table in reversed(NEW_TABLES):
        Base.metadata.tables[table].drop(bind=bind, checkfirst=True)
    for name, _ in reversed(SCAN_COLUMNS):
        op.execute(f"ALTER TABLE scans DROP COLUMN IF EXISTS {name}")
    op.execute("ALTER TABLE scans ALTER COLUMN methodology_version SET DEFAULT 'methodology_v1'")
