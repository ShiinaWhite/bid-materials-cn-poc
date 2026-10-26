"""v0.8 Project Document Intelligence: embedded images and material candidates.

- project_document_unit gains unit_type 'embedded_image' (table rebuild: the
  STRICT CHECK is closed) and a nullable image_id binding.
- project_document gains summary_json: the structured extraction summary the
  WebUI renders instead of a bare status word.
- project_document_image: the embedded-image registry (anchor + OCR state).
  One row per word/media occurrence in document order; OCR text lives here
  with a cache key so retries reuse identical work and only failures rerun.
- project_material_candidate: what discovery found. A candidate is never a
  Material: it carries evidence, subject hints and a deterministic-match
  verdict, and keeps the full provenance chain back to the source file SHA.

All four are manager-written derivatives (document_writer guards), rebuildable
from the immutable originals, and never touch the guarded Material fact layer.
"""
from .common import now
from .schema_v07 import upgrade as upgrade_v07

VERSION = 'jty-mcp-0.8.0'

_UNIT_COLUMNS = """document_id TEXT NOT NULL REFERENCES project_document(document_id),
    run_id TEXT NOT NULL REFERENCES project_document_run(run_id),
    unit_index INTEGER NOT NULL CHECK(unit_index>=1),
    unit_type TEXT NOT NULL CHECK(unit_type IN ('page','paragraph_group','sheet_chunk','embedded_image')),
    page_no INTEGER CHECK(page_no>=1), section_path TEXT, sheet_name TEXT, cell_range TEXT,
    row_start INTEGER CHECK(row_start>=1), row_end INTEGER CHECK(row_end>=row_start),
    page_status TEXT NOT NULL CHECK(page_status IN ('native','ocr','empty','failed')),
    text_raw TEXT NOT NULL, text_norm TEXT NOT NULL, error TEXT,
    image_id TEXT REFERENCES project_document_image(image_id),
    projection_path TEXT NOT NULL UNIQUE, projection_sha256 TEXT NOT NULL,
    UNIQUE(document_id,unit_index),
    CHECK((unit_type='page' AND page_no IS NOT NULL) OR
          (unit_type='paragraph_group' AND section_path IS NOT NULL AND image_id IS NULL) OR
          (unit_type='sheet_chunk' AND sheet_name IS NOT NULL AND row_start IS NOT NULL AND row_end IS NOT NULL) OR
          (unit_type='embedded_image' AND image_id IS NOT NULL AND section_path IS NOT NULL))"""

_DOCUMENTED_TABLES = ('project_document', 'project_document_run', 'project_document_unit',
                      'project_document_image', 'project_material_candidate')


def _guarded_triggers(con):
    for table in _DOCUMENTED_TABLES:
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            con.execute(f"""CREATE TRIGGER controlled_{table}_{operation} BEFORE {operation} ON {table}
                WHEN document_writer() IS NOT 1
                BEGIN SELECT RAISE(ABORT,'manager document write required'); END""")


def _rebuild_project_document_unit(con):
    """Extend the closed unit_type vocabulary and bind image units. The old
    rows never carry image_id, so the copy is a plain column projection."""
    con.execute('DROP INDEX IF EXISTS ix_project_document_unit')
    con.execute('DROP TRIGGER IF EXISTS project_document_unit_binding')
    columns = [r[1] for r in con.execute('PRAGMA table_info(project_document_unit)').fetchall()]
    con.execute(f"""CREATE TABLE project_document_unit_v08(
        unit_id TEXT PRIMARY KEY NOT NULL, {_UNIT_COLUMNS}) STRICT""")
    con.execute('INSERT INTO project_document_unit_v08(' + ','.join(columns) + ')'
                ' SELECT ' + ','.join(columns) + ' FROM project_document_unit')
    con.execute('DROP TABLE project_document_unit')
    con.execute('ALTER TABLE project_document_unit_v08 RENAME TO project_document_unit')
    con.execute('CREATE INDEX ix_project_document_unit ON project_document_unit(document_id,unit_index)')
    con.execute("""CREATE TRIGGER project_document_unit_binding BEFORE INSERT ON project_document_unit
        WHEN NEW.document_id IS NOT (SELECT document_id FROM project_document_run WHERE run_id=NEW.run_id)
        BEGIN SELECT RAISE(ABORT,'unit run mismatch'); END""")


