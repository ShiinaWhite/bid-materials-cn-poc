"""Field-level dual-engine consensus and honest machine verification.

Hard conditions for machine_verified (spec #15) — all must hold, any failure
means NO auto verify:
  1. OCR and Vision both ran against the same current original
  2. the original SHA is re-verified on disk inside the write transaction
  3. the two chains are independent (different extractor families by design)
  4. the two canonical values are exactly equal
  5. the field is allowed for machine verification by policy
  6. the deterministic validator passes (USCC check digit, patterns, types)
  7. candidate evidence binds back to that original
  8. the original has not been replaced (re-checked is_current=1)
  9. no existing human-verified fact is contradicted (human facts are never
     auto-overwritten; a machine fact is re-derived from the new original)

Existing human facts have the highest priority (spec #16): conflicts become
disputes/exceptions, never silent writes.
"""
import json

from .common import digest, normalize, now, safe_path
from .fields import ALLOWED, ORG_FIELDS, GRADE_FIELDS, field_spec, allowed_field, \
    CERTIFICATES, TYPE_TABLE, PERFORMANCE_TYPES, PERSON_TYPES
from .normalize import machine_validator
from .store import Store
from .workflow import audit
from . import providers, vision
from .titling import fact_value as _fact_value, auto_title_value, maybe_auto_title as _maybe_auto_title

# Long prose and derived fields are never machine-verified in v0.7; they stay
# candidates / searchable text (spec #12).
MACHINE_VERIFY_EXCLUDE = {'scope', 'audit_opinion', 'title'}
MACHINE_VERIFY_MAX_LEN = 200


def machine_verify_allowed(field):
    if field in MACHINE_VERIFY_EXCLUDE or field.startswith('qualification:'):
        return False  # free-form qualification names are never machine-matched
    return field_spec(field) is not None


def current_original(config, material_id):
    with Store(config, readonly=True) as s:
        row = s.db.execute("""SELECT f.file_id,f.sha256,f.relative_path,f.version_id,
                m.verification_status,t.code AS type_code
            FROM material_file f JOIN material m USING(material_id)
            JOIN material_type t ON t.type_id=m.material_type_id
            WHERE f.material_id=? AND f.role='original' AND f.is_current=1""", (material_id,)).fetchone()
    if row is None:
        raise ValueError('material has no current original')
    return dict(file_id=row['file_id'], sha256=row['sha256'], relative_path=row['relative_path'],
                version_id=row['version_id'], verification_status=row['verification_status'],
                code=row['type_code'])


def _candidates(db, original_file_id, material_id, extractor_like):
    rows = db.execute("""SELECT e.extracted_id,e.field_name,e.normalized_value,e.review_status,
            r.run_id,r.extractor FROM extracted_fact e JOIN extraction_run r USING(run_id)
        WHERE r.source_file_id=? AND e.material_id=? AND e.review_status='pending'
          AND r.extractor LIKE ?""", (original_file_id, material_id, extractor_like)).fetchall()
    return [dict(r) for r in rows]


def fact_origin(db, material_id, field):
    """'human' | 'machine' | None. A latest 'retract' means the human withdrew
    the fact, so the field is writable again (None)."""
    row = db.execute("""SELECT action,verification_method FROM verification_event
        WHERE material_id=? AND field_name=? ORDER BY event_id DESC LIMIT 1""",
        (material_id, field)).fetchone()
    if row is None or row[0] == 'retract':
        return None
    return 'human' if row[1] != 'dual_engine_consensus' else 'machine'


