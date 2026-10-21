"""Append OCR derivatives for the unchanged current original; never a new original."""
import json,uuid
from pathlib import Path
from .common import atomic_write,digest,normalize,now,safe_path
from .store import Store
from .workflow import audit,rebuild_projections
from .phase_ocr import PhaseOcr
from .business import extract_candidates

def reextract(config,record,ocr_factory=None,defer_projection=False):
    keys={'material_id','evidence_file_id','evidence_sha256','expected_search_file_id','operation_id','actor','reason'}
    if not isinstance(record,dict) or set(record)!=keys:raise ValueError('explicit current evidence, search file, actor, reason and operation ID required')
    if any(type(record[k]) is not int or record[k]<1 for k in ('material_id','evidence_file_id','expected_search_file_id')):raise ValueError('positive file/material IDs required')
    if any(not isinstance(record[k],str) or not record[k].strip() for k in ('actor','reason','operation_id')):raise ValueError('actor/reason/operation ID required')
    def current(db):
        f=db.execute("SELECT f.*,m.material_code,t.code FROM material_file f JOIN material m USING(material_id) JOIN material_type t ON t.type_id=m.material_type_id WHERE f.material_id=? AND f.file_id=? AND f.role='original' AND f.is_current=1",(record['material_id'],record['evidence_file_id'])).fetchone()
        if not f or f['sha256']!=record['evidence_sha256'] or digest(safe_path(config.repository_root,f['relative_path']))!=f['sha256']:raise ValueError('current original changed; re-extraction refused')
        return f
    with Store(config) as s:
        original=dict(current(s.db))
        prior=s.db.execute("SELECT detail_json FROM audit_log WHERE action='material.reextract.commit' AND entity_id=? AND json_extract(detail_json,'$.operation_id')=? ORDER BY log_id DESC LIMIT 1",(record['material_id'],record['operation_id'])).fetchone()
        if prior:
            d=json.loads(prior[0])
            if d['record']!=record:raise ValueError('re-extraction operation ID reused with different evidence')
            return d['receipt']
        search=s.db.execute("SELECT file_id FROM material_file WHERE material_id=? AND role='search_text' AND is_current=1",(record['material_id'],)).fetchone()
        if not search or search[0]!=record['expected_search_file_id']:raise ValueError('current derivative changed since repair plan')
        ctx=s.db.execute("SELECT detail_json FROM audit_log WHERE action='material.source_context' AND entity_id=? ORDER BY log_id DESC LIMIT 1",(record['material_id'],)).fetchone()
        context=json.loads(ctx[0]) if ctx else {}
    task=uuid.uuid4().hex;stage=config.state_dir/'staging'/('ocr-'+task);stage.mkdir(parents=True,exist_ok=False)
    source=safe_path(config.repository_root,original['relative_path']);copy=stage/('original'+source.suffix)
    atomic_write(copy,source.read_bytes(),immutable=True)
    if digest(copy)!=original['sha256']:raise ValueError('original changed during OCR staging')
    own=ocr_factory is None;ocr=PhaseOcr(config) if own else ocr_factory(config)
    ocr.source_label=context.get('source_title',original['filename'])
    try:
        ocr.start();result=ocr.document(copy)
    finally:
        if own:ocr.close()
        else:ocr.stop()
    raw=Path(result['raw']).read_text(encoding='utf-8');search_text=Path(result['search']).read_text(encoding='utf-8')
    text_status=result.get('options',{}).get('text_status','complete')
    if text_status not in ('complete','metadata_only') or result.get('options',{}).get('errors') or (text_status=='complete' and not raw.strip()):
        raise ValueError('OCR still unavailable or empty; current derivatives retained: '+json.dumps(result.get('options',{}),ensure_ascii=False))
    if (raw.strip() and search_text!=normalize(raw)) or (not raw.strip() and (normalize(search_text)!=search_text or not search_text.startswith('[METADATA ONLY:'))):raise ValueError('invalid NFKC/empty-text derivative')
    values=list(extract_candidates(raw,original['code'],context))
    target=config.materials/original['material_code']/'.derivations'/task
    target.mkdir(parents=True,exist_ok=False)
    paths={}
    for role,key,name in [('ocr_raw_text','raw','raw.txt'),('ocr_raw_jsonl','jsonl','raw.jsonl'),('search_text','search','search.txt')]:
        p=target/name;atomic_write(p,Path(result[key]).read_bytes(),immutable=True);paths[role]=p
    with Store(config) as s:
        with s.db:
            s.db.execute('BEGIN IMMEDIATE');current(s.db)
            active=s.db.execute("SELECT file_id FROM material_file WHERE material_id=? AND role='search_text' AND is_current=1",(record['material_id'],)).fetchone()
            if not active or active[0]!=record['expected_search_file_id']:raise ValueError('current derivative changed during OCR; no DB switch')
            old=[dict(r) for r in s.db.execute("SELECT file_id,role,sha256,relative_path FROM material_file WHERE material_id=? AND is_current=1 AND role IN ('ocr_raw_text','ocr_raw_jsonl','search_text')",(record['material_id'],))]
            s.db.execute("UPDATE material_file SET is_current=0 WHERE material_id=? AND role IN ('ocr_raw_text','ocr_raw_jsonl','search_text') AND is_current=1",(record['material_id'],))
            files={role:s.register_file(record['material_id'],p,role,original['version_id']) for role,p in paths.items()}
            options=dict(result['options'],text_status=text_status,derived_files=files,derivative_task=task,original_sha256=original['sha256'])
            run=s.db.execute("INSERT INTO extraction_run(material_id,source_file_id,extractor,extractor_version,params_json,run_at,status) VALUES(?,?,'umi-ocr+typed-candidate-rules','2.1.5/v0.4-reextract',?,?,?) RETURNING run_id",(record['material_id'],original['file_id'],json.dumps(options,ensure_ascii=False),now(),'succeeded' if text_status=='complete' else 'partial')).fetchone()[0]
            s.db.execute("UPDATE extracted_fact SET review_status='superseded',review_note='Machine OCR retry for same original; previous candidate retained in history' WHERE material_id=? AND review_status='pending'",(record['material_id'],))
            for field,vt,r,n in values:s.db.execute('INSERT INTO extracted_fact(run_id,material_id,field_name,value_type,raw_value,normalized_value,evidence_span) VALUES(?,?,?,?,?,?,?)',(run,record['material_id'],field,vt,r,n,r[:1000]))
            s.db.execute('UPDATE search_projection SET search_file_id=?,sha256=? WHERE material_id=?',(files['search_text'],digest(paths['search_text']),record['material_id']))
            receipt=dict(material_id=record['material_id'],original_file_id=original['file_id'],original_sha256=original['sha256'],files=files,task=task,run_id=run,text_status=text_status,original_version_unchanged=True)
            audit(s.db,record['actor'],'material.reextract.commit','material',record['material_id'],dict(operation_id=record['operation_id'],record=record,receipt=receipt,previous_derivatives=old,reason=record['reason'],no_human_verification=True))
    if not defer_projection:rebuild_projections(config)
    return receipt
