"""Offline, serialized material import/version/review operations."""
import json
from pathlib import Path
import re
import shutil
import uuid

from .common import atomic_write, digest, normalize, now, safe_path
from .ocr import Ocr
from .store import Store
from .fields import resolve_type,PERSON_TYPES,PERFORMANCE_TYPES,TYPE_TABLE,CERTIFICATES


def audit(db, actor, action, entity, entity_id, detail):
    db.execute("INSERT INTO audit_log(at,actor,action,entity_type,entity_id,detail_json) VALUES(?,?,?,?,?,?)",
               (now(), actor, action, entity, entity_id, json.dumps(detail, ensure_ascii=False)))


def rebuild_projections(config):
    with Store(config) as store:
        for row in store.db.execute("""SELECT p.relative_path,p.sha256,f.relative_path AS source
                FROM search_projection p JOIN material_file f ON f.file_id=p.search_file_id WHERE f.is_current=1"""):
            source = safe_path(config.repository_root, row["source"])
            if digest(source) != row["sha256"]:
                raise ValueError("stored search version checksum mismatch")
            target=safe_path(config.repository_root,row['relative_path'])
            if not target.is_file() or digest(target)!=row['sha256']:
                atomic_write(target, source.read_bytes())


def import_material(config, source, kind=None, title=None, actor=None, material_id=None, subtype=None, ocr_factory=Ocr,batch_context=None,source_metadata=None,defer_projection=False):
    source = Path(source).resolve(strict=True)
    if source.suffix.lower() not in ('.pdf','.png','.jpg','.jpeg','.docx','.xlsx','.md','.aip') or not source.is_file():
        raise ValueError("supported originals: PDF/PNG/JPEG/DOCX/XLSX/MD/AIP; preserve source bytes")
    if source.is_relative_to(config.repository_root) or source.is_relative_to(config.state_dir):
        raise ValueError("source must be outside managed repository/staging")
    if not actor or not actor.strip():
        raise ValueError("actor is required")
    if material_id is None and (kind not in ("certificate", "contract",'material') or not title or not title.strip()):
        raise ValueError("new import requires certificate/contract kind and title")
    if material_id is None:kind,type_code,certificate_kind=resolve_type(kind,subtype)
    source_hash = digest(source)
    if batch_context is not None:
        if set(batch_context)!={'batch_id','item_id','plan_sha256','source_sha256','expected_current_sha256'} or batch_context['source_sha256']!=source_hash:
            raise ValueError('batch source/context mismatch')
    with Store(config) as store:
        if batch_context is not None:
            prior=batch_receipt(store.db,batch_context)
            if prior is not None:return prior
            duplicate=store.db.execute("SELECT f.material_id,f.file_id,f.version_id,t.code FROM material_file f JOIN material m USING(material_id) JOIN material_type t ON t.type_id=m.material_type_id WHERE f.role='original' AND f.sha256=? ORDER BY f.file_id LIMIT 1",(source_hash,)).fetchone()
            if duplicate is not None:
                if duplicate['code']!=subtype:raise ValueError('duplicate original type conflicts with approved subtype')
                if material_id is not None and duplicate['material_id']!=material_id:raise ValueError('duplicate belongs to another material; review plan')
                receipt=dict(material_id=duplicate['material_id'],duplicate=True,sha256=source_hash,original_file_id=duplicate['file_id'],version_id=duplicate['version_id'])
                with store.db:audit(store.db,actor,'batch.item.commit','material',duplicate['material_id'],dict(**batch_context,receipt=receipt))
                return receipt
        if material_id is not None:
            m = store.db.execute("SELECT m.*,t.code FROM material m JOIN material_type t ON t.type_id=m.material_type_id WHERE material_id=?", (material_id,)).fetchone()
            if m is None:
                raise ValueError("material not found")
            kind,type_code,certificate_kind=resolve_type(kind,subtype,existing=m['code'])
            current = store.db.execute("SELECT sha256 FROM material_file WHERE material_id=? AND role='original' AND is_current=1", (material_id,)).fetchone()
            if current and current[0] == source_hash:
                return {"material_id": material_id, "unchanged": True, "sha256": source_hash}
            if batch_context is not None and (not current or current[0]!=batch_context['expected_current_sha256']):raise ValueError('current original changed since approved plan')
    version = uuid.uuid4().hex
    stage = config.state_dir / "staging" / version
    stage.mkdir(parents=True, exist_ok=False)
    original = stage / ('original'+source.suffix.lower())
    atomic_write(original, source.read_bytes(), immutable=True)
    if digest(original) != source_hash:
        raise ValueError("source changed during copy")
    ocr = ocr_factory(config)
    try:
        ocr.start()
        result = ocr.document(original)
    finally:
        ocr.stop()
    text = Path(result["raw"]).read_text(encoding="utf-8")
    if material_id is None and type_code=='other' and source_metadata and source_metadata.get('auto_type_from_explicit_title'):
        header=normalize(text[:500])
        detected='software_copyright' if '计算机软件著作权登记证书' in header else 'patent' if '实用新型专利证书' in header or '发明专利证书' in header else None
        if detected:
            source_metadata=dict(source_metadata,inferred_type_before=type_code,type_inference_basis='explicit title in original OCR, still unverified');type_code=detected;certificate_kind=None
    from .business import extract_candidates
    candidates=list(extract_candidates(text,type_code,source_metadata))
    with Store(config) as store:
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            if batch_context is not None and material_id is not None:
                current=store.db.execute("SELECT sha256 FROM material_file WHERE material_id=? AND role='original' AND is_current=1",(material_id,)).fetchone()
                if not current or current[0]!=batch_context['expected_current_sha256']:raise ValueError('current original changed during staging')
            if material_id is None:
                subject_org=None
                if type_code=='business_license':
                    subject_org=store.db.execute("INSERT INTO org(org_code,name,name_norm,status,created_at,updated_at) VALUES(?,'','','unknown',?,?) RETURNING org_id",('ORG-'+uuid.uuid4().hex,now(),now())).fetchone()[0]
                material_id = store.db.execute("""INSERT INTO material(title,material_type_id,source_note,created_at,updated_at,subject_org_id)
                    VALUES(?,(SELECT type_id FROM material_type WHERE code=?),?,?,?,?) RETURNING material_id""",
                    (title,type_code,
                     "offline import; human review required", now(), now(),subject_org)).fetchone()[0]
                if type_code in CERTIFICATES:
                    store.db.execute("INSERT INTO certificate_detail(material_id,certificate_kind) VALUES(?,?)", (material_id,certificate_kind))
                    if type_code=='power_facility_license':
                        store.db.executemany('INSERT INTO power_license_item(material_id,activity) VALUES(?,?)',[(material_id,a) for a in ('install','repair','test')])
                elif type_code in PERFORMANCE_TYPES:
                    store.db.execute("INSERT INTO performance_detail(material_id) VALUES(?)", (material_id,))
                elif type_code in PERSON_TYPES:
                    store.db.execute('INSERT INTO person_certificate_detail(material_id,certificate_kind) VALUES(?,?)',(material_id,PERSON_TYPES[type_code]))
                elif type_code in ('patent','software_copyright'):
                    store.db.execute('INSERT INTO '+TYPE_TABLE[type_code]+'(material_id) VALUES(?)',(material_id,))
            code = store.db.execute("SELECT material_code FROM material WHERE material_id=?", (material_id,)).fetchone()[0]
            target = config.materials / code / ".versions" / version
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(stage, target)
            old_files = [dict(r) for r in store.db.execute("SELECT file_id,role,sha256,version_id FROM material_file WHERE material_id=? AND is_current=1", (material_id,))]
            old_people=[dict(r) for r in store.db.execute('SELECT * FROM material_person WHERE material_id=?',(material_id,))]
            store.db.execute("UPDATE material_file SET is_current=0 WHERE material_id=? AND is_current=1", (material_id,))
            store.db.execute("UPDATE extracted_fact SET review_status='superseded' WHERE material_id=?", (material_id,))
            files = {}
            for role, key in (("original", "original"), ("ocr_raw_text", "raw"), ("ocr_raw_jsonl", "jsonl"), ("search_text", "search")):
                relative_stage = Path(result[key]).relative_to(stage)
                files[role] = store.register_file(material_id, target / relative_stage, role, version)
            run = store.db.execute("""INSERT INTO extraction_run(material_id,source_file_id,extractor,extractor_version,params_json,run_at,status)
                VALUES(?,?,'umi-ocr+typed-candidate-rules','2.1.5/v0.6.1',?,?,?) RETURNING run_id""",
                (material_id, files["original"], json.dumps(result["options"], ensure_ascii=False), now(),'partial' if result['options'].get('text_status') in ('metadata_only','partial') else 'succeeded')).fetchone()[0]
            for field, value_type, raw, normalized in candidates:
                store.db.execute("""INSERT INTO extracted_fact(run_id,material_id,field_name,value_type,raw_value,normalized_value,evidence_span)
                    VALUES(?,?,?,?,?,?,?)""", (run, material_id, field, value_type, raw, normalized, raw[:1000]))
            search_file = store.db.execute("SELECT relative_path,sha256 FROM material_file WHERE file_id=?", (files["search_text"],)).fetchone()
            projection = f"materials/{code}/current.search.txt"
            safe_path(config.repository_root, projection)
            store.db.execute("""INSERT INTO search_projection(material_id,relative_path,search_file_id,sha256) VALUES(?,?,?,?)
                ON CONFLICT(material_id) DO UPDATE SET search_file_id=excluded.search_file_id,sha256=excluded.sha256""",
                (material_id, projection, files["search_text"], search_file["sha256"]))
            store.db.execute("UPDATE material SET verification_status='unverified',verified_at=NULL,verified_by=NULL WHERE material_id=?", (material_id,))
            audit(store.db, actor, "material.version" if old_files else "material.import", "material", material_id,
                  {"version_id": version, "source_sha256": source_hash, "source_name": source.name,
                   "files": files, "previous_files": old_files, "previous_person_relations":old_people,"requires_human_review": True})
            receipt={"material_id":material_id,"material_code":code,"version_id":version,"files":files,"original_sha256":source_hash,"requires_human_review":True}
            if batch_context is not None:audit(store.db,actor,'batch.item.commit','material',material_id,dict(**batch_context,receipt=receipt))
            if source_metadata is not None:
                audit(store.db,actor,'material.source_context','material',material_id,dict(source_metadata,original_file_id=files['original'],original_sha256=source_hash,candidate_only=True))
                person_label=source_metadata.get('person_label')
                if person_label and source_metadata.get('person_relation_status')=='candidate':
                    person_key='SRC-'+__import__('hashlib').sha256(source_metadata['person_source_key'].encode()).hexdigest()[:20]
                    person=store.db.execute('SELECT person_id FROM person WHERE person_code=?',(person_key,)).fetchone()
                    if person is None:
                        pid=store.db.execute("INSERT INTO person(person_code,name,name_norm,status,identity_status,notes,created_at,updated_at) VALUES(?,?,?,'unknown','candidate',?,?,?) RETURNING person_id",(person_key,person_label,normalize(person_label),'Directory label only; not human-verified identity',now(),now())).fetchone()[0]
                    else:pid=person[0]
                    store.db.execute("INSERT INTO material_person(material_id,person_id,role_on_material,is_primary,relation_status,evidence_file_id) VALUES(?,?,'holder',1,'candidate',?) ON CONFLICT(material_id,person_id,role_on_material) DO UPDATE SET evidence_file_id=excluded.evidence_file_id,relation_status='candidate'",(material_id,pid,files['original']))
            if type_code in PERSON_TYPES and old_people and not (source_metadata and source_metadata.get('person_label') and source_metadata.get('person_relation_status')=='candidate'):
                store.db.execute("UPDATE material_person SET evidence_file_id=?,relation_status='relation_pending' WHERE material_id=?",(files['original'],material_id))
                audit(store.db,actor,'person.relation_version_pending','material',material_id,dict(previous=old_people,new_original_file_id=files['original'],reason='original replaced; prior identity relation retained only as pending, not reverified'))
    if not defer_projection:rebuild_projections(config)
    config.log("material.imported", material_id=material_id, version_id=version, original_sha256=source_hash, files=files)
    return {"material_id": material_id, "material_code": code, "version_id": version,
            "files": files, "original_sha256": source_hash, "requires_human_review": True}


