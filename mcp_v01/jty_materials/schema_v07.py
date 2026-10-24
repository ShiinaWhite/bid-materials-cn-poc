"""v0.7 Zero-Fill: honest machine verification semantics, minimal-invasive.

- extracted_fact gains verification/verified_at/verification_json so a
  dual-engine consensus can be recorded per candidate without touching the
  append-only candidate identity (immutable_candidate_source still holds).
- verification_event is rebuilt with one extra verification_method value:
  'dual_engine_consensus' (two independent machine chains + validator). It is
  NOT 'manual_original' and never claims a human actor.
- The fact-write triggers are regenerated from the current closed vocabulary
  with exactly two admitted write contexts: an explicit human review context
  (unchanged semantics) and an explicit machine verification context bound to
  the current original SHA. Any other write still aborts.
"""
import json

from .common import now
from .schema_v06 import upgrade as upgrade_v06
from .fields import ORG_FIELDS, GRADE_FIELDS, DETAIL_FIELDS, TYPE_TABLE

VERSION = 'jty-mcp-0.7.0'

MACHINE_METHOD = 'dual_engine_consensus'


def _cover():
    """(table, alias, column, default, org_binding, material_expr) for every
    guarded fact column, derived from the closed vocabulary in fields.py."""
    rows = []
    for alias, (_, column, _) in ORG_FIELDS.items():
        rows.append(('org', alias, column, None, True, 'review_material()'))
    cert = [('valid_from', None), ('valid_until', None), ('long_term_valid', None),
            ('certificate_no', None), ('issuing_authority', None), ('scope', None),
            ('standard', None), ('issue_date', None), ('title', None), ('year', 'tax_year'),
            ('rating', None), ('authority_source', None)]
    for alias, column in cert:
        rows.append(('certificate_detail', alias, column or alias, None, False, 'NEW.material_id'))
    for alias, (column, _) in DETAIL_FIELDS['performance_detail'].items():
        rows.append(('performance_detail', alias, column, None, False, 'NEW.material_id'))
    rows.append(('power_license_item', 'GRADE', 'grade', None, False, 'NEW.material_id'))
    for table in ('person_certificate_detail', 'patent_detail', 'software_copyright_detail', 'audit_report_detail'):
        for alias, (column, _) in DETAIL_FIELDS[table].items():
            default = 'unknown' if (table, column) in (('patent_detail', 'patent_type'),
                                                       ('audit_report_detail', 'audit_opinion')) else None
            rows.append((table, alias, column, default, False, 'NEW.material_id'))
    # Human qualification grading keeps its ledger (machine policy excludes it).
    rows.append(('construction_qual_item', 'QUAL', 'grade', None, False, 'NEW.material_id'))
    return rows


def _drop_review_triggers(con):
    # The historical naming family is v0X_review_*, v04_v02_review_* and the
    # original harden() names; drop the whole family before regeneration.
    prefixes = ('v02_review_', 'v03_review_', 'v04_review_', 'review_certificate_detail_',
                'review_performance_detail_')
    names = [r[0] for r in con.execute("SELECT name FROM sqlite_schema WHERE type='trigger'").fetchall()]
    for name in names:
        if name.startswith(prefixes) or (name.startswith('v0') and '_review_' in name):
            con.execute('DROP TRIGGER "' + name + '"')


def _field_expr(alias):
    if alias == 'GRADE':
        return "NEW.activity||'_grade'"
    if alias == 'QUAL':
        return "'qualification:'||NEW.qualification_name"
    return "'" + alias + "'"


def event_material_expr(material_expr):
    """Org writes resolve the material via the review context; machine writes
    must resolve it via the machine context instead."""
    if material_expr == 'review_material()':
        return 'CASE WHEN machine_write() IS 1 THEN machine_material() ELSE review_material() END'
    return material_expr


