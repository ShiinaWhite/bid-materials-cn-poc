"""Small offline batch rehearsal: immutable plan, per-item receipts, no fact writes."""
import datetime as dt
import json
from pathlib import Path
import re
import shutil
import uuid
from .common import FileLock,atomic_write,digest,now,safe_path
from .store import Store
from .fields import CERTIFICATES,ORG_FIELDS,GRADE_FIELDS,allowed_field
from .workflow import batch_receipt
from .manager import request

ACTIONS={'new_material','replace_existing','duplicate','defer','manual_review'}
TYPES=set(CERTIFICATES)

def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def write(path,value,immutable=False):atomic_write(path,json.dumps(value,ensure_ascii=False,indent=2),immutable=immutable)

def inventory(config,source_root,limit=50):
    """Immediate children only; metadata/hash/filename hints. No OCR or write."""
    root=Path(source_root).resolve(strict=True)
    if not root.is_dir() or type(limit) is not int or not 1<=limit<=100:raise ValueError('directory and limit 1..100 required')
    paths=sorted(root.iterdir(),key=lambda p:p.name)
    files=[p for p in paths if p.is_file()]
    if len(files)>limit:raise ValueError('inventory exceeds explicit limit; choose a curated directory')
    with Store(config,readonly=True) as s:
        originals=[dict(r) for r in s.db.execute("SELECT f.material_id,f.file_id,f.sha256,f.is_current,t.code AS material_type,c.certificate_no FROM material_file f JOIN material m USING(material_id) JOIN material_type t ON t.type_id=m.material_type_id LEFT JOIN certificate_detail c USING(material_id) WHERE f.role='original'")]
    rows=[]
    for i,p in enumerate(files,1):
        safe_path(root,p.name)  # also refuses reparse targets outside the approved directory
        sha=digest(p);name=p.name
        guess=next((t for word,t in [('营业执照','business_license'),('安全生产','safety_prod_license'),('承装修试','power_facility_license'),('建筑业','construction_qual'),('总承包','construction_qual'),('体系','iso_cert')] if word in name),None)
        exact=[r for r in originals if r['sha256']==sha]
        no_match=re.search(r'\d{5}[QES]\d+R\d+[A-Z]',name)
        potential=[r for r in originals if (no_match and r['certificate_no']==no_match[0]) or (guess and r['material_type']==guess)]
        if exact:action='duplicate';reason='SHA256 matches a registered original (including historical versions); never reactivate history'
        elif '通知书' in name:action='manual_review';reason='notice is not automatically a standalone certification; determine relationship manually'
        elif p.suffix.lower() not in ('.pdf','.png','.jpg','.jpeg'):action='defer';reason='format outside current original reader'
        elif potential:action='manual_review';reason='different SHA; possible related material, human comparison required'
        elif guess:action='new_material';reason='filename-only suggestion; not an identity decision'
        else:action='manual_review';reason='type/identity unknown'
        rows.append(dict(item_id=f'I{i:02}',original_path=str(p),filename=name,extension=p.suffix.lower(),size_bytes=p.stat().st_size,sha256=sha,
            guessed_material_type=guess,possible_already_registered=bool(exact),exact_original_matches=exact,
            possible_new_version=bool(potential and not exact),possible_material_ids=sorted({r['material_id'] for r in potential}),
            filename_certificate_no_hint=no_match[0] if no_match else None,pages=None,content_inspected=False,
            sensitive_information='unknown',sensitivity_basis='metadata-only inventory, no content OCR; never a defer rule',
            suggested_action=action,suggestion_reason=reason))
    return dict(format='jty-inventory-1',created_at=now(),source_root=str(root),recursive=False,limit=limit,ignored_subdirectories=[p.name for p in paths if p.is_dir()],items=rows)

def batch_dir(config,batch_id):
    if not isinstance(batch_id,str) or not re.fullmatch(r'B-[A-Za-z0-9_-]{1,60}',batch_id):raise ValueError('invalid batch ID')
    return config.repository_root/'batches'/batch_id

def context(manifest,decision,row):
    return dict(batch_id=manifest['batch_id'],item_id=row['item_id'],plan_sha256=manifest['plan_sha256'],source_sha256=row['sha256'],expected_current_sha256=decision.get('expected_current_sha256'))

