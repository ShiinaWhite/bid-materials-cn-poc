"""Historical human provenance plus machine comparison, never a new human review.

Uses the existing append-only audit ledger. Does not change business facts,
verification_event, verification guards, material status or eligibility.
"""
import json
from .common import FileLock, atomic_write, digest, now, safe_path
from .store import Store
from .fields import allowed_field, validate_value


def inherited_facts(config, db, material_id, material_type, files):
    current = next((f for f in files if f['role']=='original' and f['is_current'] and f['integrity_ok']), None)
    if current is None:return {}
    result={}
    for row in db.execute("SELECT log_id,at,actor,detail_json FROM audit_log WHERE action='fact.inherit_consistency' AND entity_id=? ORDER BY log_id",(material_id,)):
        d=json.loads(row['detail_json']);field=d['field']
        if not allowed_field(material_type,field) or d['evidence_file_id']!=current['file_id'] or d['evidence_sha256']!=current['sha256']:continue
        source=db.execute('SELECT * FROM verification_event WHERE event_id=?',(d['historical_event_id'],)).fetchone()
        latest=db.execute('SELECT event_id FROM verification_event WHERE material_id=? AND field_name=? ORDER BY event_id DESC LIMIT 1',(material_id,field)).fetchone()
        if source is None or latest is None or latest[0]!=source['event_id']:continue
        if source['material_id']!=material_id or source['field_name']!=field or source['verification_method']!='manual_original' or source['action'] not in ('set','verify','correct'):continue
        if source['evidence_file_id']!=d['historical_evidence_file_id'] or source['new_value']!=str(d['value']):continue
        old=next((f for f in files if f['file_id']==source['evidence_file_id'] and f['role']=='original' and f['integrity_ok']),None)
        if old is None or old['sha256']!=d['historical_evidence_sha256']:continue
        proof=safe_path(config.repository_root,d['comparison_relative_path'])
        if not proof.is_file() or digest(proof)!=d['comparison_sha256']:raise ValueError('machine comparison evidence checksum mismatch')
        checked=json.loads(proof.read_text(encoding='utf-8'))
        f=checked['fields'].get(field,{})
        if checked['material_id']!=material_id or checked['new_original_file_id']!=current['file_id'] or checked['new_original_sha256']!=current['sha256']:raise ValueError('machine comparison evidence binding mismatch')
        if f.get('status')!='consistent' or f.get('historical_event_id')!=source['event_id'] or str(f.get('value'))!=source['new_value']:raise ValueError('machine comparison does not support inherited field')
        result[field]=dict(value=d['value'],basis='historical_manual_with_machine_consistency',
            human_verified_current_original=False,historical_event_id=source['event_id'],
            historical_verified_by=source['verified_by'],historical_verified_at=source['occurred_at'],
            historical_evidence_file_id=old['file_id'],historical_evidence_sha256=old['sha256'],
            evidence_file_id=current['file_id'],evidence_sha256=current['sha256'],
            consistency_checked_by=row['actor'],consistency_checked_at=row['at'],
            audit_log_id=row['log_id'],comparison_relative_path=d['comparison_relative_path'],comparison_sha256=d['comparison_sha256'])
    return result