def _create_fact_trigger(con, table, alias, column, default, org, material_expr, update_only=False,
                         column_default=None):
    field = _field_expr(alias)
    insert_condition = f'NEW.{column} IS NOT NULL'
    if column_default is not None:
        insert_condition += f' AND NEW.{column} IS NOT ({column_default})'
    if default:
        insert_condition += f" AND NEW.{column}<>'{default}'"
    operations = (('UPDATE', f'NEW.{column} IS NOT OLD.{column}'),) if update_only else \
        (('INSERT', insert_condition), ('UPDATE', f'NEW.{column} IS NOT OLD.{column}'))
    for op, condition in operations:
        old = 'NULL' if op == 'INSERT' else f'CAST(OLD.{column} AS TEXT)'
        new = f'NEW.{column}'
        if org:
            human = (f"review_actor() IS NOT NULL AND review_field() IS ({field})"
                     " AND NOT EXISTS(SELECT 1 FROM material WHERE material_id=review_material()"
                     " AND subject_org_id<>NEW.org_id)")
            machine = (f"machine_write() IS 1 AND machine_field() IS ({field})"
                       " AND NOT EXISTS(SELECT 1 FROM material WHERE material_id=machine_material()"
                       " AND subject_org_id<>NEW.org_id)")
        else:
            human = (f"review_actor() IS NOT NULL AND review_field() IS ({field})"
                     f" AND {material_expr} IS review_material()")
            machine = (f"machine_write() IS 1 AND machine_field() IS ({field})"
                       f" AND {material_expr} IS machine_material()")
        con.execute(f"""CREATE TRIGGER v07_review_{table}_{alias}_{op} BEFORE {op} ON {table} WHEN {condition} BEGIN
          SELECT CASE WHEN NOT (({machine}) OR ({human}))
            THEN RAISE(ABORT,'facts require explicit human review or machine verification context') END;
          INSERT INTO verification_event(material_id,field_name,action,old_value,new_value,evidence_file_id,
            source_extracted_id,verification_method,verified_by,reason,occurred_at)
          VALUES({event_material_expr(material_expr)},{field},
            CASE WHEN review_action()='retract' AND machine_write() IS NOT 1 THEN 'retract'
                 WHEN {old} IS NULL OR {old}='' THEN 'verify' ELSE 'correct' END,
            {old},
            CASE WHEN review_action()='retract' AND machine_write() IS NOT 1 THEN NULL
                 ELSE CAST({new} AS TEXT) END,
            CASE WHEN machine_write() IS 1 THEN machine_evidence() ELSE review_evidence() END,
            CASE WHEN machine_write() IS 1 THEN NULL ELSE review_candidate() END,
            CASE WHEN machine_write() IS 1 THEN '{MACHINE_METHOD}' ELSE 'manual_original' END,
            CASE WHEN machine_write() IS 1 THEN machine_identity() ELSE review_actor() END,
            CASE WHEN machine_write() IS 1 THEN machine_reason() ELSE review_reason() END,
            CASE WHEN machine_write() IS 1 THEN machine_time() ELSE review_time() END);
          END""")


def _column_defaults(con, table):
    """PRAGMA dflt_value per column: INSERT conditions must ignore rows whose
    value equals the column default (same rule as the v02/v04 guards)."""
    return {r[1]: r[4] for r in con.execute(f'PRAGMA table_info({table})').fetchall()}


def recreate_fact_triggers(con):
    """Unified regeneration: same human semantics as v02/v04, plus a machine
    verification context. Guard coverage is identical to the previous set."""
    _drop_review_triggers(con)
    defaults = {table: _column_defaults(con, table) for table in
                {row[0] for row in _cover()}}
    for table, alias, column, default, org, material_expr in _cover():
        _create_fact_trigger(con, table, alias, column, default, org, material_expr,
                             column_default=defaults[table].get(column),
                             update_only=table in ('org', 'power_license_item', 'construction_qual_item'))


def recreate_guards(con):
    con.execute('DROP TRIGGER IF EXISTS guard_verification_insert')
    con.execute(f"""CREATE TRIGGER guard_verification_insert BEFORE INSERT ON verification_event
      WHEN NOT (
        (review_actor() IS NOT NULL
          AND NEW.verification_method='manual_original' AND trim(NEW.reason)<>''
          AND NEW.material_id IS review_material() AND NEW.verified_by IS review_actor()
          AND NEW.field_name IS review_field() AND NEW.evidence_file_id IS review_evidence()
          AND NEW.reason IS review_reason()
          AND EXISTS(SELECT 1 FROM material_file f WHERE f.file_id=NEW.evidence_file_id
            AND f.material_id=NEW.material_id AND f.role='original' AND f.is_current=1 AND f.sha256=review_sha256()))
        OR
        (machine_write() IS 1
          AND NEW.verification_method='{MACHINE_METHOD}' AND trim(NEW.reason)<>''
          AND NEW.material_id IS machine_material() AND NEW.verified_by IS machine_identity()
          AND NEW.field_name IS machine_field() AND NEW.evidence_file_id IS machine_evidence()
          AND NEW.reason IS machine_reason()
          AND EXISTS(SELECT 1 FROM material_file f WHERE f.file_id=NEW.evidence_file_id
            AND f.material_id=NEW.material_id AND f.role='original' AND f.is_current=1 AND f.sha256=machine_sha256()))
      )
      BEGIN SELECT RAISE(ABORT,'explicit human review or machine verification of the current original required'); END""")
    # Append-only ledger protection survives the table rebuild.
    for operation in ('UPDATE', 'DELETE'):
        con.execute(f"DROP TRIGGER IF EXISTS protect_verification_event_{operation}")
        con.execute(f"""CREATE TRIGGER protect_verification_event_{operation} BEFORE {operation} ON verification_event
            BEGIN SELECT RAISE(ABORT,'append-only ledger'); END""")
    con.execute("""CREATE TRIGGER protect_verification_event_replace BEFORE INSERT ON verification_event
        WHEN EXISTS(SELECT 1 FROM verification_event WHERE event_id=NEW.event_id)
        BEGIN SELECT RAISE(ABORT,'ledger replacement forbidden'); END""")
    # Material type/entity binding stays immutable EXCEPT for the controlled
    # exception-resolution reclassification path (review context only; there
    # is no machine or plain-SQL route).
    con.execute('DROP TRIGGER IF EXISTS v02_material_binding')
    con.execute("""CREATE TRIGGER v02_material_binding BEFORE UPDATE ON material
      WHEN NEW.material_type_id<>OLD.material_type_id OR NEW.subject_org_id IS NOT OLD.subject_org_id
      BEGIN SELECT CASE WHEN review_action() IS NOT 'reclassify'
        THEN RAISE(ABORT,'material type/entity binding is immutable') END; END""")
    # AFTER INSERT audit projection (same semantics as v02_verification_audit).
    org_fields = ','.join("'" + f + "'" for f in ORG_FIELDS)
    con.execute('DROP TRIGGER IF EXISTS v02_verification_audit')
    con.execute(f"""CREATE TRIGGER v07_verification_audit AFTER INSERT ON verification_event BEGIN
      INSERT INTO audit_log(at,actor,action,entity_type,entity_id,detail_json,note)
      VALUES(NEW.occurred_at,NEW.verified_by,'fact.'||NEW.action,
        CASE WHEN NEW.field_name IN ({org_fields}) THEN 'org' ELSE 'fact' END,
        CASE WHEN NEW.field_name IN ({org_fields}) THEN (SELECT subject_org_id FROM material WHERE material_id=NEW.material_id) ELSE NEW.material_id END,
        json_object('material_id',NEW.material_id,'org_id',(SELECT subject_org_id FROM material WHERE material_id=NEW.material_id),
          'field',NEW.field_name,'old',NEW.old_value,'new',NEW.new_value,'event_id',NEW.event_id,
          'evidence_file_id',NEW.evidence_file_id,'evidence_sha256',(SELECT sha256 FROM material_file WHERE file_id=NEW.evidence_file_id),
          'verification_method',NEW.verification_method),NEW.reason);
      END""")


