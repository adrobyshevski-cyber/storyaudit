"""Allow `product` annotations in the annotation validator.

Revision ID: 0003_annotation_product
Revises: 0002_report_v2

Phase C stores actant maps keyed by a product, so the annotation guard must
recognise the `product` object type. The function is replaced wholesale to keep
the existing checks intact.
"""

from alembic import op

revision = "0003_annotation_product"
down_revision = "0002_report_v2"
branch_labels = None
depends_on = None

FUNCTION = """
CREATE OR REPLACE FUNCTION validate_annotation() RETURNS trigger AS $$
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
        SELECT s.scan_id INTO object_scan FROM documents d
        JOIN sources s ON s.id=d.source_id WHERE d.id=NEW.object_id;
    ELSIF NEW.object_type = 'proposition' THEN
        SELECT s.scan_id INTO object_scan FROM propositions p
        JOIN blocks b ON b.id=p.block_id
        JOIN documents d ON d.id=b.document_id
        JOIN sources s ON s.id=d.source_id WHERE p.id=NEW.object_id;
    ELSIF NEW.object_type = 'product' THEN
        SELECT scan_id INTO object_scan FROM products WHERE id = NEW.object_id;
    ELSIF NEW.object_type = 'finding' THEN
        SELECT scan_id INTO object_scan FROM findings_v2 WHERE id = NEW.object_id;
    ELSE
        RAISE EXCEPTION 'Unknown annotation object type: %', NEW.object_type;
    END IF;
    IF object_scan IS NULL OR object_scan <> NEW.scan_id THEN
        RAISE EXCEPTION 'Annotation object does not belong to scan';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.execute(FUNCTION)


def downgrade() -> None:
    # Restore the pre-phase-C function (scan/source/document/proposition only).
    op.execute("""
    CREATE OR REPLACE FUNCTION validate_annotation() RETURNS trigger AS $$
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
            SELECT s.scan_id INTO object_scan FROM documents d
            JOIN sources s ON s.id=d.source_id WHERE d.id=NEW.object_id;
        ELSIF NEW.object_type = 'proposition' THEN
            SELECT s.scan_id INTO object_scan FROM propositions p
            JOIN blocks b ON b.id=p.block_id
            JOIN documents d ON d.id=b.document_id
            JOIN sources s ON s.id=d.source_id WHERE p.id=NEW.object_id;
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