def record_comparison(config, relative_path, proof):
    """Offline serialized append only; proof is retained in batch backups."""
    mid=proof['material_id'];target=safe_path(config.repository_root,relative_path)
    with FileLock(config.repository_root/'db/operation.lock'):
        if config.maintenance_file.exists():raise ValueError('another maintenance operation requires recovery')
        atomic_write(config.maintenance_file,json.dumps(dict(action='machine_comparison',at=now())))
        try:
            if target.exists():
                if json.loads(target.read_text(encoding='utf-8'))!=proof:raise ValueError('immutable comparison already exists with different content')
            else:atomic_write(target,json.dumps(proof,ensure_ascii=False,indent=2),immutable=True)
            with Store(config) as s:
                with s.db:
                    s.db.execute('BEGIN IMMEDIATE')
                    m=s.db.execute('SELECT t.code FROM material m JOIN material_type t ON t.type_id=m.material_type_id WHERE material_id=?',(mid,)).fetchone()
                    current=s.db.execute("SELECT * FROM material_file WHERE material_id=? AND role='original' AND is_current=1",(mid,)).fetchone()
                    if current['file_id']!=proof['new_original_file_id'] or current['sha256']!=proof['new_original_sha256'] or digest(safe_path(config.repository_root,current['relative_path']))!=current['sha256']:raise ValueError('current original changed before comparison commit')
                    for field,item in proof['fields'].items():
                        if item['status']!='consistent':continue
                        if not allowed_field(m['code'],field):raise ValueError('unsupported inherited field')
                        validate_value(field,item['value'])
                        source=s.db.execute('SELECT * FROM verification_event WHERE material_id=? AND field_name=? ORDER BY event_id DESC LIMIT 1',(mid,field)).fetchone()
                        if source is None or source['event_id']!=item['historical_event_id'] or source['verification_method']!='manual_original' or source['action'] not in ('set','verify','correct') or source['new_value']!=str(item['value']):raise ValueError('historical manual fact changed or withdrawn')
                        old=s.db.execute("SELECT * FROM material_file WHERE file_id=? AND material_id=? AND role='original'",(source['evidence_file_id'],mid)).fetchone()
                        if old is None or old['sha256']!=proof['old_original_sha256'] or old['file_id']!=proof['old_original_file_id'] or digest(safe_path(config.repository_root,old['relative_path']))!=old['sha256']:raise ValueError('historical original binding mismatch')
                        existing=s.db.execute("SELECT 1 FROM audit_log WHERE action='fact.inherit_consistency' AND entity_id=? AND json_extract(detail_json,'$.field')=? AND json_extract(detail_json,'$.evidence_file_id')=? AND json_extract(detail_json,'$.historical_event_id')=?",(mid,field,current['file_id'],source['event_id'])).fetchone()
                        if existing:continue
                        detail=dict(material_id=mid,field=field,value=item['value'],historical_event_id=source['event_id'],
                            historical_evidence_file_id=old['file_id'],historical_evidence_sha256=old['sha256'],
                            historical_verified_by=source['verified_by'],historical_verified_at=source['occurred_at'],
                            evidence_file_id=current['file_id'],evidence_sha256=current['sha256'],
                            comparison_relative_path=relative_path,comparison_sha256=digest(target),
                            basis='historical_manual_with_machine_consistency',human_rereview_performed=False,
                            human_review_claim='历史人工核验沿用；本次仅机器一致性复核及证据关联，不是用户再次逐字段确认。')
                        s.db.execute("INSERT INTO audit_log(at,actor,action,entity_type,entity_id,detail_json,note) VALUES(?,'offline-machine-consistency','fact.inherit_consistency','material',?,?,?)",(now(),mid,json.dumps(detail,ensure_ascii=False),detail['human_review_claim']))
            generation=config.state_dir/'generation.json'
            atomic_write(generation,json.dumps((json.loads(generation.read_text()) if generation.exists() else 0)+1))
            config.log('fact.machine_consistency',material_id=mid,comparison_relative_path=relative_path,comparison_sha256=digest(target))
        finally:config.maintenance_file.unlink(missing_ok=True)


def append_candidates(config, material_id, values):
    """Only OCR-derived pending candidates; never writes a formal fact."""
    with FileLock(config.repository_root/'db/operation.lock'), Store(config) as s:
        if config.maintenance_file.exists():raise ValueError('maintenance pending')
        with s.db:
            s.db.execute('BEGIN IMMEDIATE')
            source=s.db.execute("SELECT f.file_id,t.code FROM material_file f JOIN material m USING(material_id) JOIN material_type t ON t.type_id=m.material_type_id WHERE f.material_id=? AND f.role='original' AND f.is_current=1",(material_id,)).fetchone()
            run=s.db.execute("SELECT run_id FROM extraction_run WHERE material_id=? AND source_file_id=? ORDER BY run_id DESC LIMIT 1",(material_id,source['file_id'])).fetchone()
            for v in values:
                if not allowed_field(source['code'],v['field']):raise ValueError('unsupported candidate field')
                validate_value(v['field'],v['value'])
                if s.db.execute('SELECT 1 FROM extracted_fact WHERE run_id=? AND field_name=? AND normalized_value=?',(run[0],v['field'],str(v['value']))).fetchone():continue
                s.db.execute("INSERT INTO extracted_fact(run_id,material_id,field_name,value_type,raw_value,normalized_value,evidence_span) VALUES(?,?,?,?,?,?,?)",(run[0],material_id,v['field'],v.get('value_type','text'),v['raw'],str(v['value']),v['raw']))
            s.db.execute("INSERT INTO audit_log(at,actor,action,entity_type,entity_id,detail_json) VALUES(?,'offline-ocr-rule','candidate.append','material',?,?)",(now(),material_id,json.dumps(dict(source_file_id=source['file_id'],fields=[v['field'] for v in values],review_status='pending',human_verified=False),ensure_ascii=False)))
