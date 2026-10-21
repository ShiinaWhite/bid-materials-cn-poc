"""Existing schema + a deliberately small, hardened certificate/contract slice.

Human writes are offline operations, never MCP tools. SQLite triggers guard
ordinary SQL too. An owner who can replace the DB or drop triggers is outside
this protection boundary; this is not a cryptographic tamper-proof ledger.
"""
import datetime as dt
import json
from pathlib import Path
import sqlite3
from contextlib import closing

from .common import digest, now, safe_path
from .fields import FIELDS, ALLOWED, ORG_FIELDS, GRADE_FIELDS, validate_value, org_norm,field_spec,allowed_field,qualification_name
from .schema_v04 import upgrade, VERSION


SCHEMA_VERSION = VERSION


def release_schema(con):
    con.execute("ALTER TABLE material_file ADD COLUMN version_id TEXT")
    con.execute("CREATE INDEX ix_file_version ON material_file(material_id,version_id)")
    con.execute("""CREATE TABLE search_projection(
        material_id INTEGER PRIMARY KEY REFERENCES material(material_id),
        relative_path TEXT NOT NULL UNIQUE,
        rel_path_key TEXT GENERATED ALWAYS AS(lower(relative_path)) VIRTUAL,
        search_file_id INTEGER NOT NULL REFERENCES material_file(file_id),
        sha256 TEXT NOT NULL
    ) STRICT""")
    con.execute("CREATE UNIQUE INDEX ix_projection_path ON search_projection(rel_path_key)")
    con.execute("UPDATE schema_meta SET meta_value='jty-mcp-0.1.0',updated_at=? WHERE meta_key='schema_version'", (now(),))


def no_replace_guards(con):
    for table in ("material", "certificate_detail", "performance_detail"):
        con.execute(f"CREATE TRIGGER no_replace_{table} BEFORE INSERT ON {table} "
                    f"WHEN EXISTS(SELECT 1 FROM {table} WHERE material_id=NEW.material_id) "
                    "BEGIN SELECT RAISE(ABORT,'identity replacement forbidden'); END")
    con.execute("""CREATE TRIGGER no_replace_file BEFORE INSERT ON material_file
        WHEN EXISTS(SELECT 1 FROM material_file WHERE file_id=NEW.file_id OR rel_path_key=lower(NEW.relative_path))
        BEGIN SELECT RAISE(ABORT,'file replacement forbidden'); END""")


def final_identity_guards(con):
    con.execute("DROP TRIGGER no_replace_material")
    con.execute("""CREATE TRIGGER no_replace_material BEFORE INSERT ON material
        WHEN EXISTS(SELECT 1 FROM material WHERE material_id=NEW.material_id
          OR (NEW.material_code<>'' AND material_code=NEW.material_code))
        BEGIN SELECT RAISE(ABORT,'identity replacement forbidden'); END""")
    for table in ("certificate_detail", "performance_detail"):
        con.execute(f"CREATE TRIGGER stable_{table}_identity BEFORE UPDATE ON {table} "
                    "WHEN NEW.material_id<>OLD.material_id BEGIN SELECT RAISE(ABORT,'detail identity is immutable'); END")
    con.execute("""CREATE TRIGGER immutable_candidate_source BEFORE UPDATE ON extracted_fact
        WHEN NEW.extracted_id<>OLD.extracted_id OR NEW.run_id<>OLD.run_id OR NEW.material_id<>OLD.material_id
          OR NEW.field_name IS NOT OLD.field_name OR NEW.raw_value IS NOT OLD.raw_value
          OR NEW.normalized_value IS NOT OLD.normalized_value OR NEW.evidence_span IS NOT OLD.evidence_span
        BEGIN SELECT RAISE(ABORT,'append a new extraction candidate instead'); END""")