def dual_engine_process(config, material_id, actor='local_webui', provider=None):
    """Run the independent vision chain against the current original and
    reconcile field-by-field with the OCR chain. Manager action."""
    provider = provider or providers.vision_provider(config)
    original = current_original(config, material_id)
    original_path = safe_path(config.repository_root, original['relative_path'])
    if digest(original_path) != original['sha256']:
        raise ValueError('current original checksum mismatch')
    original['path'] = str(original_path)
    with Store(config, readonly=True) as s:
        ocr_rows = _candidates(s.db, original['file_id'], material_id, 'umi-ocr%')
    with Store(config) as s:
        with s.db:
            vision_receipt = vision.run_vision_extraction(config, s.db, material_id, original, provider=provider)
    vision_rows = [dict(field_name=row['field'], normalized_value=row['canonical'],
                        extracted_id=row['extracted_id'], run_id=vision_receipt['run_id'],
                        extractor=vision_receipt.get('extractor', ''), page_no=row['page'])
                   for row in vision_receipt.get('candidates', [])]
    if vision_receipt['status'] != 'succeeded':
        receipt = dict(material_id=material_id, vision_run_id=vision_receipt['run_id'],
                       vision_status=vision_receipt['status'], vision_error=vision_receipt.get('error'),
                       verified={}, disputed={}, ambiguous={}, single_chain=[], validator_failed=[],
                       human_conflicts=[], policy_skipped=[],
                       original_file_id=original['file_id'], original_sha256=original['sha256'])
        with Store(config) as s:
            with s.db:
                audit(s.db, actor, 'material.vision_deferred', 'material', material_id,
                      dict(run_id=vision_receipt['run_id'], error=vision_receipt.get('error'),
                           note='vision failed; original already ingested, candidates stay pending'))
        return receipt

    verified, disputed, ambiguous = {}, {}, {}
    single_chain, validator_failed, human_conflicts, policy_skipped = [], [], [], []
    identity = 'machine:' + vision_receipt.get('extractor', 'vision')
    with Store(config) as s:
        with s.db:
            s.db.execute('BEGIN IMMEDIATE')
            cur = s.db.execute('SELECT is_current,sha256,version_id FROM material_file WHERE file_id=?',
                               (original['file_id'],)).fetchone()
            # Final critical-section recheck (spec review blocker #2): the disk
            # bytes, the registered row and the recognized original must ALL
            # still be the same evidence before any machine fact is written.
            if cur is None or not cur['is_current'] or cur['sha256'] != original['sha256'] \
                    or cur['version_id'] != original['version_id'] \
                    or digest(safe_path(config.repository_root, original['relative_path'])) != original['sha256']:
                raise ValueError('current original changed during recognition; no consensus written')
            type_code = s.db.execute("""SELECT t.code FROM material m JOIN material_type t
                ON t.type_id=m.material_type_id WHERE material_id=?""", (material_id,)).fetchone()[0]
            ocr_map, vision_map = _group(ocr_rows), _group(vision_rows)
            for field in sorted(set(ocr_map) | set(vision_map)):
                ocr_values = ocr_map.get(field, {})
                vision_values = vision_map.get(field, {})
                # Policy precheck (final tiny fix #1): a field the machine
                # verification policy excludes — or any candidate value longer
                # than the prose budget — is not a machine-verify candidate at
                # all. Single-chain / ambiguity / disagreement on such fields
                # is therefore never an exception; they policy-skip first.
                if not machine_verify_allowed(field) or any(
                        len(value) > MACHINE_VERIFY_MAX_LEN
                        for value in list(ocr_values) + list(vision_values)):
                    policy_skipped.append(field)
                    continue
                if len(ocr_values) > 1 or len(vision_values) > 1:
                    ambiguous[field] = {'ocr': sorted(ocr_values), 'vision': sorted(vision_values)}
                    continue
                ocr_value = next(iter(ocr_values), None)
                vision_value = next(iter(vision_values), None)
                if ocr_value is None or vision_value is None:
                    single_chain.append(field)  # single-chain fields are never auto-verified
                    continue
                if ocr_value != vision_value:
                    _mark_dispute(s.db, field, ocr_values[ocr_value], vision_values[vision_value],
                                  ocr_value, vision_value, original)
                    disputed[field] = {'ocr': ocr_value, 'vision': vision_value}
                    continue
                outcome = _try_machine_verify(s, material_id, type_code, field, ocr_value,
                                              ocr_values[ocr_value], vision_values[vision_value],
                                              original, vision_receipt['run_id'], identity)
                if outcome == 'verified':
                    verified[field] = ocr_value
                elif outcome == 'conflict':
                    existing = _fact_value(s.db, material_id, type_code, field)
                    _mark_dispute(s.db, field, ocr_values[ocr_value], vision_values[vision_value],
                                  ocr_value, vision_value, original, human_fact=True,
                                  existing_human_value=existing)
                    disputed[field] = {'ocr': ocr_value, 'vision': vision_value, 'existing_human_fact': True}
                    human_conflicts.append(field)
                elif outcome == 'validator_failed':
                    validator_failed.append(field)
                else:
                    policy_skipped.append(field)
            if verified:
                status = s.db.execute('SELECT verification_status FROM material WHERE material_id=?',
                                      (material_id,)).fetchone()[0]
                if status == 'unverified':
                    s.db.execute("UPDATE material SET verification_status='ai_extracted' WHERE material_id=?", (material_id,))
    receipt = dict(material_id=material_id, vision_run_id=vision_receipt['run_id'],
                   vision_status=vision_receipt['status'], verified=verified, disputed=disputed,
                   ambiguous=ambiguous, single_chain=sorted(single_chain),
                   validator_failed=sorted(validator_failed),
                   human_conflicts=sorted(human_conflicts),
                   policy_skipped=sorted(policy_skipped),
                   original_file_id=original['file_id'], original_sha256=original['sha256'])
    _maybe_auto_title(config, material_id)
    with Store(config) as s:
        with s.db:
            audit(s.db, actor, 'material.dual_engine', 'material', material_id, receipt)
    return receipt


