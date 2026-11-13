"""Initial StoryAudit runtime schema.

Revision ID: 0001_initial
Revises:
"""
from alembic import op

from app.db import Base
from app import models  # noqa: F401

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    Base.metadata.create_all(bind=op.get_bind())
    op.execute("CREATE UNIQUE INDEX one_current_profile ON source_profiles(scan_id, source_id) WHERE is_current")
    op.execute("CREATE UNIQUE INDEX one_current_metric ON metrics(scan_id, code) WHERE is_current")
    op.execute("CREATE UNIQUE INDEX one_current_capital ON capitals(scan_id, code) WHERE is_current")
    op.execute("CREATE UNIQUE INDEX one_current_index ON indices(scan_id, code) WHERE is_current")
    op.execute("CREATE UNIQUE INDEX one_calculation_generation ON calculation_runs(scan_id, analysis_generation)")
    op.execute("""
        CREATE FUNCTION reject_corpus_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'Immutable corpus does not allow %', TG_OP;
        END;
        $$ LANGUAGE plpgsql
    """)
    for table in ("sources", "documents", "blocks", "propositions"):
        op.execute(f"CREATE TRIGGER immutable_{table} BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_corpus_mutation()")
    op.execute("""
        CREATE FUNCTION validate_annotation() RETURNS trigger AS $$
        DECLARE object_scan uuid;
        BEGIN
            IF NEW.supersedes_annotation_id = NEW.id THEN
                RAISE EXCEPTION 'Annotation cannot supersede itself';
            END IF;
            IF NEW.object_type = 'scan' THEN
                SELECT id INTO object_scan FROM scans WHERE id = NEW.object_id;
            ELSIF NEW.object_type = 'source' THEN
                SELECT scan_id INTO object_scan FROM sources WHERE id = NEW.object_id;
            ELSIF NEW.object_type = 'document' THEN
                SELECT s.scan_id INTO object_scan FROM documents d JOIN sources s ON s.id=d.source_id WHERE d.id=NEW.object_id;
            ELSIF NEW.object_type = 'proposition' THEN
                SELECT s.scan_id INTO object_scan FROM propositions p JOIN blocks b ON b.id=p.block_id JOIN documents d ON d.id=b.document_id JOIN sources s ON s.id=d.source_id WHERE p.id=NEW.object_id;
            ELSE
                RAISE EXCEPTION 'Unknown annotation object type: %', NEW.object_type;
            END IF;
            IF object_scan IS NULL OR object_scan <> NEW.scan_id THEN
                RAISE EXCEPTION 'Annotation object does not belong to scan';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("CREATE TRIGGER validate_annotation_object BEFORE INSERT OR UPDATE ON annotations FOR EACH ROW EXECUTE FUNCTION validate_annotation()")
    op.execute("""
        CREATE FUNCTION claim_next_job(worker_id text) RETURNS SETOF jobs AS $$
        WITH candidate AS (
            SELECT j.id
            FROM jobs j
            WHERE j.status = 'pending'
              AND j.available_at <= now()
              AND NOT EXISTS (
                  SELECT 1 FROM job_dependencies d
                  JOIN jobs parent ON parent.id = d.depends_on_job_id
                  WHERE d.job_id = j.id AND parent.status <> 'succeeded'
              )
            ORDER BY j.available_at, j.created_at, j.id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        ), claimed AS (
            UPDATE jobs j
            SET status = 'running',
                claimed_by = worker_id,
                claimed_at = now(),
                started_at = COALESCE(j.started_at, now()),
                claim_token = gen_random_uuid(),
                heartbeat_at = now(),
                lease_expires_at = now() + make_interval(secs => COALESCE(NULLIF(current_setting('app.job_lease_seconds', true), '')::integer, 300)),
                attempt_count = j.attempt_count + 1,
                updated_at = now()
            FROM candidate
            WHERE j.id = candidate.id
            RETURNING j.*
        )
        SELECT * FROM claimed;
        $$ LANGUAGE sql
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS claim_next_job(text)")
    Base.metadata.drop_all(bind=op.get_bind())
    op.execute("DROP FUNCTION IF EXISTS validate_annotation()")
    op.execute("DROP FUNCTION IF EXISTS reject_corpus_mutation()")