def create(config,inventory_path,plan_path):
    inv=read(inventory_path);plan=read(plan_path)
    if plan.get('inventory_sha256')!=digest(inventory_path) or not plan.get('approved_by') or not plan.get('approved_at') or not plan.get('approval_evidence'):raise ValueError('explicit human-approved plan bound to inventory required')
    inv_rows={r['item_id']:r for r in inv['items']};decisions=plan.get('items',[])
    if not 1<=len(decisions)<=5:raise ValueError('rehearsal batch is limited to 1..5 explicitly selected items')
    if len(decisions)!=len(inv_rows) or {d['item_id'] for d in decisions}!=set(inv_rows):raise ValueError('every inventory item needs exactly one decision')
    for d in decisions:
        action=d.get('action');row=inv_rows[d['item_id']]
        if action not in ACTIONS or not d.get('reason'):raise ValueError('unsupported action or missing reason')
        if action in ('new_material','replace_existing'):
            if d.get('material_type') not in TYPES or not d.get('identity_review') or not d.get('original_confirmed') or not d.get('title'):raise ValueError('supported type and explicit identity/original review required')
            required=set(d.get('required_verified_fields',[]));typ=d['material_type']
            minimum=set(ORG_FIELDS) if typ=='business_license' else {'certificate_no'}
            if typ=='iso_cert':minimum|={'standard','scope'}
            if typ=='power_facility_license':minimum|=set(GRADE_FIELDS)
            if typ=='construction_qual' and not any(f.startswith('qualification:') for f in required):raise ValueError('construction plan must enumerate qualification/grade review fields')
            if not minimum.issubset(required) or any(not allowed_field(typ,f) for f in required):raise ValueError('plan must require supported type-specific key facts')
            if 'valid_until' not in required and not (d.get('review_unknown_fields')==['valid_until'] and d.get('unknown_term_reason')):raise ValueError('expiry must be reviewed or explicitly acknowledged unknown, never silently valid')
            if not isinstance(d.get('mcp_search'),dict) or d['mcp_search'].get('material_type')!=typ:raise ValueError('typed MCP acceptance search required')
            if action=='replace_existing':
                from .query import Query
                m=Query(config).get_material(d.get('material_id'));cur=next(f for f in m['files'] if f['role']=='original' and f['is_current'])
                if m['material_type']!=d['material_type'] or cur['sha256']!=d.get('expected_current_sha256'):raise ValueError('replacement target/type/current hash mismatch')
        if action=='duplicate' and not row['exact_original_matches']:raise ValueError('duplicate requires matching registered original SHA')
    directory=batch_dir(config,plan['batch_id']);directory.mkdir(parents=True,exist_ok=False)
    atomic_write(directory/'inventory.json',Path(inventory_path).read_bytes(),immutable=True)
    atomic_write(directory/'plan.json',Path(plan_path).read_bytes(),immutable=True)
    manifest=dict(format='jty-batch-1',batch_id=plan['batch_id'],source_root=inv['source_root'],created_at=now(),actor=plan['approved_by'],
        planned_items=len(decisions),completed_items=0,failed_items=0,skipped_items=0,backup_before=None,backup_after=None,status='planned',
        inventory_sha256=digest(directory/'inventory.json'),plan_sha256=digest(directory/'plan.json'),items=[dict(item_id=d['item_id'],status='pending',attempts=0,failures=[]) for d in decisions])
    save(directory,manifest,'batch.planned');return manifest

def save(directory,manifest,event,**details):
    manifest['completed_items']=sum(i['status']=='mcp_verified' for i in manifest['items'])
    manifest['failed_items']=sum(i['status']=='failed' for i in manifest['items'])
    manifest['skipped_items']=sum(i['status']=='skipped' for i in manifest['items'])
    manifest['failed_attempts']=sum(len(i['failures']) for i in manifest['items'])
    manifest['updated_at']=now()
    # Publish state after a durable event. Audit in SQLite is the commit authority.
    with (directory/'events.jsonl').open('a',encoding='utf-8') as f:
        f.write(json.dumps(dict(at=now(),event=event,**details),ensure_ascii=False)+'\n');f.flush();__import__('os').fsync(f.fileno())
    write(directory/'manifest.json',manifest)

def load(config,batch_id):
    directory=batch_dir(config,batch_id);m=read(directory/'manifest.json')
    if digest(directory/'inventory.json')!=m['inventory_sha256'] or digest(directory/'plan.json')!=m['plan_sha256']:raise ValueError('inventory/approved plan changed; do not resume')
    return directory,m,read(directory/'inventory.json'),read(directory/'plan.json')