def _rebuild_verification_event(con):
    """Only change: verification_method gains 'dual_engine_consensus'.
    Dependent views are dropped and restored verbatim (same columns)."""
    columns = [r[1] for r in con.execute('PRAGMA table_info(verification_event)').fetchall()]
    dependent = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_schema WHERE type='view' AND sql LIKE '%verification_event%'").fetchall()]
    saved = {name: con.execute("SELECT sql FROM sqlite_schema WHERE type='view' AND name=?",
                               (name,)).fetchone()[0] for name in dependent}
    for name in dependent:
        con.execute('DROP VIEW ' + name)
    con.execute(f"""CREATE TABLE verification_event_v07 (
        event_id        INTEGER PRIMARY KEY,
        material_id     INTEGER NOT NULL REFERENCES material(material_id) ON DELETE CASCADE,
        field_name      TEXT NOT NULL,
        action          TEXT NOT NULL CHECK (action IN ('set','verify','correct','dispute','retract','import')),
        old_value       TEXT,
        new_value       TEXT,
        evidence_file_id INTEGER REFERENCES material_file(file_id),
        evidence_external TEXT,
        source_extracted_id INTEGER REFERENCES extracted_fact(extracted_id),
        verification_method TEXT NOT NULL CHECK (verification_method IN (
                              'manual_original','manual_system','external_lookup','import',
                              'ai_assisted_review','direct_edit','{MACHINE_METHOD}')),
        verified_by     TEXT NOT NULL,
        reason          TEXT,
        occurred_at     TEXT NOT NULL
    ) STRICT""")
    con.execute('INSERT INTO verification_event_v07(' + ','.join(columns) + ')'
                ' SELECT ' + ','.join(columns) + ' FROM verification_event')
    con.execute('DROP TABLE verification_event')
    con.execute('ALTER TABLE verification_event_v07 RENAME TO verification_event')
    for name in dependent:
        con.execute(saved[name])


def upgrade(con):
    version = con.execute("SELECT meta_value FROM schema_meta WHERE meta_key='schema_version'").fetchone()[0]
    if version == VERSION:
        return False
    if version != 'jty-mcp-0.6.0':
        upgrade_v06(con)
    con.execute("ALTER TABLE extracted_fact ADD COLUMN verification TEXT"
                " CHECK (verification IS NULL OR verification IN ('machine_verified','disputed'))")
    con.execute('ALTER TABLE extracted_fact ADD COLUMN verified_at TEXT')
    con.execute('ALTER TABLE extracted_fact ADD COLUMN verification_json TEXT')
    # Old fact triggers reference the verification_event table by name: drop
    # them before the rebuild, then regenerate against the new table.
    _drop_review_triggers(con)
    _rebuild_verification_event(con)
    recreate_fact_triggers(con)
    recreate_guards(con)
    con.execute("UPDATE schema_meta SET meta_value=?,updated_at=? WHERE meta_key='schema_version'", (VERSION, now()))
    return True