def _group(rows):
    grouped = {}
    for row in rows:
        grouped.setdefault(row['field_name'], {}).setdefault(row['normalized_value'], []).append(row)
    return grouped


def _try_machine_verify(s, material_id, type_code, field, value, ocr_rows, vision_rows,
                        original, vision_run_id, identity):
    """Apply every hard condition (spec #15). Outcomes: verified | conflict |
    validator_failed | policy_skipped (policy exclusions and fields a human
    already confirmed at exactly this value — neither is an exception)."""
    if not machine_verify_allowed(field) or len(value) > MACHINE_VERIFY_MAX_LEN:
        return 'policy_skipped'
    if not machine_validator(field, value):
        return 'validator_failed'
    existing = _fact_value(s.db, material_id, type_code, field)
    origin = fact_origin(s.db, material_id, field)
    if origin == 'human':
        if existing is not None and str(existing) != value:
            return 'conflict'  # human fact wins; becomes an exception
        return 'policy_skipped'  # human already confirmed exactly this value
    occurred = now()
    chain = json.dumps({'method': 'dual_engine_consensus', 'ocr_run': ocr_rows[0]['run_id'],
                        'vision_run': vision_run_id, 'original_sha256': original['sha256'],
                        'provider_identity': identity, 'validator': 'PASS'}, ensure_ascii=False)
    for row in list(ocr_rows) + list(vision_rows):
        s.db.execute("""UPDATE extracted_fact SET verification='machine_verified',verified_at=?,verification_json=?
            WHERE extracted_id=?""", (occurred, chain, row['extracted_id']))
    s.machine = dict(write=1, identity=identity, field=field, material=material_id,
                     evidence=original['file_id'], sha256=original['sha256'],
                     reason='dual_engine_consensus: independent OCR and vision chains agree, validator PASS',
                     time=occurred)
    try:
        _machine_fact(s, material_id, field, value, type_code)
    finally:
        s.machine = None
    return 'verified'


def _machine_fact(s, material_id, field, value, type_code):
    table, column, kind = field_spec(field, type_code)
    if field in ORG_FIELDS:
        org = s.db.execute('SELECT subject_org_id FROM material WHERE material_id=?', (material_id,)).fetchone()
        where, params = 'org_id=?', [org[0] if org else None]
    elif field in GRADE_FIELDS:
        where, params = 'material_id=? AND activity=?', [material_id, GRADE_FIELDS[field]]
        table = 'power_license_item'
    else:
        where, params = 'material_id=?', [material_id]
    if table == 'audit_report_detail' and field == 'fiscal_year' and not s.db.execute(
            'SELECT 1 FROM audit_report_detail WHERE material_id=?', (material_id,)).fetchone():
        s.db.execute('INSERT INTO audit_report_detail(material_id,fiscal_year) VALUES(?,?)', (material_id, value))
        return
    stored = '' if field == 'enterprise_name' and value is None else 0 if field == 'long_term_valid' and value is None else value
    if field == 'enterprise_name':
        s.db.execute(f'UPDATE org SET name=?,name_norm=? WHERE {where}', [stored, normalize(stored).replace(' ', ''), *params])
    else:
        s.db.execute(f'UPDATE {table} SET {column}=? WHERE {where}', [stored, *params])


