"""Material candidate discovery over Project Document text (v0.8 phase 1).

Deterministic, single-chain (OCR text) detection. A candidate is evidence,
never a Material: nothing here writes the guarded Material fact layer. The
detection vocabulary is reused verbatim from the zero-fill classifier so
project discovery and material import can never disagree about what a
document header means.

Runs on the manager dispatcher after a document run is published. Input is
the current-run unit text (native + embedded-image OCR); output is a full
replace of the document's candidate rows plus, for deterministic exact
matches, an audited project->material reference.
"""
import json
import re
import uuid

from .classify import ocr_classify
from .common import now
from .fields import org_norm
from .workflow import audit

USCC_CHARS = set('0123456789ABCDEFGHJKLMNPQRTUWXY')
_USCC_PATTERN = re.compile(r'[0-9A-Z][0-9A-Z\s]{16}[0-9A-Z]')
_ID_PATTERN = re.compile(
    r'(?:统一社会信用代码|证书编号|许可证编号|许可证号|注册号|执照号|资质证书编号|证书号)'
    r'[:：]?\s*([0-9A-Za-z（）()\[\]【】\-—·、\u4e00-\u9fff]{6,40})')
_ORG_NAME_PATTERN = re.compile(
    r'(?:名\s*称|单位名称|企业名称)[:：]?\s*([^\n：:，,。;；\s]{4,40}(?:公司|厂|院|中心|所|集团|合作社|事务所|银行))')
_ORG_NAME_FALLBACK = re.compile(r'([^\n：:，,。;；\s]{6,40}(?:有限公司|有限责任公司|股份有限公司|集团有限公司))')
_PERSON_NAME_PATTERN = re.compile(r'姓\s*名[:：]?\s*([\u4e00-\u9fff]{2,4})')
_PERSON_ID_PATTERN = re.compile(r'(?:公民身份号码|身份证号|身份证号码|身份证)[:：]?\s*(\d{17}[\dXx])')
_LEGAL_REP_PATTERN = re.compile(r'法定代表人[:：]?\s*([\u4e00-\u9fff]{2,4})')
PERSON_TYPES = {'person_id_card'}

# Lifecycle first rules: content-driven, never title-only. Anything the rules
# cannot decide stays 'unknown' for human triage instead of being guessed.
REUSABLE_TYPES = frozenset({
    'business_license', 'construction_qual', 'safety_prod_license', 'power_facility_license',
    'iso_cert', 'patent', 'software_copyright', 'audit_report', 'financial_statement',
    'tax_credit', 'tax_payment_cert', 'award_notice', 'honor_cert',
    'person_id_card', 'person_title_cert', 'person_registered_cert', 'person_special_op',
    'person_safety_cert', 'person_education', 'person_social_security',
})
TRANSIENT_MARKERS = ('信用中国', '失信被执行人', '查询时间', '生成时间', '报告编号', '查询结果', '严重违法')
EVIDENCE_LIMIT = 400


def extract_usccs(text):
    codes = {re.sub(r'\s+', '', c) for c in _USCC_PATTERN.findall(text or '')}
    return sorted(c for c in codes if len(c) == 18 and set(c) <= USCC_CHARS)


def _first(pattern, text):
    match = pattern.search(text or '')
    return match.group(1).strip() if match else None


def subject_hints(candidate_type, text):
    """WHO: deterministic identifiers first, normalized names afterwards."""
    hints = dict(subject_uscc=None, subject_name=None, subject_person=None,
                 subject_id_masked=None, legal_representative=None, identifier=None)
    if candidate_type in PERSON_TYPES:
        hints['subject_person'] = _first(_PERSON_NAME_PATTERN, text)
        person_id = _first(_PERSON_ID_PATTERN, text)
        if person_id:
            hints['subject_id_masked'] = person_id[:6] + '********' + person_id[-4:]
    else:
        codes = extract_usccs(text)
        hints['subject_uscc'] = codes[0] if codes else None
        hints['subject_name'] = _first(_ORG_NAME_PATTERN, text) or _first(_ORG_NAME_FALLBACK, text)
        hints['legal_representative'] = _first(_LEGAL_REP_PATTERN, text)
        identifier = _first(_ID_PATTERN, text)
        hints['identifier'] = None if identifier == hints['subject_uscc'] else identifier
        if candidate_type == 'business_license' and hints['subject_uscc']:
            hints['identifier'] = hints['subject_uscc']
    return hints