def status(config,batch_id):
    directory,m,_,_=load(config,batch_id)
    if m['status'] in ('completed','partial') and m.get('backup_after'):
        if not backup_complete(m['backup_after']):m=dict(m,status='partial' if m.get('close_error') else 'committing',closure_pending=True)
    return m

def backup_complete(info):
    path=Path(info['backup'])
    return (path/'COMPLETE').is_file() and (path/'COMPLETE').read_text()==digest(path/'manifest.json')

def execute(config,batch_id,transport=request):
    directory=batch_dir(config,batch_id)
    with FileLock(config.state_dir/'batch-execution.lock'),FileLock(directory/'controller.lock'):
        directory,m,inv,plan=load(config,batch_id)
        if m['status'] in ('completed','partial') and m.get('backup_after') and backup_complete(m['backup_after']):raise ValueError('closed batch; create a new plan for later work')
        if not m['backup_before']:
            try:m['backup_before']=transport(config,'backup');save(directory,m,'batch.pre_backup')
            except Exception as error:
                m['status']='failed';save(directory,m,'batch.pre_backup_failed',message=str(error));raise
        rows={r['item_id']:r for r in inv['items']};ds={d['item_id']:d for d in plan['items']}
        for item in m['items']:
            d=ds[item['item_id']];row=rows[item['item_id']]
            if item['status'] in ('skipped','mcp_verified','awaiting_review'):continue
            if d['action'] in ('duplicate','defer','manual_review'):
                try:
                    if d['action']=='duplicate':
                        source=safe_path(inv['source_root'],row['filename'])
                        if digest(source)!=row['sha256']:raise ValueError('duplicate source changed after inventory')
                        with Store(config,readonly=True) as s:
                            if not s.db.execute("SELECT 1 FROM material_file WHERE role='original' AND sha256=?",(row['sha256'],)).fetchone():raise ValueError('duplicate evidence disappeared')
                    item.update(status='skipped',skip_action=d['action']);save(directory,m,'item.skipped',item_id=item['item_id'],action=d['action'])
                except Exception as error:
                    item['status']='failed';item['failures'].append(dict(at=now(),message=str(error)));save(directory,m,'item.skip_validation_failed',item_id=item['item_id'],message=str(error))
                continue
            ctx=context(m,d,row);item['attempts']+=1
            try:
                with Store(config,readonly=True) as s:receipt=batch_receipt(s.db,ctx)
                if receipt is None:
                    m['status']='staging';item['status']='staging';save(directory,m,'item.staging',item_id=item['item_id'])
                    source=safe_path(inv['source_root'],row['filename'])
                    if digest(source)!=row['sha256']:raise ValueError('source changed after approved inventory; re-inventory required')
                    staged=config.state_dir.parent/'batch-staging'/batch_id/item['item_id']/row['filename']
                    if staged.exists():
                        if digest(staged)!=row['sha256']:raise ValueError('staged original changed; investigate')
                    else:atomic_write(staged,source.read_bytes(),immutable=True)
                    if digest(staged)!=row['sha256'] or digest(source)!=row['sha256']:raise ValueError('source changed during copy')
                    item['staged_original']=str(staged);item['status']='committing';m['status']='committing';save(directory,m,'item.committing',item_id=item['item_id'])
                    payload=dict(source=str(staged),actor=m['actor'],subtype=d['material_type'],batch_context=ctx)
                    if d['action']=='replace_existing':payload['material_id']=d['material_id']
                    else:payload.update(kind='certificate',title=d['title'])
                    receipt=transport(config,'batch_import',payload)
                else:
                    transport(config,'index');save(directory,m,'item.reconciled',item_id=item['item_id'])
                item.update(receipt=receipt,status='skipped' if receipt.get('duplicate') else 'awaiting_review')
                if receipt.get('duplicate'):item['skip_action']='duplicate'
                save(directory,m,'item.registered',item_id=item['item_id'],receipt=receipt)
            except Exception as error:
                item['failures'].append(dict(at=now(),message=str(error)))
                # A lost reply is not a failed import. Resolve the transactional token.
                with Store(config,readonly=True) as s:committed=batch_receipt(s.db,ctx)
                if committed is not None:
                    item.update(receipt=committed,status='registered_needs_index')
                    save(directory,m,'item.commit_reply_lost',item_id=item['item_id'])
                else:item['status']='failed'
                if config.maintenance_file.exists():
                    try:transport(config,'index')
                    except Exception as repair_error:
                        save(directory,m,'batch.repair_required',message=str(repair_error));m['status']='partial';save(directory,m,'batch.paused');return m
                save(directory,m,'item.failed_or_reconciled',item_id=item['item_id'],message=str(error))
        m['status']='partial' if any(i['status'] in ('failed','registered_needs_index') for i in m['items']) else 'awaiting_review'
        save(directory,m,'batch.execution_checkpoint');return m

