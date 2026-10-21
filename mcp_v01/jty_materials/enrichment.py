"""Post-intake document profiles and cautious financial candidates. No verified writes."""
import datetime as dt,hashlib,json,re
from decimal import Decimal
from pathlib import Path
from PIL import Image,ImageOps
import pymupdf
from .common import digest,now,safe_path,normalize
from .store import Store
from .workflow import audit
from .fields import validate_value

def _blocks(record):
    out=[]
    response=record.get('response',{})
    data=response.get('data',[]) if isinstance(response,dict) else []
    if not isinstance(data,list):return out  # Umi code 101 may return a string.
    for block in data:
        if not isinstance(block,dict) or not isinstance(block.get('text'),str):continue
        box=block.get('box') or block.get('position')
        if not isinstance(box,list) or len(box)<4 or any(not isinstance(p,(list,tuple)) or len(p)!=2 or any(type(v) not in (int,float) for v in p) for p in box):continue
        xs=[p[0] for p in box];ys=[p[1] for p in box]
        out.append(dict(text=normalize(block['text']).strip(),x=(min(xs)+max(xs))/2,y=(min(ys)+max(ys))/2,
                        x0=min(xs),x1=max(xs),h=max(ys)-min(ys)))
    return out

def financial_candidates(records,source):
    """Use page-local units and aligned single rows; ambiguous columns are skipped."""
    result=[]
    for index,record in enumerate(records):
        blocks=_blocks(record)
        text='\n'.join(b['text'] for b in blocks) or record.get('text','')
        units=re.findall(r'(?:金额)?单位\s*[:：]\s*(?:人民币)?(万元|元)',text)
        if not units or len(set(units))!=1 or any(c in text for c in ['美元','港元']):continue
        unit=1000000 if units[0]=='万元' else 100
        headers=[b for b in blocks if re.fullmatch(r'(本期金额|本年金额|本年累计金额|本期发生额|本年发生额|期末余额|年末余额)',b['text'])]
        for label,field in [('营业收入','revenue'),('净利润','net_profit'),('资产总计','total_assets'),('负债合计','total_liabilities')]:
            for b in blocks:
                if label not in b['text'] or '\n' in b['text']:continue
                values=[];basis=None
                inline=re.findall(r'(?<![\d.(])(-?\d[\d,]*\.\d{2})(?![\d.])',b['text'])
                if len(inline)==1:values=inline;basis='single amount in explicitly labeled row'
                if not values:
                    cells=[x for x in blocks if x['x']>b['x'] and '\n' not in x['text'] and abs(x['y']-b['y'])<=max(x['h'],b['h'],10)*0.65
                            and re.fullmatch(r'-?\d[\d,]*\.\d{2}',x['text'])]
                    cells.sort(key=lambda x:x['x'])
                    if headers and cells:
                        h=headers[0];cell=min(cells,key=lambda x:abs(x['x']-h['x']))
                        if abs(cell['x']-h['x'])<=max(h['x1']-h['x0'],120):values=[cell['text']];basis='page-local 本期/本年/期末 column aligned by OCR coordinates'
                    elif len(cells)==1:values=[cells[0]['text']];basis='single aligned monetary cell; period still candidate'
                for raw in values:
                    try:value=int(Decimal(raw.replace(',',''))*unit);validate_value(field,value)
                    except (ValueError,TypeError):continue
                    evidence=json.dumps(dict(page=record.get('page',index+1),label=b['text'],amount=raw,unit=units[0],column_basis=basis,warning='candidate; period and audited interpretation not human confirmed'),ensure_ascii=False)
                    result.append(dict(field=field,value=value,page=record.get('page',index+1),raw=evidence))
        # Native PDFs often preserve rows as text, but not reliable OCR coordinates.
        if not blocks:
            for label,field in [('营业收入','revenue'),('净利润','net_profit'),('资产总计','total_assets'),('负债合计','total_liabilities')]:
                for m in re.finditer(label+r'[^\d\n-]{0,12}(-?\d[\d,]*\.\d{2})\s*(?=\n|$)',text):
                    try:value=int(Decimal(m[1].replace(',',''))*unit);validate_value(field,value)
                    except (ValueError,TypeError):continue
                    result.append(dict(field=field,value=value,page=record.get('page',index+1),raw=json.dumps(dict(page=record.get('page',index+1),row=m[0],unit=units[0],warning='candidate; comparative period must be checked'),ensure_ascii=False)))
    return result