def lifecycle_for(candidate_type, text):
    """LIFECYCLE: reusable by material type, transient only with point-in-time
    evidence markers in the content itself (titles lie — the production
    transient-purge campaigns proved that)."""
    if candidate_type in REUSABLE_TYPES:
        return 'reusable'
    if candidate_type == 'credit_report' and any(m in (text or '') for m in TRANSIENT_MARKERS):
        return 'transient'
    return 'unknown'


def _evidence(text):
    visible = (text or '').strip().replace('\r', '')
    return (visible[:EVIDENCE_LIMIT] + '…') if len(visible) > EVIDENCE_LIMIT else visible


def match_library(db, candidate_type, hints):
    """High-reliability matching only: identifier-exact or nothing. Fuzzy or
    vector similarity is deliberately out of scope for the first chain."""
    uscc, identifier = hints.get('subject_uscc'), hints.get('identifier')
    if uscc:
        rows = [r[0] for r in db.execute(
            """SELECT m.material_id FROM material m JOIN org o ON o.org_id=m.subject_org_id
               JOIN material_type t ON t.type_id=m.material_type_id
               WHERE o.unified_social_credit_code=? AND t.code=? AND m.business_status<>'archived'""",
            (uscc, candidate_type))]
        if len(rows) == 1:
            return 'exact_match', rows[0]
        if len(rows) > 1:
            return 'possible_match', None  # ambiguous identity is an exception, never a guess
    if identifier:
        rows = [r[0] for r in db.execute(
            """SELECT m.material_id FROM material m JOIN certificate_detail c USING(material_id)
               JOIN material_type t ON t.type_id=m.material_type_id
               WHERE c.certificate_no=? AND t.code=? AND m.business_status<>'archived'""",
            (identifier, candidate_type))]
        if len(rows) == 1:
            return 'exact_match', rows[0]
    name = hints.get('subject_name')
    if name:
        rows = [r[0] for r in db.execute(
            """SELECT m.material_id FROM material m JOIN org o ON o.org_id=m.subject_org_id
               JOIN material_type t ON t.type_id=m.material_type_id
               WHERE o.name_norm=? AND t.code=? AND m.business_status<>'archived' LIMIT 6""",
            (org_norm(name), candidate_type))]
        if len(rows) >= 1:
            return 'possible_match', None
    return 'new_candidate', None


def detect(units):
    """Yield candidate dicts from current-run units. Image OCR text stands on
    its own (a scanned certificate is the document); native text additionally
    needs an identifier in the same unit so a table of contents or a bid form
    that merely mentions 营业执照 is never a candidate. Images sort first so
    the stronger evidence wins any in-document identity duplicate."""
    ordered = sorted(units, key=lambda u: (u['unit_type'] != 'embedded_image', u['unit_index']))
    seen, found = set(), []
    for unit in ordered:
        text = unit['text_raw'] or ''
        code, basis = ocr_classify(text)
        if code is None:
            continue
        hints = subject_hints(code, text)
        is_image = unit['unit_type'] == 'embedded_image'
        if not is_image and not (hints['subject_uscc'] or hints['identifier']):
            continue
        identity = (code, hints['subject_uscc'], hints['subject_person'],
                    hints['subject_id_masked'], hints['identifier'])
        if identity in seen:
            continue
        seen.add(identity)
        confidence = 'high' if (hints['subject_uscc'] or hints['identifier'] or
                                (is_image and hints['subject_person'])) else 'medium'
        found.append(dict(unit=unit, candidate_type=code, basis=basis, confidence=confidence,
                          evidence=_evidence(text), hints=hints,
                          lifecycle=lifecycle_for(code, text)))
    return found