def validate_items(config,batch_id):
    """Read through the actual stdio MCP; never creates verification events."""
    from .rpc import Rpc
    import sys
    directory=batch_dir(config,batch_id)
    with FileLock(config.state_dir/'batch-execution.lock'),FileLock(directory/'controller.lock'):
        directory,m,inv,plan=load(config,batch_id);ds={d['item_id']:d for d in plan['items']}
        if m.get('backup_after') and backup_complete(m['backup_after']):raise ValueError('closed batch is read-only')
        with Rpc([sys.executable,'-B',str(Path(__file__).resolve().parents[1]/'serve.py'),'--config',str(config.path)]) as rpc:
            for item in m['items']:
                if item['status'] not in ('awaiting_review','mcp_verified'):continue
                d=ds[item['item_id']];mid=item['receipt']['material_id'];meta=rpc.tool('get_material',{'material_id':mid})
                original=next(f for f in meta['files'] if f['role']=='original' and f['is_current'])
                required=d.get('required_verified_fields',[])
                if not required or not set(required).issubset(meta['verified_facts']):continue
                if original['sha256']!=item['receipt']['original_sha256'] or not original['integrity_ok']:raise ValueError('MCP original differs from batch receipt')
                if not all(meta['verified_facts'][f]['evidence_file_id']==original['file_id'] and meta['verified_facts'][f]['evidence_sha256']==original['sha256'] for f in required):raise ValueError('review evidence differs from current original')
                # Intake completion and bid eligibility differ. An explicitly
                # reviewed unknown term stays unknown and is never made valid.
                if 'valid_until' not in required and (not d.get('unknown_term_reason') or meta['validity']['status']!='unknown'):continue
                text=rpc.tool('get_material_text',{'material_id':mid});found=rpc.tool('search_materials',d['mcp_search'])
                if not text['complete'] or mid not in [r['material_id'] for r in found['materials']]:raise ValueError('MCP search/text mismatch')
                proof=dict(metadata=meta,text=text,search=found)
                write(directory/(item['item_id']+'-mcp.json'),proof)
                item['status']='mcp_verified';save(directory,m,'item.mcp_verified',item_id=item['item_id'])
        m['status']='awaiting_review' if any(i['status']=='awaiting_review' for i in m['items']) else 'partial' if any(i['status']!='mcp_verified' and i['status']!='skipped' for i in m['items']) else 'committing'
        save(directory,m,'batch.mcp_checkpoint');return m

def close(config,batch_id,accept_partial=False,transport=request):
    directory=batch_dir(config,batch_id)
    with FileLock(config.state_dir/'batch-execution.lock'),FileLock(directory/'controller.lock'):
        directory,m,_,_=load(config,batch_id)
        if m.get('backup_after') and backup_complete(m['backup_after']):return status(config,batch_id)
        incomplete=[i for i in m['items'] if i['status'] not in ('skipped','mcp_verified')]
        if incomplete and not accept_partial:raise ValueError('unfinished items; review/reconcile or explicitly close partial')
        if not m['backup_before']:raise ValueError('pre-backup missing')
        name=dt.datetime.now().strftime('%Y%m%d-%H%M%S-')+batch_id+'-'+uuid.uuid4().hex[:6]
        # Reserve the path before backup to include the exact closed manifest.
        # Status is terminal only if that backup's COMPLETE marker verifies.
        m.pop('close_error',None)
        m.update(status='partial' if incomplete else 'completed',backup_after={'backup':str(config.backup_dir/name)},closed_at=now())
        save(directory,m,'batch.close_prepared',accept_partial=accept_partial)
        try:
            result=transport(config,'backup',{'name':name})
            if not backup_complete(result):raise RuntimeError('post-backup incomplete')
        except Exception as error:
            m.update(status='partial',close_error=str(error));save(directory,m,'batch.close_failed',message=str(error));raise
        return m
