"""Deterministic title building over verified/consensus values (spec #19).

Kept independent from both workflow and consensus so import and dual-engine
processing can both renew the title without import cycles. Never invents
wording: a title is built only when a reliable value exists, otherwise the
current title stays.
"""
import json

from .common import now
from .fields import ORG_FIELDS, GRADE_FIELDS, field_spec, PERFORMANCE_TYPES, PERSON_TYPES
from .store import Store
from .workflow import audit


def fact_value(db, material_id, type_code, field):
    """Current authoritative column value (human or machine), or None."""
    table, column, kind = field_spec(field, type_code)
    if field in ORG_FIELDS:
        row = db.execute('SELECT subject_org_id FROM material WHERE material_id=?', (material_id,)).fetchone()
        if not row or not row[0]:
            return None
        row = db.execute(f'SELECT {column} FROM org WHERE org_id=?', (row[0],)).fetchone()
    elif field in GRADE_FIELDS:
        row = db.execute('SELECT grade FROM power_license_item WHERE material_id=? AND activity=?',
                         (material_id, GRADE_FIELDS[field])).fetchone()
    else:
        row = db.execute(f'SELECT {column} FROM {table} WHERE material_id=?', (material_id,)).fetchone()
    value = row[0] if row else None
    if field == 'enterprise_name' and value == '':
        value = None
    return value


def auto_title_value(db, material_id, type_code):
    """A new title, or None to keep the current one."""
    def unique(field):
        rows = db.execute("""SELECT DISTINCT e.normalized_value FROM extracted_fact e
            JOIN extraction_run r USING(run_id)
            WHERE e.material_id=? AND e.field_name=? AND e.review_status='pending'
              AND (e.verification='machine_verified' OR e.verification IS NULL)""",
            (material_id, field)).fetchall()
        return rows[0][0] if len(rows) == 1 else None

    def fact(field):
        value = fact_value(db, material_id, type_code, field)
        return str(value) if value not in (None, '') else None

    if type_code == 'business_license':
        name = fact('enterprise_name') or unique('enterprise_name')
        return (str(name) + ' 营业执照') if name else None
    if type_code in ('audit_report', 'financial_statement'):
        year = fact('fiscal_year') or unique('fiscal_year')
        label = '审计报告' if type_code == 'audit_report' else '财务报表'
        return (str(year) + '年度' + label) if year else None
    if type_code == 'tax_credit':
        year = fact('year') or unique('year')
        return (str(year) + '年度纳税信用') if year else None
    if type_code == 'patent':
        return unique('patent_name')
    if type_code == 'software_copyright':
        return unique('software_name')
    if type_code in PERFORMANCE_TYPES:
        return None  # display layer derives names from verified project facts
    if type_code in PERSON_TYPES:
        specialty = fact('specialty') or unique('specialty')
        if specialty:
            return str(specialty)
        cert = fact('certificate_no') or unique('certificate_no')
        return ('证书 ' + str(cert)) if cert else None
    cert = fact('certificate_no') or unique('certificate_no')
    return ('证书 ' + str(cert)) if cert else None


def maybe_auto_title(config, material_id):
    """Renew an auto-origin title after new recognition evidence. Only titles
    that provably came from a filename fallback (or an earlier auto build) are
    touched; CLI titles and user-chosen names are never rewritten."""
    with Store(config, readonly=True) as s:
        row = s.db.execute("""SELECT m.title,t.code FROM material m
            JOIN material_type t ON t.type_id=m.material_type_id WHERE material_id=?""",
            (material_id,)).fetchone()
        if row is None:
            return
        origin_row = s.db.execute("""SELECT detail_json FROM audit_log
            WHERE action='material.source_context' AND entity_id=? ORDER BY log_id DESC LIMIT 1""",
            (material_id,)).fetchone()
        origin = json.loads(origin_row[0]).get('title_origin') if origin_row else None
    if origin not in ('filename_fallback', 'auto'):
        return  # no explicit auto-origin evidence: keep the stored title
    with Store(config) as s:
        with s.db:
            title = auto_title_value(s.db, material_id, row['code'])
            if title and title != row['title'] and len(title) <= 200:
                s.db.execute('UPDATE material SET title=? WHERE material_id=?', (title, material_id))
                audit(s.db, 'local_webui', 'material.auto_title', 'material', material_id,
                      dict(title=title, previous=row['title'], builder='deterministic_v0.7'))