def harden(con):
    con.execute("DROP TRIGGER trg_cert_no_direct_edit")
    for table in ("material", "org"):
        con.execute(f"DROP TRIGGER trg_{table}_touch")
        pk = "material_id" if table == "material" else "org_id"
        con.execute(f"""CREATE TRIGGER trg_{table}_touch AFTER UPDATE ON {table}
          WHEN NEW.updated_at=OLD.updated_at AND NEW.updated_at IS NOT strftime('%Y-%m-%dT%H:%M:%fZ','now')
          BEGIN UPDATE {table} SET updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE {pk}=NEW.{pk}; END""")
    for table, pk in (("verification_event", "event_id"), ("audit_log", "log_id")):
        for operation in ("UPDATE", "DELETE"):
            con.execute(f"CREATE TRIGGER protect_{table}_{operation} BEFORE {operation} ON {table} "
                        "BEGIN SELECT RAISE(ABORT,'append-only ledger'); END")
        con.execute(f"CREATE TRIGGER protect_{table}_replace BEFORE INSERT ON {table} "
                    f"WHEN EXISTS(SELECT 1 FROM {table} WHERE {pk}=NEW.{pk}) "
                    "BEGIN SELECT RAISE(ABORT,'ledger replacement forbidden'); END")
    con.execute("""CREATE TRIGGER guard_verification_insert BEFORE INSERT ON verification_event
        WHEN review_actor() IS NULL OR NEW.verified_by IS NOT review_actor()
          OR NEW.field_name IS NOT review_field() OR NEW.evidence_file_id IS NOT review_evidence()
          OR NEW.reason IS NULL OR trim(NEW.reason)='' OR NEW.verification_method<>'manual_original'
        BEGIN SELECT RAISE(ABORT,'human review context required'); END""")

    # SQL checks work even with external SQLite tools. The API additionally
    # resolves filesystem junctions/symlinks and checks actual containment.
    invalid = """NEW.relative_path='' OR instr(NEW.relative_path,':')>0
        OR instr(NEW.relative_path,char(92))>0 OR substr(NEW.relative_path,1,1)='/'
        OR instr('/'||NEW.relative_path||'/','/../')>0
        OR instr('/'||NEW.relative_path||'/','/./')>0 OR instr(NEW.relative_path,'//')>0"""
    for operation in ("INSERT", "UPDATE"):
        con.execute(f"CREATE TRIGGER guard_path_{operation} BEFORE {operation} ON material_file "
                    f"WHEN {invalid} BEGIN SELECT RAISE(ABORT,'invalid relative path'); END")
    con.execute("""CREATE TRIGGER immutable_file_evidence BEFORE UPDATE ON material_file
        WHEN NEW.material_id<>OLD.material_id OR NEW.role<>OLD.role
          OR NEW.relative_path<>OLD.relative_path OR NEW.sha256<>OLD.sha256
          OR NEW.size_bytes<>OLD.size_bytes OR NEW.file_id<>OLD.file_id
        BEGIN SELECT RAISE(ABORT,'register a new file version; evidence is immutable'); END""")
    for table in ("material", "material_file", "certificate_detail", "performance_detail"):
        con.execute(f"CREATE TRIGGER no_delete_{table} BEFORE DELETE ON {table} "
                    "BEGIN SELECT RAISE(ABORT,'archive instead of deleting evidence'); END")
    con.execute("""CREATE TRIGGER stable_material_identity BEFORE UPDATE ON material
        WHEN NEW.material_id<>OLD.material_id OR (OLD.material_code<>'' AND NEW.material_code<>OLD.material_code)
        BEGIN SELECT RAISE(ABORT,'material identity is immutable'); END""")
    no_replace_guards(con)

    # This slice supports two detail types. Every factual column in those
    # tables is guarded; unsupported detail types cannot be written at all.
    for table, meta in (("certificate_detail", {"material_id", "certificate_kind", "notes"}),
                        ("performance_detail", {"material_id", "notes"})):
        for _, col, _, _, default, _ in con.execute(f"PRAGMA table_info({table})").fetchall():
            if col in meta:
                continue
            alias = "contract_amount" if col == "contract_amount_cents" else col
            for operation in ("INSERT", "UPDATE"):
                if operation == "INSERT":
                    condition = f"NEW.{col} IS NOT NULL"
                    if default is not None:
                        condition += f" AND NEW.{col} IS NOT ({default})"
                    old = "NULL"
                else:
                    condition = f"NEW.{col} IS NOT OLD.{col}"
                    old = f"CAST(OLD.{col} AS TEXT)"
                con.execute(f"""CREATE TRIGGER review_{table}_{col}_{operation}
                    BEFORE {operation} ON {table} WHEN {condition}
                    BEGIN
                      SELECT CASE WHEN review_actor() IS NULL OR review_field()<>'{alias}'
                        THEN RAISE(ABORT,'facts require explicit human review') END;
                      INSERT INTO verification_event(material_id,field_name,action,old_value,new_value,
                        evidence_file_id,source_extracted_id,verification_method,verified_by,reason,occurred_at)
                      VALUES(NEW.material_id,'{alias}',CASE WHEN NEW.{col} IS NULL THEN 'retract'
                        WHEN {old} IS NULL THEN 'verify' ELSE 'correct' END,{old},CAST(NEW.{col} AS TEXT),
                        review_evidence(),review_candidate(),'manual_original',review_actor(),review_reason(),review_time());
                      INSERT INTO audit_log(at,actor,action,entity_type,entity_id,detail_json,note)
                      VALUES(review_time(),review_actor(),'fact.review','fact',NEW.material_id,
                        json_object('field','{alias}','old',{old},'new',CAST(NEW.{col} AS TEXT)),review_reason());
                    END""")
    for table in ("person_certificate_detail", "patent_detail", "patent_legal_status",
                  "software_copyright_detail", "audit_report_detail", "material_attr"):
        for op in ("INSERT", "UPDATE", "DELETE"):
            con.execute(f"CREATE TRIGGER unsupported_{table}_{op} BEFORE {op} ON {table} "
                        "BEGIN SELECT RAISE(ABORT,'detail type outside v0.1'); END")
    final_identity_guards(con)
    con.execute("UPDATE schema_meta SET meta_value='4-pilot',updated_at=? WHERE meta_key='schema_version'", (now(),))