def enrich(config):
    counts=dict(profiles=0,financial_candidates=0,invalid_voltage_candidates_superseded=0,unneeded_identity_candidates_superseded=0)
    with Store(config) as s:
        rows=[dict(r) for r in s.db.execute("SELECT f.*,t.code AS material_type FROM material_file f JOIN material m USING(material_id) JOIN material_type t ON t.type_id=m.material_type_id WHERE f.role='original' AND f.is_current=1 AND m.material_id>10")]
        for row in rows:
            mid=row['material_id'];source=safe_path(config.repository_root,row['relative_path'])
            if digest(source)!=row['sha256']:raise ValueError('registered original checksum mismatch')
            extraction=s.db.execute("SELECT * FROM extraction_run WHERE source_file_id=? AND extractor='umi-ocr+typed-candidate-rules' ORDER BY run_id DESC LIMIT 1",(row['file_id'],)).fetchone()
            opts=json.loads(extraction['params_json']) if extraction else {}
            profile=dict(original_file_id=row['file_id'],original_sha256=row['sha256'],extension=source.suffix,
                text_status=opts.get('text_status','unknown'),extraction_errors=opts.get('errors',[]),
                completeness='complete source bytes retained; legal/document completeness not automatically certified')
            current_text=s.db.execute("SELECT relative_path,file_id FROM material_file WHERE material_id=? AND role='ocr_raw_text' AND is_current=1",(mid,)).fetchone()
            if current_text:
                profile['raw_file_id']=current_text['file_id']
                raw_text=safe_path(config.repository_root,current_text[0]).read_text(encoding='utf-8')
                profile['text_quality']='sparse_or_non_document' if len(re.sub(r'\W','',raw_text))<12 else 'not_human_assessed'
                if profile['text_quality']=='sparse_or_non_document':profile['text_quality_note']='Photo/blank page/sparse OCR possible; directory person association is still a candidate, not visual identity confirmation.'
            try:
                if source.suffix.lower()=='.pdf':
                    with pymupdf.open(source) as doc:profile['page_count']=len(doc)
                elif source.suffix.lower() in ('.jpg','.jpeg','.png'):
                    with Image.open(source) as im:profile.update(page_count=1,width=im.width,height=im.height,exif_orientation=im.getexif().get(274))
            except Exception as e:profile['profile_warning']=type(e).__name__+': '+str(e)[:200]
            with s.db:
                if not s.db.execute("SELECT 1 FROM audit_log WHERE action='material.document_profile' AND entity_id=? AND json_extract(detail_json,'$.original_file_id')=? AND json_extract(detail_json,'$.raw_file_id')=?",(mid,row['file_id'],profile.get('raw_file_id'))).fetchone():
                    audit(s.db,'phase1-rule','material.document_profile','material',mid,profile);counts['profiles']+=1
            if row['material_type']=='person_id_card':
                with s.db:
                    for old in list(s.db.execute("SELECT e.extracted_id FROM extracted_fact e JOIN extraction_run r USING(run_id) WHERE r.source_file_id=? AND e.field_name='certificate_no' AND e.review_status='pending'",(row['file_id'],))):
                        s.db.execute("UPDATE extracted_fact SET review_status='superseded',review_note='Machine rule revision: identity number is not a bid certificate number; complete original/OCR retained' WHERE extracted_id=?",(old['extracted_id'],))
                        audit(s.db,'phase1-rule','candidate.identity_rule_revision','material',mid,dict(extracted_id=old['extracted_id'],field='certificate_no',reason='identity document text stays in original/OCR; do not mislabel it as a professional certificate number',human_verified=False));counts['unneeded_identity_candidates_superseded']+=1
            if row['material_type']=='audit_report':
                with s.db:
                    old_values=list(s.db.execute("SELECT e.extracted_id,e.field_name FROM extracted_fact e JOIN extraction_run r USING(run_id) WHERE r.source_file_id=? AND r.extractor='umi-ocr+typed-candidate-rules' AND e.field_name IN ('revenue','net_profit','total_assets','total_liabilities') AND e.review_status='pending'",(row['file_id'],)))
                    for old in old_values:
                        s.db.execute("UPDATE extracted_fact SET review_status='superseded',review_note='Machine rule revision: use page-local units and columns, not document-wide unit inference' WHERE extracted_id=?",(old['extracted_id'],))
                        audit(s.db,'phase1-rule','candidate.financial_rule_revision','material',mid,dict(extracted_id=old['extracted_id'],field=old['field_name'],reason='require page-local unit/column evidence',human_verified=False))
                raw=s.db.execute("SELECT relative_path FROM material_file WHERE material_id=? AND role='ocr_raw_jsonl' AND is_current=1",(mid,)).fetchone()
                if raw:
                    records=[json.loads(l) for l in safe_path(config.repository_root,raw[0]).read_text(encoding='utf-8').splitlines() if l.strip()]
                    values=financial_candidates(records,source)
                    if values:
                        with s.db:
                            run=s.db.execute("INSERT INTO extraction_run(material_id,source_file_id,extractor,extractor_version,params_json,run_at,status) VALUES(?,?,'page-local-financial-candidate-rules','0.4',?,?,'succeeded') RETURNING run_id",(mid,row['file_id'],json.dumps(dict(original_sha256=row['sha256'],candidate_only=True)),now())).fetchone()[0]
                            for v in values:
                                if s.db.execute('SELECT 1 FROM extracted_fact e JOIN extraction_run r USING(run_id) WHERE r.source_file_id=? AND e.field_name=? AND e.normalized_value=? AND e.evidence_span=?',(row['file_id'],v['field'],str(v['value']),v['raw'])).fetchone():continue
                                s.db.execute("INSERT INTO extracted_fact(run_id,material_id,field_name,value_type,raw_value,normalized_value,value_amount_cents,page_no,evidence_span) VALUES(?,?,?,'amount',?,?,?,?,?)",(run,mid,v['field'],v['raw'],str(v['value']),v['value'],v['page'],v['raw']));counts['financial_candidates']+=1
                            audit(s.db,'phase1-rule','candidate.financial_page_rules','material',mid,dict(original_file_id=row['file_id'],fields=sorted({v['field'] for v in values}),human_verified=False))
            if row['material_type'] in ('contract','award_notice','acceptance_doc','owner_evaluation'):
                raw=s.db.execute("SELECT relative_path FROM material_file WHERE material_id=? AND role='ocr_raw_text' AND is_current=1",(mid,)).fetchone()
                ctx=s.db.execute("SELECT detail_json FROM audit_log WHERE action='material.source_context' AND entity_id=? ORDER BY log_id DESC LIMIT 1",(mid,)).fetchone()
                if raw:
                    text=normalize(safe_path(config.repository_root,raw[0]).read_text(encoding='utf-8'))+'\n'+(json.loads(ctx[0]).get('source_relative_path','') if ctx else '')
                    valid={int(m[1]) for m in re.finditer(r'(?<![\d.])(\d{1,3})\s*[kK][vV](?![aA])',text) if int(m[1])>0}
                    invalid=[dict(r) for r in s.db.execute("SELECT e.* FROM extracted_fact e JOIN extraction_run r USING(run_id) WHERE r.source_file_id=? AND e.field_name='voltage_level_kv' AND e.review_status='pending'",(row['file_id'],)) if int(r['normalized_value']) not in valid]
                    with s.db:
                        for old in invalid:
                            s.db.execute("UPDATE extracted_fact SET review_status='superseded',review_note='Machine rule revision: do not confuse kVA or truncate decimal kV; not a human review' WHERE extracted_id=?",(old['extracted_id'],))
                            audit(s.db,'phase1-rule','candidate.rule_revision','material',mid,dict(extracted_id=old['extracted_id'],old_value=old['normalized_value'],field='voltage_level_kv',reason='kVA is capacity, not voltage; decimal kV must not be truncated',human_verified=False));counts['invalid_voltage_candidates_superseded']+=1
    return counts