def batch_receipt(db,context):
    row=db.execute("SELECT detail_json FROM audit_log WHERE action='batch.item.commit' AND json_extract(detail_json,'$.batch_id')=? AND json_extract(detail_json,'$.item_id')=? ORDER BY log_id DESC LIMIT 1",(context['batch_id'],context['item_id'])).fetchone()
    if row is None:return None
    detail=json.loads(row[0])
    if any(detail[k]!=context[k] for k in ('plan_sha256','source_sha256','expected_current_sha256')):raise ValueError('batch token reused with different plan/source')
    return detail['receipt']


def review_fact(config, record):
    required = {"material_id", "field", "value", "actor", "reason", "evidence_file_id", "evidence_sha256"}
    if not isinstance(record, dict) or not required.issubset(record) or set(record) - required - {"candidate_id"}:
        raise ValueError("review requires explicit material, field, value, actor, reason, evidence ID and SHA256")
    if any(type(record[k]) is not int or record[k]<1 for k in ('material_id','evidence_file_id')):raise ValueError('positive integer material/evidence IDs required')
    if not isinstance(record['evidence_sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',record['evidence_sha256']):raise ValueError('canonical SHA256 required')
    with Store(config) as store:
        row = store.db.execute("SELECT relative_path,sha256 FROM material_file WHERE file_id=? AND is_current=1 AND role='original'", (record["evidence_file_id"],)).fetchone()
        if row is None or row["sha256"] != record["evidence_sha256"]:
            raise ValueError("review refers to an outdated or different original")
        store.verify(record["material_id"], record["field"], record["value"], actor=record["actor"],
                     reason=record["reason"], evidence_file_id=record["evidence_file_id"], candidate_id=record.get("candidate_id"))
    config.log("fact.reviewed", material_id=record["material_id"], field=record["field"], actor=record["actor"], evidence_sha256=record["evidence_sha256"])
    return {"material_id": record["material_id"], "field": record["field"], "reviewed": True}