def _mark_dispute(db, field, ocr_rows, vision_rows, ocr_value, vision_value, original,
                  human_fact=False, existing_human_value=None):
    detail = {'kind': 'human_fact_conflict' if human_fact else 'field_conflict',
              'ocr_value': ocr_value, 'vision_value': vision_value,
              'original_sha256': original['sha256']}
    if human_fact:
        # The real conflict: the authoritative human value versus the new
        # dual-engine consensus (OCR == Vision here). Both come from ledger /
        # fact-column state, never guessed.
        detail['existing_human_value'] = '' if existing_human_value is None else str(existing_human_value)
        detail['machine_consensus_value'] = ocr_value
    detail = json.dumps(detail, ensure_ascii=False)
    for row in list(ocr_rows) + list(vision_rows):
        db.execute("""UPDATE extracted_fact SET verification='disputed',verified_at=?,verification_json=?
            WHERE extracted_id=?""", (now(), detail, row['extracted_id']))


def disputes_for(config, material_id):
    """Open field conflicts for the exception UX (current original only)."""
    with Store(config, readonly=True) as s:
        original = s.db.execute("""SELECT file_id FROM material_file
            WHERE material_id=? AND role='original' AND is_current=1""", (material_id,)).fetchone()
        if original is None:
            return []
        rows = s.db.execute("""SELECT e.extracted_id,e.field_name,e.normalized_value,e.verification_json,
                r.extractor,e.page_no,e.evidence_span
            FROM extracted_fact e JOIN extraction_run r USING(run_id)
            WHERE e.material_id=? AND r.source_file_id=? AND e.verification='disputed'
            ORDER BY e.field_name,e.extracted_id""", (material_id, original['file_id'])).fetchall()
    fields = {}
    for row in rows:
        entry = fields.setdefault(row['field_name'], {'field': row['field_name'], 'sides': [],
                                                      'detail': json.loads(row['verification_json'] or '{}')})
        side = 'vision' if row['extractor'].startswith('vision:') else 'ocr'
        if not any(x['side'] == side for x in entry['sides']):
            try:
                span = json.loads(row['evidence_span'] or '{}')
                if not isinstance(span, dict):
                    span = {}
            except ValueError:
                span = {}  # import-path spans are raw OCR text, not JSON
            entry['sides'].append({'side': side, 'value': row['normalized_value'],
                                   'extracted_id': row['extracted_id'], 'page': row['page_no'],
                                   'evidence_file_id': span.get('original_file_id')})
    return [fields[key] for key in sorted(fields)]


def resolve_exception(config, payload):
    """Exception resolution: the click IS the audit event. No actor/note input
    (spec #23). choice: 'ocr' | 'vision' | 'manual'."""
    material_id = payload['material_id']
    field = payload['field']
    choice = payload['choice']
    original = current_original(config, material_id)
    # A human picking a side on a human-fact conflict is an explicit human
    # replacement of a previously verified fact, not an ordinary engine pick.
    with Store(config, readonly=True) as s:
        dispute_row = s.db.execute("""SELECT e.verification_json FROM extracted_fact e
            JOIN extraction_run r USING(run_id)
            WHERE e.material_id=? AND e.field_name=? AND e.verification='disputed'
            ORDER BY e.extracted_id LIMIT 1""", (material_id, field)).fetchone()
    dispute_kind = None
    if dispute_row is not None:
        try:
            dispute_kind = json.loads(dispute_row[0] or '{}').get('kind')
        except ValueError:
            dispute_kind = None
    if choice == 'manual':
        value = payload.get('value')
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError('manual resolution requires a value')
        with Store(config) as s:
            s.verify(material_id, field, value, actor='local_webui',
                     reason='human_corrected_conflict', evidence_file_id=original['file_id'])
    else:
        prefix = 'vision:' if choice == 'vision' else 'umi-ocr'
        with Store(config, readonly=True) as s:
            row = s.db.execute("""SELECT e.extracted_id,e.normalized_value FROM extracted_fact e
                JOIN extraction_run r USING(run_id)
                WHERE e.material_id=? AND e.field_name=? AND r.source_file_id=?
                  AND e.review_status='pending' AND r.extractor LIKE ?
                ORDER BY e.extracted_id DESC LIMIT 1""",
                (material_id, field, original['file_id'], prefix + '%')).fetchone()
        if row is None:
            raise ValueError('no pending candidate for the selected engine')
        reason = ('human_replaced_verified_fact_with_machine_consensus'
                  if dispute_kind == 'human_fact_conflict'
                  else 'human_selected_' + choice + '_candidate')
        with Store(config) as s:
            s.verify(material_id, field, row['normalized_value'], actor='local_webui',
                     reason=reason, evidence_file_id=original['file_id'], candidate_id=row['extracted_id'])
    with Store(config) as s:
        with s.db:
            s.db.execute("""UPDATE extracted_fact SET verification=NULL,verified_at=NULL,verification_json=NULL
                WHERE material_id=? AND field_name=? AND verification='disputed'""", (material_id, field))
            audit(s.db, 'local_webui', 'material.exception_resolved', 'material', material_id,
                  dict(field=field, choice=choice))
    return {'material_id': material_id, 'field': field, 'resolved': choice}