def _link_exact(s, pid, material_id, document_id, candidate_id):
    """Create the project reference for a deterministic exact match (spec #17).
    Idempotent: an existing active reference is left untouched."""
    row = s.db.execute('SELECT reference_id,status FROM project_material WHERE project_id=? AND material_id=?',
                       (pid, material_id)).fetchone()
    if row is not None and row['status'] == 'active':
        return row['reference_id']
    at = now()
    if row is not None:
        s.db.execute("UPDATE project_material SET status='active',added_at=?,added_by=?,note=NULL WHERE reference_id=?",
                     (at, 'project-intelligence', row['reference_id']))
        reference_id = row['reference_id']
    else:
        reference_id = s.db.execute(
            """INSERT INTO project_material(project_id,material_id,status,added_at,added_by,note)
               VALUES(?,?,'active',?,'project-intelligence',?) RETURNING reference_id""",
            (pid, material_id, at,
             json.dumps(dict(method='project_document_exact_match', document_id=document_id,
                             candidate_id=candidate_id), ensure_ascii=False))).fetchone()[0]
    s.db.execute('UPDATE project SET updated_at=? WHERE project_id=?', (at, pid))
    return reference_id


def discover(config, store, document_id, actor='project-intelligence'):
    """Full replace of the document's candidates. Caller holds operation.lock
    and supplies an open writable Store with document_write enabled."""
    s = store
    document = s.db.execute('SELECT d.*,f.project_id FROM project_document d '
                            'JOIN project_file f USING(project_file_id) WHERE document_id=?',
                            (document_id,)).fetchone()
    if document is None:
        raise ValueError('document not found')
    units = [dict(r) for r in s.db.execute(
        'SELECT * FROM project_document_unit WHERE document_id=? AND run_id=? ORDER BY unit_index',
        (document_id, document['current_run_id']))]
    images = {r['image_id']: dict(r) for r in s.db.execute(
        'SELECT * FROM project_document_image WHERE document_id=?', (document_id,))}
    detected = detect(units)
    at = now()
    s.db.execute('DELETE FROM project_material_candidate WHERE document_id=?', (document_id,))
    counts = dict(candidates=0, exact_matches=0, possible_matches=0, new_candidates=0, linked=0)
    with s.db:
        for item in detected:
            unit = item['unit']
            image = images.get(unit['image_id'])
            hints = item['hints']
            match_status, matched = match_library(s.db, item['candidate_type'], hints)
            candidate_id = uuid.uuid4().hex
            s.db.execute(
                """INSERT INTO project_material_candidate(candidate_id,project_id,document_id,source_kind,
                   source_unit_id,source_image_id,candidate_type,basis,confidence,subject_name,subject_uscc,
                   subject_person,subject_id_masked,identifier,evidence_text,source_sha256,lifecycle,
                   match_status,matched_material_id,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (candidate_id, document['project_id'], document_id,
                 'embedded_image' if unit['unit_type'] == 'embedded_image' else 'text_unit',
                 unit['unit_id'], unit['image_id'], item['candidate_type'], item['basis'], item['confidence'],
                 hints['subject_name'], hints['subject_uscc'], hints['subject_person'], hints['subject_id_masked'],
                 hints['identifier'], item['evidence'], document['source_sha256'], item['lifecycle'],
                 match_status, matched, 'active', at, at))
            counts['candidates'] += 1
            counts[{'exact_match': 'exact_matches', 'possible_match': 'possible_matches',
                    'new_candidate': 'new_candidates'}.get(match_status, 'new_candidates')] += 1
            if match_status == 'exact_match' and matched:
                s.project_write = dict(actor=actor, action='project.material.autolink')
                try:
                    reference = _link_exact(s, document['project_id'], matched, document_id, candidate_id)
                    audit(s.db, actor, 'project.material.autolink', 'project_material', reference,
                          dict(project_id=document['project_id'], material_id=matched,
                               method='project_document_exact_match', document_id=document_id,
                               candidate_id=candidate_id, candidate_type=item['candidate_type'],
                               basis=hints['subject_uscc'] or hints['identifier']))
                    counts['linked'] += 1
                finally:
                    s.project_write = None
        summary = json.loads(document['summary_json'] or '{}')
        summary['discovery'] = counts
        s.db.execute('UPDATE project_document SET summary_json=?,updated_at=? WHERE document_id=?',
                     (json.dumps(summary, ensure_ascii=False), at, document_id))
        audit(s.db, actor, 'project.material.discover', 'project', document['project_id'],
              dict(project_id=document['project_id'], document_id=document_id, **counts))
    return dict(document_id=document_id, **counts)
