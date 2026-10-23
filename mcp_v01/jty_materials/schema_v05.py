"""Project containers, private evidence and references; no material conversion."""
from .common import now
from .schema_v04 import upgrade as upgrade_v04

VERSION = 'jty-mcp-0.5.0'
CATEGORIES = ('tender_docs', 'bid_outputs', 'project_evidence', 'process', 'result', 'other')


def upgrade(con):
    version = con.execute("SELECT meta_value FROM schema_meta WHERE meta_key='schema_version'").fetchone()[0]
    if version == VERSION:
        return False
    if version != 'jty-mcp-0.4.0':
        upgrade_v04(con)
    # Preserve ledger values/IDs and every existing fact trigger. ANY retains
    # legacy INTEGER IDs and permits stable UUID project-file identities.
    con.execute("""CREATE TABLE audit_log_v05(
        log_id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT,
        action TEXT NOT NULL, entity_type TEXT NOT NULL CHECK(entity_type IN
        ('material','file','fact','extraction','org','person','tag','service','system',
         'project','project_file','project_material')),
        entity_id ANY, detail_json TEXT, note TEXT) STRICT""")
    con.execute('INSERT INTO audit_log_v05 SELECT * FROM audit_log')
    con.execute('DROP TABLE audit_log')
    con.execute('PRAGMA legacy_alter_table=ON')
    try:
        con.execute('ALTER TABLE audit_log_v05 RENAME TO audit_log')
    finally:
        con.execute('PRAGMA legacy_alter_table=OFF')
    con.execute('CREATE INDEX ix_audit_at ON audit_log(at)')
    con.execute('CREATE INDEX ix_audit_entity ON audit_log(entity_type,entity_id,at)')
    for op in ('UPDATE', 'DELETE'):
        con.execute(f"CREATE TRIGGER protect_audit_log_{op} BEFORE {op} ON audit_log BEGIN SELECT RAISE(ABORT,'append-only ledger'); END")
    con.execute("""CREATE TRIGGER protect_audit_log_replace BEFORE INSERT ON audit_log
        WHEN EXISTS(SELECT 1 FROM audit_log WHERE log_id=NEW.log_id)
        BEGIN SELECT RAISE(ABORT,'ledger replacement forbidden'); END""")
    statements = [
        """CREATE TABLE project(
            project_id INTEGER PRIMARY KEY CHECK(project_id>0), project_code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 500),
            tender_no TEXT, tenderer TEXT, lot_name TEXT,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            CHECK(project_code=printf('PRJ-%06d',project_id))) STRICT""",
        """CREATE TABLE project_file(
            project_file_id TEXT PRIMARY KEY NOT NULL CHECK(length(project_file_id)=32
                AND project_file_id NOT GLOB '*[^a-f0-9]*'),
            project_id INTEGER NOT NULL REFERENCES project(project_id),
            category TEXT NOT NULL CHECK(category IN ('tender_docs','bid_outputs','project_evidence','process','result','other')),
            relative_path TEXT NOT NULL UNIQUE,
            path_key TEXT GENERATED ALWAYS AS(lower(relative_path)) VIRTUAL,
            filename TEXT NOT NULL, extension TEXT NOT NULL,
            sha256 TEXT NOT NULL CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^a-f0-9]*'),
            size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 1 AND 209715200),
            imported_at TEXT NOT NULL, imported_by TEXT NOT NULL CHECK(length(trim(imported_by))>0),
            status TEXT NOT NULL DEFAULT 'active' CHECK(status='active'),
            UNIQUE(project_id,sha256), UNIQUE(path_key),
            CHECK(extension IN ('pdf','png','jpg','jpeg','doc','docx','xls','xlsx','ppt','pptx','txt','md','zip'))
        ) STRICT""",
        """CREATE TABLE project_material(
            reference_id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES project(project_id),
            material_id INTEGER NOT NULL REFERENCES material(material_id),
            status TEXT NOT NULL CHECK(status IN ('active','removed')),
            added_at TEXT NOT NULL, added_by TEXT NOT NULL,
            note TEXT, UNIQUE(project_id,material_id)) STRICT""",
        'CREATE INDEX ix_project_file_project ON project_file(project_id)',
        'CREATE INDEX ix_project_material_reverse ON project_material(material_id,status)',
        """CREATE TRIGGER project_identity BEFORE UPDATE ON project
            WHEN NEW.project_id IS NOT OLD.project_id OR NEW.project_code IS NOT OLD.project_code
                OR NEW.created_at IS NOT OLD.created_at
            BEGIN SELECT RAISE(ABORT,'project identity is immutable'); END""",
        """CREATE TRIGGER project_file_path BEFORE INSERT ON project_file
            WHEN NEW.relative_path IS NOT ('projects/' ||
                (SELECT project_code FROM project WHERE project_id=NEW.project_id) ||
                '/files/' || NEW.project_file_id || '.' || NEW.extension)
            BEGIN SELECT RAISE(ABORT,'invalid project evidence path'); END""",
        """CREATE TRIGGER project_file_immutable BEFORE UPDATE ON project_file
            BEGIN SELECT RAISE(ABORT,'project evidence is immutable'); END""",
        """CREATE TRIGGER project_material_identity BEFORE UPDATE ON project_material
            WHEN NEW.reference_id IS NOT OLD.reference_id OR NEW.project_id IS NOT OLD.project_id
                OR NEW.material_id IS NOT OLD.material_id
            BEGIN SELECT RAISE(ABORT,'reference identity is immutable'); END""",
    ]
    for sql in statements:
        con.execute(sql)
    for table, identity in [('project', 'project_id=NEW.project_id OR project_code=NEW.project_code'),
                            ('project_file', 'project_file_id=NEW.project_file_id OR path_key=lower(NEW.relative_path)'),
                            ('project_material', 'reference_id=NEW.reference_id OR (project_id=NEW.project_id AND material_id=NEW.material_id)')]:
        con.execute(f"""CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table}
            WHEN EXISTS(SELECT 1 FROM {table} WHERE {identity})
            BEGIN SELECT RAISE(ABORT,'project identity replacement forbidden'); END""")
        con.execute(f"""CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(ABORT,'retain project history'); END""")
        for op in ('INSERT', 'UPDATE'):
            con.execute(f"""CREATE TRIGGER {table}_controlled_{op} BEFORE {op} ON {table}
                WHEN project_actor() IS NULL OR project_action() IS NULL
                BEGIN SELECT RAISE(ABORT,'controlled project write required'); END""")
    for table in ('project_file', 'project_material'):
        for op in ('INSERT', 'UPDATE'):
            con.execute(f"""CREATE TRIGGER {table}_active_{op} BEFORE {op} ON {table}
                WHEN (SELECT status FROM project WHERE project_id=NEW.project_id) IS NOT 'active'
                BEGIN SELECT RAISE(ABORT,'project is missing or archived'); END""")
    con.execute("UPDATE schema_meta SET meta_value=?,updated_at=? WHERE meta_key='schema_version'", (VERSION, now()))
    return True