def reclassify(config, payload):
    """Exception-only material reclassification. v0.7 supports exactly one
    transition: 'other' -> any typed category, and only while the material has
    no human verification events and no project references."""
    material_id = payload['material_id']
    subtype = payload['subtype']
    if subtype not in ALLOWED or subtype == 'other':
        raise ValueError('unsupported target type')
    with Store(config) as s:
        with s.db:
            s.db.execute('BEGIN IMMEDIATE')
            m = s.db.execute("""SELECT m.material_type_id,t.code,m.subject_org_id FROM material m
                JOIN material_type t ON t.type_id=m.material_type_id WHERE material_id=?""",
                (material_id,)).fetchone()
            if m is None:
                raise ValueError('material not found')
            if m['code'] != 'other':
                raise ValueError('v0.7 reclassification supports "other" materials only')
            if s.db.execute("""SELECT 1 FROM verification_event WHERE material_id=?
                    AND verification_method IN ('manual_original','manual_system','external_lookup') LIMIT 1""",
                    (material_id,)).fetchone():
                raise ValueError('material already carries human-verified facts')
            if s.db.execute('SELECT 1 FROM project_material WHERE material_id=? LIMIT 1', (material_id,)).fetchone():
                raise ValueError('material is referenced by a project')
            type_id = s.db.execute('SELECT type_id FROM material_type WHERE code=?', (subtype,)).fetchone()
            if type_id is None:
                raise ValueError('unknown target type')
            s.review = dict(actor='local_webui', field='reclassify', evidence=None, candidate=None,
                            reason='human_resolved_classification_exception', time=now(),
                            material=material_id, sha256=None, action='reclassify')
            s.db.execute('UPDATE material SET material_type_id=? WHERE material_id=?', (type_id[0], material_id))
            if subtype in CERTIFICATES:
                kind = CERTIFICATES[subtype]
                existing = s.db.execute('SELECT certificate_kind FROM certificate_detail WHERE material_id=?',
                                        (material_id,)).fetchone()
                if existing is None:
                    s.db.execute('INSERT INTO certificate_detail(material_id,certificate_kind) VALUES(?,?)',
                                 (material_id, kind))
                    if subtype == 'power_facility_license':
                        s.db.executemany('INSERT INTO power_license_item(material_id,activity) VALUES(?,?)',
                                         [(material_id, a) for a in ('install', 'repair', 'test')])
                elif existing[0] != kind:
                    raise ValueError('existing certificate detail conflicts with target type')
            elif subtype in PERFORMANCE_TYPES:
                if not s.db.execute('SELECT 1 FROM performance_detail WHERE material_id=?', (material_id,)).fetchone():
                    s.db.execute('INSERT INTO performance_detail(material_id) VALUES(?)', (material_id,))
            elif subtype in PERSON_TYPES:
                if not s.db.execute('SELECT 1 FROM person_certificate_detail WHERE material_id=?', (material_id,)).fetchone():
                    s.db.execute('INSERT INTO person_certificate_detail(material_id,certificate_kind) VALUES(?,?)',
                                 (material_id, PERSON_TYPES[subtype]))
            elif subtype in TYPE_TABLE:
                table = TYPE_TABLE[subtype]
                if not s.db.execute(f'SELECT 1 FROM {table} WHERE material_id=?', (material_id,)).fetchone():
                    s.db.execute(f'INSERT INTO {table}(material_id) VALUES(?)', (material_id,))
            if subtype == 'business_license' and not m['subject_org_id']:
                import uuid
                org_id = s.db.execute("INSERT INTO org(org_code,name,name_norm,status,created_at,updated_at) VALUES(?,'','','unknown',?,?) RETURNING org_id",
                                      ('ORG-' + uuid.uuid4().hex, now(), now())).fetchone()[0]
                s.db.execute('UPDATE material SET subject_org_id=? WHERE material_id=?', (org_id, material_id))
            s.review = None
            audit(s.db, 'local_webui', 'material.reclassified', 'material', material_id,
                  dict(subtype=subtype, reason='human_resolved_classification_exception'))
    return {'material_id': material_id, 'material_type': subtype}