class Store:
    def __init__(self, config, *, readonly=False):
        self.c = config
        self.review = None
        if readonly:
            self.db = sqlite3.connect(config.db_path.as_uri() + "?mode=ro", uri=True, timeout=5)
            self.db.execute("PRAGMA query_only=ON")
        else:
            if not config.db_path.is_file():
                raise FileNotFoundError("initialize a new v0.1 database first")
            self.db = sqlite3.connect(config.db_path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA recursive_triggers=ON")
        try:
            version = self.db.execute("SELECT meta_value FROM schema_meta WHERE meta_key='schema_version'").fetchone()
            if version is None or version[0] != SCHEMA_VERSION:
                raise ValueError("expected schema " + SCHEMA_VERSION + "; use an explicit migration for older databases")
        except BaseException:
            self.db.close()
            raise
        for key in ("actor", "field", "evidence", "candidate", "reason", "time", "material", "sha256", "action"):
            self.db.create_function("review_" + key, 0,
                                    lambda k=key: self.review.get(k) if self.review else None)
        self.db.create_function('org_norm',1,org_norm)
        # Once initialized, app connections cannot remove guards or alter schema.
        denied = {getattr(sqlite3, name) for name in (
            "SQLITE_DROP_TRIGGER", "SQLITE_DROP_TABLE", "SQLITE_DROP_INDEX", "SQLITE_DROP_VIEW",
            "SQLITE_ALTER_TABLE", "SQLITE_ATTACH", "SQLITE_DETACH", "SQLITE_CREATE_TRIGGER",
            "SQLITE_CREATE_TABLE", "SQLITE_CREATE_INDEX", "SQLITE_CREATE_VIEW")}
        self.db.set_authorizer(lambda action, a, b, c, d: sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK)

    @staticmethod
    def initialize(config):
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation, no unlink/reset/--force path.
        with config.db_path.open("xb"):
            pass
        con = sqlite3.connect(config.db_path)
        try:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("PRAGMA busy_timeout=5000")
            con.execute("PRAGMA journal_mode=WAL")
            schema = Path(__file__).with_name("schema_v1.sql")
            con.executescript(schema.read_text(encoding="utf-8"))
            harden(con)
            release_schema(con)
            upgrade(con)
            con.commit()
        finally:
            con.close()

    @staticmethod
    def migrate(config):
        import uuid
        if not config.db_path.is_file():raise FileNotFoundError(config.db_path)
        con=sqlite3.connect(config.db_path,timeout=5)
        try:
            con.execute('PRAGMA foreign_keys=ON');con.execute('PRAGMA busy_timeout=5000')
            version=con.execute("SELECT meta_value FROM schema_meta WHERE meta_key='schema_version'").fetchone()[0]
            if version==SCHEMA_VERSION:return {'changed':False,'schema':SCHEMA_VERSION}
            if version not in ('jty-mcp-0.1.0','jty-mcp-0.2.0','jty-mcp-0.3.0'):raise ValueError('migration only accepts earlier compatible schemas; database was not changed')
            config.backup_dir.mkdir(parents=True,exist_ok=True)
            backup=config.backup_dir/('schema-before-v04-'+uuid.uuid4().hex[:12]+'.db')
            with backup.open('xb'):pass
            with closing(sqlite3.connect(backup)) as dest:con.backup(dest)
            with con:
                con.execute('BEGIN IMMEDIATE');upgrade(con)
                con.execute("INSERT INTO audit_log(at,actor,action,entity_type,detail_json) VALUES(?,'offline-cli','schema.migrate','system',?)",(now(),json.dumps({'from':version,'to':SCHEMA_VERSION,'backup':str(backup)})))
            config.log('sqlite.migration',old=version,new=SCHEMA_VERSION,backup=str(backup))
            return {'changed':True,'schema':SCHEMA_VERSION,'pre_migration_database':str(backup)}
        finally:con.close()

    def close(self):
        self.db.close()

    def register_file(self, material_id, path, role, version_id=None):
        path = Path(path).resolve()
        relative = path.relative_to(self.c.repository_root).as_posix()
        safe_path(self.c.repository_root, relative)
        return self.db.execute("""INSERT INTO material_file(material_id,role,relative_path,filename,
            extension,sha256,size_bytes,imported_at,source_type,version_id)
            VALUES(?,?,?,?,?,?,?,?,?,?) RETURNING file_id""", (material_id, role, relative, path.name,
                path.suffix[1:].lower(), digest(path), path.stat().st_size, now(), "generated" if role != "original" else "scan", version_id)).fetchone()[0]

    def verify(self, material_id, field, value, *, actor, reason, evidence_file_id, candidate_id=None):
        if field_spec(field) is None or not isinstance(actor,str) or not actor.strip() or not isinstance(reason,str) or not reason.strip():
            raise ValueError("unsupported fact or missing reviewer/reason")
        m=self.db.execute('SELECT m.*,t.code FROM material m JOIN material_type t ON t.type_id=m.material_type_id WHERE material_id=?',(material_id,)).fetchone()
        if m is None or not allowed_field(m['code'],field):raise ValueError('verified field is not allowed for this material type')
        value=validate_value(field,value)
        table, column, kind = field_spec(field,m['code'])
        ev = self.db.execute("SELECT * FROM material_file WHERE file_id=? AND material_id=? AND role='original' AND is_current=1",
                             (evidence_file_id, material_id)).fetchone()
        if ev is None or digest(safe_path(self.c.repository_root, ev["relative_path"])) != ev["sha256"]:
            raise ValueError("review must cite the unchanged original of this material")
        if candidate_id is not None and not self.db.execute("SELECT 1 FROM extracted_fact WHERE extracted_id=? AND material_id=? AND field_name=?",
                    (candidate_id, material_id, field)).fetchone():
            raise ValueError("candidate does not belong to reviewed fact")
        if self.db.in_transaction:
            raise RuntimeError("review requires its own transaction")
        self.review = dict(actor=actor, field=field, evidence=evidence_file_id, candidate=candidate_id, reason=reason, time=now(),material=material_id,sha256=ev['sha256'],action='retract' if value is None else 'review')
        try:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                where='org_id=?' if field in ORG_FIELDS else 'material_id=?'
                params=[m['subject_org_id'] if field in ORG_FIELDS else material_id]
                if table=='audit_report_detail' and field=='fiscal_year' and not self.db.execute('SELECT 1 FROM audit_report_detail WHERE material_id=?',(material_id,)).fetchone():
                    self.db.execute('INSERT INTO audit_report_detail(material_id,fiscal_year) VALUES(?,?)',(material_id,value))
                    self.db.execute("UPDATE material SET verification_status='human_verified',verified_by=?,verified_at=? WHERE material_id=?",(actor,now(),material_id))
                    return
                if field in GRADE_FIELDS:where+=' AND activity=?';params.append(GRADE_FIELDS[field])
                qname=qualification_name(field)
                if qname is not None:
                    where+=' AND qualification_name=?';params.append(qname)
                    self.db.execute('INSERT INTO construction_qual_item(material_id,qualification_name) SELECT ?,? WHERE NOT EXISTS(SELECT 1 FROM construction_qual_item WHERE material_id=? AND qualification_name=?)',(material_id,qname,material_id,qname))
                old = self.db.execute(f"SELECT {column} FROM {table} WHERE {where}",params).fetchone()
                if old is None:
                    raise ValueError("missing detail row")
                stored='' if field=='enterprise_name' and value is None else 0 if field=='long_term_valid' and value is None else value
                if old[0] == stored:
                    self.db.execute("""INSERT INTO verification_event(material_id,field_name,action,old_value,new_value,
                        evidence_file_id,source_extracted_id,verification_method,verified_by,reason,occurred_at)
                        VALUES(?,?,?,?,?,?,?, 'manual_original',?,?,?)""",
                        (material_id, field, 'retract' if value is None else 'verify', None if old[0] is None else str(old[0]), None if value is None else str(value),
                         evidence_file_id, candidate_id, actor, reason, self.review['time']))
                else:
                    if field=='enterprise_name':self.db.execute(f'UPDATE org SET name=?,name_norm=? WHERE {where}',[stored,org_norm(stored),*params])
                    else:self.db.execute(f"UPDATE {table} SET {column}=? WHERE {where}",[stored,*params])
                self.db.execute("UPDATE material SET verification_status=?,verified_by=?,verified_at=? WHERE material_id=?",
                                ("human_verified" if value is not None else "unverified", actor, now(), material_id))
                if candidate_id:
                    # A human may correct a candidate; do not label a different
                    # OCR value accepted merely because it was reviewed.
                    candidate = self.db.execute("SELECT normalized_value FROM extracted_fact WHERE extracted_id=?", (candidate_id,)).fetchone()[0]
                    self.db.execute("UPDATE extracted_fact SET review_status=?,reviewed_by=?,reviewed_at=?,review_note=? WHERE extracted_id=?",
                                    ("accepted" if str(value) == candidate else "rejected", actor, now(), reason, candidate_id))
        finally:
            self.review = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
