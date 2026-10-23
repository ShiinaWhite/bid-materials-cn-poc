"""Rebuildable project document text. Never a Material or a verified fact."""
from .common import now
from .schema_v05 import upgrade as upgrade_v05

VERSION = 'jty-mcp-0.6.0'


def upgrade(con):
    version = con.execute("SELECT meta_value FROM schema_meta WHERE meta_key='schema_version'").fetchone()[0]
    if version == VERSION:
        return False
    if version != 'jty-mcp-0.5.0':
        upgrade_v05(con)
    statements = [
        """CREATE TABLE project_document(
            document_id TEXT PRIMARY KEY NOT NULL,
            project_file_id TEXT NOT NULL UNIQUE REFERENCES project_file(project_file_id),
            source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
            extractor TEXT NOT NULL DEFAULT 'project-document-v1',
            status TEXT NOT NULL CHECK(status IN ('pending','queued','processing','complete','partial','failed','unsupported')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            current_run_id TEXT, text_length INTEGER NOT NULL DEFAULT 0 CHECK(text_length>=0),
            unit_count INTEGER NOT NULL DEFAULT 0 CHECK(unit_count>=0), error TEXT,
            index_status TEXT NOT NULL DEFAULT 'pending' CHECK(index_status IN ('pending','ready','failed'))
        ) STRICT""",
        """CREATE TABLE project_document_run(
            run_id TEXT PRIMARY KEY NOT NULL,
            document_id TEXT NOT NULL REFERENCES project_document(document_id),
            status TEXT NOT NULL CHECK(status IN ('queued','processing','complete','partial','failed','unsupported')),
            requested_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            processed_units INTEGER NOT NULL DEFAULT 0 CHECK(processed_units>=0),
            total_units INTEGER CHECK(total_units>=processed_units), progress_kind TEXT,
            error TEXT
        ) STRICT""",
        """CREATE TABLE project_document_unit(
            unit_id TEXT PRIMARY KEY NOT NULL,
            document_id TEXT NOT NULL REFERENCES project_document(document_id),
            run_id TEXT NOT NULL REFERENCES project_document_run(run_id),
            unit_index INTEGER NOT NULL CHECK(unit_index>=1),
            unit_type TEXT NOT NULL CHECK(unit_type IN ('page','paragraph_group','sheet_chunk')),
            page_no INTEGER CHECK(page_no>=1), section_path TEXT, sheet_name TEXT, cell_range TEXT,
            row_start INTEGER CHECK(row_start>=1), row_end INTEGER CHECK(row_end>=row_start),
            page_status TEXT NOT NULL CHECK(page_status IN ('native','ocr','empty','failed')),
            text_raw TEXT NOT NULL, text_norm TEXT NOT NULL, error TEXT,
            projection_path TEXT NOT NULL UNIQUE, projection_sha256 TEXT NOT NULL,
            UNIQUE(document_id,unit_index),
            CHECK((unit_type='page' AND page_no IS NOT NULL) OR
                  (unit_type='paragraph_group' AND section_path IS NOT NULL) OR
                  (unit_type='sheet_chunk' AND sheet_name IS NOT NULL AND row_start IS NOT NULL AND row_end IS NOT NULL))
        ) STRICT""",
        'CREATE INDEX ix_project_document_run ON project_document_run(document_id,created_at)',
        'CREATE INDEX ix_project_document_unit ON project_document_unit(document_id,unit_index)',
        """CREATE TRIGGER project_document_source BEFORE INSERT ON project_document
            WHEN NEW.source_sha256 IS NOT (SELECT sha256 FROM project_file WHERE project_file_id=NEW.project_file_id)
            BEGIN SELECT RAISE(ABORT,'document source mismatch'); END""",
        """CREATE TRIGGER project_document_identity BEFORE UPDATE ON project_document
            WHEN NEW.document_id IS NOT OLD.document_id OR NEW.project_file_id IS NOT OLD.project_file_id
                 OR NEW.source_sha256 IS NOT OLD.source_sha256 OR NEW.created_at IS NOT OLD.created_at
            BEGIN SELECT RAISE(ABORT,'document source identity is immutable'); END""",
        """CREATE TRIGGER project_document_unit_binding BEFORE INSERT ON project_document_unit
            WHEN NEW.document_id IS NOT (SELECT document_id FROM project_document_run WHERE run_id=NEW.run_id)
            BEGIN SELECT RAISE(ABORT,'unit run mismatch'); END""",
    ]
    for sql in statements:
        con.execute(sql)
    for table in ('project_document', 'project_document_run', 'project_document_unit'):
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            con.execute(f"""CREATE TRIGGER controlled_{table}_{operation} BEFORE {operation} ON {table}
                WHEN document_writer() IS NOT 1
                BEGIN SELECT RAISE(ABORT,'manager document write required'); END""")
    # Existing originals gain only a metadata projection; no extraction on migration.
    con.create_function('document_writer', 0, lambda: 1)
    con.execute("""INSERT INTO project_document(document_id,project_file_id,source_sha256,status,created_at,updated_at)
        SELECT lower(hex(randomblob(16))),project_file_id,sha256,'pending',?,? FROM project_file""", (now(), now()))
    con.execute("UPDATE schema_meta SET meta_value=?,updated_at=? WHERE meta_key='schema_version'", (VERSION, now()))
    return True