def upgrade(con):
    version = con.execute("SELECT meta_value FROM schema_meta WHERE meta_key='schema_version'").fetchone()[0]
    if version == VERSION:
        return False
    if version != 'jty-mcp-0.7.0':
        upgrade_v07(con)
    con.execute('ALTER TABLE project_document ADD COLUMN summary_json TEXT')
    # The guarded tables below reject writes without the writer context; the
    # migration connection supplies it explicitly (same rule as schema_v06).
    con.create_function('document_writer', 0, lambda: 1)
    # The controlled_* family is regenerated once below for all five tables.
    for row in con.execute("SELECT name FROM sqlite_schema WHERE type='trigger' AND name LIKE 'controlled_%'").fetchall():
        con.execute('DROP TRIGGER "' + row[0] + '"')
    # Images first: the rebuilt unit table carries a foreign key to them.
    con.execute("""CREATE TABLE project_document_image(
        image_id TEXT PRIMARY KEY NOT NULL,
        document_id TEXT NOT NULL REFERENCES project_document(document_id),
        run_id TEXT NOT NULL REFERENCES project_document_run(run_id),
        document_order INTEGER NOT NULL CHECK(document_order>=1),
        media_path TEXT NOT NULL, relationship_id TEXT,
        section_path TEXT, nearest_heading TEXT,
        preceding_text TEXT, following_text TEXT,
        width_px INTEGER CHECK(width_px IS NULL OR width_px>0),
        height_px INTEGER CHECK(height_px IS NULL OR height_px>0),
        mime TEXT NOT NULL, byte_size INTEGER NOT NULL CHECK(byte_size>0),
        sha256 TEXT NOT NULL CHECK(length(sha256)=64),
        ocr_status TEXT NOT NULL CHECK(ocr_status IN ('pending','done','failed','skipped')),
        ocr_text_raw TEXT, ocr_text_norm TEXT, ocr_error TEXT,
        ocr_elapsed_ms INTEGER CHECK(ocr_elapsed_ms IS NULL OR ocr_elapsed_ms>=0),
        ocr_cache_key TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(document_id,document_order)
    ) STRICT""")
    con.execute('CREATE INDEX ix_project_document_image ON project_document_image(document_id)')
    _rebuild_project_document_unit(con)
    con.execute("""CREATE TABLE project_material_candidate(
        candidate_id TEXT PRIMARY KEY NOT NULL,
        project_id INTEGER NOT NULL REFERENCES project(project_id),
        document_id TEXT NOT NULL REFERENCES project_document(document_id),
        source_kind TEXT NOT NULL CHECK(source_kind IN ('embedded_image','text_unit')),
        source_unit_id TEXT REFERENCES project_document_unit(unit_id),
        source_image_id TEXT REFERENCES project_document_image(image_id),
        candidate_type TEXT NOT NULL, basis TEXT NOT NULL,
        confidence TEXT NOT NULL CHECK(confidence IN ('high','medium','low')),
        subject_name TEXT, subject_uscc TEXT, subject_person TEXT, subject_id_masked TEXT,
        identifier TEXT, evidence_text TEXT NOT NULL,
        source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
        lifecycle TEXT NOT NULL CHECK(lifecycle IN ('reusable','project_only','transient','unknown')),
        match_status TEXT NOT NULL CHECK(match_status IN ('new_candidate','possible_match','exact_match','not_material')),
        matched_material_id INTEGER REFERENCES material(material_id),
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK((source_kind='embedded_image' AND source_image_id IS NOT NULL) OR
              (source_kind='text_unit' AND source_unit_id IS NOT NULL))
    ) STRICT""")
    con.execute('CREATE INDEX ix_project_candidate_project ON project_material_candidate(project_id,status)')
    con.execute('CREATE INDEX ix_project_candidate_document ON project_material_candidate(document_id)')
    _guarded_triggers(con)
    con.execute("UPDATE schema_meta SET meta_value=?,updated_at=? WHERE meta_key='schema_version'", (VERSION, now()))
    return True
