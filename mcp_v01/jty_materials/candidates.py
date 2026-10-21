"""Offline, evidence-bound candidate intake. Never a human verification entry."""
import json
from .common import digest,now,safe_path
from .fields import allowed_field,field_spec,validate_value
from .store import Store
from .workflow import audit

def append_candidates(config,record):
    required={'material_id','evidence_file_id','evidence_sha256','actor','extractor','candidates'}
    if set(record)!=required:raise ValueError('candidate record requires exactly '+','.join(sorted(required)))
    if not record['actor'] or not record['extractor'] or not 1<=len(record['candidates'])<=100:raise ValueError('actor/extractor and 1..100 candidates required')
    with Store(config) as s:
        original=s.db.execute("SELECT f.*,t.code FROM material_file f JOIN material m USING(material_id) JOIN material_type t ON t.type_id=m.material_type_id WHERE f.material_id=? AND f.file_id=? AND f.role='original' AND f.is_current=1",(record['material_id'],record['evidence_file_id'])).fetchone()
        if original is None or original['sha256']!=record['evidence_sha256'] or digest(safe_path(config.repository_root,original['relative_path']))!=record['evidence_sha256']:raise ValueError('candidate evidence is not the unchanged current original')
        checked=[]
        for row in record['candidates']:
            if set(row)!={'field','value','page','evidence'} or not allowed_field(original['code'],row['field']):raise ValueError('unsupported candidate field/record')
            value=validate_value(row['field'],row['value'])
            if value is None or type(row['page']) is not int or row['page']<1 or not isinstance(row['evidence'],str) or not row['evidence'].strip():raise ValueError('candidate value, positive page and evidence required')
            kind=field_spec(row['field'],original['code'])[2];vt='amount' if kind in ('amount','signed_amount') else 'int' if kind in ('int','year','grade') else 'text' if kind=='credit' else kind
            # Provenance is part of the candidate itself, not a verification claim.
            span=json.dumps(dict(extractor=record['extractor'],human_verified=False,page=row['page'],basis=row['evidence'],original_file_id=original['file_id'],original_sha256=original['sha256']),ensure_ascii=False)
            checked.append((row['field'],vt,str(value),row['page'],span))
        added=0
        with s.db:
            run=s.db.execute("INSERT INTO extraction_run(material_id,source_file_id,extractor,extractor_version,params_json,run_at,status) VALUES(?,?,?,'0.4',?,?,'succeeded') RETURNING run_id",(record['material_id'],original['file_id'],record['extractor'],json.dumps(dict(candidate_only=True,original_sha256=original['sha256'])),now())).fetchone()[0]
            for field,vt,value,page,span in checked:
                if s.db.execute('SELECT 1 FROM extracted_fact e JOIN extraction_run r USING(run_id) WHERE r.source_file_id=? AND e.field_name=? AND e.normalized_value=? AND e.evidence_span=?',(original['file_id'],field,value,span)).fetchone():continue
                s.db.execute("INSERT INTO extracted_fact(run_id,material_id,field_name,value_type,raw_value,normalized_value,page_no,evidence_span) VALUES(?,?,?,?,?,?,?,?)",(run,record['material_id'],field,vt,span,value,page,span));added+=1
            audit(s.db,record['actor'],'candidate.append','material',record['material_id'],dict(extractor=record['extractor'],fields=[x[0] for x in checked],added=added,evidence_file_id=original['file_id'],evidence_sha256=original['sha256'],human_verified=False))
        return dict(material_id=record['material_id'],added=added,human_verified=False)
