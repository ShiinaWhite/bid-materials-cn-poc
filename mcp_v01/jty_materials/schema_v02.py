"""Forward-only migration: one org column and one fixed power-permission table."""
from .common import now
from .fields import ORG_FIELDS,GRADE_FIELDS

VERSION='jty-mcp-0.2.0'

def upgrade(con):
    version=con.execute("SELECT meta_value FROM schema_meta WHERE meta_key='schema_version'").fetchone()[0]
    if version==VERSION:return False
    if version!='jty-mcp-0.1.0':raise ValueError('v0.2 migration requires schema jty-mcp-0.1.0')
    con.execute("ALTER TABLE org ADD COLUMN established_on TEXT CHECK(established_on IS NULL OR (length(established_on)=10 AND date(established_on)=established_on))")
    con.execute("""CREATE TABLE power_license_item(
      material_id INTEGER NOT NULL REFERENCES certificate_detail(material_id),
      activity TEXT NOT NULL CHECK(activity IN ('install','repair','test')),
      grade INTEGER CHECK(grade IS NULL OR grade BETWEEN 1 AND 5),
      PRIMARY KEY(material_id,activity)) STRICT""")
    # Identity/binding guards: evidence cannot be silently repointed to another entity/type.
    con.execute("""CREATE TRIGGER v02_material_binding BEFORE UPDATE ON material
      WHEN NEW.material_type_id<>OLD.material_type_id OR NEW.subject_org_id IS NOT OLD.subject_org_id
      BEGIN SELECT RAISE(ABORT,'material type/entity binding is immutable'); END""")
    mapping="(SELECT code FROM material_type WHERE type_id=(SELECT material_type_id FROM material WHERE material_id=NEW.material_id))"
    con.execute(f"""CREATE TRIGGER v02_certificate_type BEFORE INSERT ON certificate_detail
      WHEN NOT (({mapping}='business_license' AND NEW.certificate_kind='business_license')
        OR ({mapping}='safety_prod_license' AND NEW.certificate_kind='safety_production')
        OR ({mapping}='power_facility_license' AND NEW.certificate_kind='power_facility'))
      BEGIN SELECT RAISE(ABORT,'certificate type/kind mismatch or unsupported type'); END""")
    con.execute("""CREATE TRIGGER v02_certificate_kind BEFORE UPDATE ON certificate_detail
      WHEN NEW.certificate_kind<>OLD.certificate_kind BEGIN SELECT RAISE(ABORT,'certificate kind is immutable'); END""")
    con.execute("""CREATE TRIGGER v02_org_insert BEFORE INSERT ON org
      WHEN NEW.name<>'' OR NEW.name_norm<>'' OR NEW.unified_social_credit_code IS NOT NULL OR NEW.established_on IS NOT NULL
      BEGIN SELECT RAISE(ABORT,'create unverified empty entity then review facts'); END""")
    con.execute("""CREATE TRIGGER v02_org_replace BEFORE INSERT ON org
      WHEN EXISTS(SELECT 1 FROM org WHERE org_id=NEW.org_id OR org_code=NEW.org_code OR (NEW.is_self=1 AND is_self=1))
      BEGIN SELECT RAISE(ABORT,'org replacement forbidden'); END""")
    con.execute("""CREATE TRIGGER v02_org_identity BEFORE UPDATE ON org
      WHEN NEW.org_id<>OLD.org_id OR NEW.org_code<>OLD.org_code OR NEW.is_self<>OLD.is_self
      BEGIN SELECT RAISE(ABORT,'entity identity is immutable'); END""")
    con.execute("""CREATE TRIGGER v02_org_norm BEFORE UPDATE ON org
      WHEN NEW.name_norm<>org_norm(NEW.name) BEGIN SELECT RAISE(ABORT,'name_norm must be derived from name'); END""")
    for table in ('org','power_license_item'):
        con.execute(f"CREATE TRIGGER v02_no_delete_{table} BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT,'retain entity/permission evidence'); END")
    con.execute("""CREATE TRIGGER v02_power_insert BEFORE INSERT ON power_license_item
      WHEN NEW.grade IS NOT NULL OR EXISTS(SELECT 1 FROM power_license_item WHERE material_id=NEW.material_id AND activity=NEW.activity)
        OR NOT EXISTS(SELECT 1 FROM material m JOIN material_type t ON t.type_id=m.material_type_id WHERE m.material_id=NEW.material_id AND t.code='power_facility_license')
      BEGIN SELECT RAISE(ABORT,'create unverified permission only for power license; no replace'); END""")
    con.execute("""CREATE TRIGGER v02_power_identity BEFORE UPDATE ON power_license_item
      WHEN NEW.material_id<>OLD.material_id OR NEW.activity<>OLD.activity
      BEGIN SELECT RAISE(ABORT,'permission identity is immutable'); END""")
    con.execute('DROP TRIGGER guard_verification_insert')
    con.execute("""CREATE TRIGGER guard_verification_insert BEFORE INSERT ON verification_event
      WHEN review_actor() IS NULL OR NEW.material_id IS NOT review_material()
        OR NEW.verified_by IS NOT review_actor() OR NEW.field_name IS NOT review_field()
        OR NEW.evidence_file_id IS NOT review_evidence() OR NEW.reason IS NOT review_reason()
        OR NEW.verification_method<>'manual_original' OR trim(NEW.reason)=''
        OR NOT EXISTS(SELECT 1 FROM material_file f WHERE f.file_id=NEW.evidence_file_id
          AND f.material_id=NEW.material_id AND f.role='original' AND f.is_current=1 AND f.sha256=review_sha256())
      BEGIN SELECT RAISE(ABORT,'explicit review of current original/SHA256 required'); END""")
    # Recreate the existing per-column guards with the same coverage, routing
    # all history through one evidence-audit trigger (including same-value review).
    for r in con.execute("SELECT name FROM sqlite_schema WHERE type='trigger' AND (name LIKE 'review_certificate_detail_%' OR name LIKE 'review_performance_detail_%')").fetchall():
        con.execute('DROP TRIGGER "'+r[0]+'"')
    for table,meta in [('certificate_detail',{'material_id','certificate_kind','notes'}),('performance_detail',{'material_id','notes'})]:
        for _,col,_,_,default,_ in con.execute(f'PRAGMA table_info({table})').fetchall():
            if col in meta:continue
            alias='contract_amount' if col=='contract_amount_cents' else col
            for op in ('INSERT','UPDATE'):
                condition=f'NEW.{col} IS NOT NULL'+(f' AND NEW.{col} IS NOT ({default})' if default is not None else '') if op=='INSERT' else f'NEW.{col} IS NOT OLD.{col}'
                old='NULL' if op=='INSERT' else f'CAST(OLD.{col} AS TEXT)'
                fact_trigger(con,f'v02_review_{table}_{col}_{op}',table,op,condition,"'"+alias+"'",old,f'NEW.{col}', 'NEW.material_id')
    for alias,(_,col,_) in ORG_FIELDS.items():
        fact_trigger(con,'v02_review_org_'+col,'org','UPDATE',f'NEW.{col} IS NOT OLD.{col}',"'"+alias+"'",f'CAST(OLD.{col} AS TEXT)',f'NEW.{col}','review_material()',org=True)
    fact_trigger(con,'v02_review_power_grade','power_license_item','UPDATE','NEW.grade IS NOT OLD.grade',"NEW.activity||'_grade'",'CAST(OLD.grade AS TEXT)','NEW.grade','NEW.material_id')
    org_fields=','.join("'"+f+"'" for f in ORG_FIELDS)
    con.execute(f"""CREATE TRIGGER v02_verification_audit AFTER INSERT ON verification_event BEGIN
      INSERT INTO audit_log(at,actor,action,entity_type,entity_id,detail_json,note)
      VALUES(NEW.occurred_at,NEW.verified_by,'fact.'||NEW.action,
        CASE WHEN NEW.field_name IN ({org_fields}) THEN 'org' ELSE 'fact' END,
        CASE WHEN NEW.field_name IN ({org_fields}) THEN (SELECT subject_org_id FROM material WHERE material_id=NEW.material_id) ELSE NEW.material_id END,
        json_object('material_id',NEW.material_id,'org_id',(SELECT subject_org_id FROM material WHERE material_id=NEW.material_id),
          'field',NEW.field_name,'old',NEW.old_value,'new',NEW.new_value,'event_id',NEW.event_id,
          'evidence_file_id',NEW.evidence_file_id,'evidence_sha256',(SELECT sha256 FROM material_file WHERE file_id=NEW.evidence_file_id)),NEW.reason);
      END""")
    original_view=con.execute("SELECT sql FROM sqlite_schema WHERE name='v_current_facts'").fetchone()[0]
    con.execute('DROP VIEW v_current_facts')
    extra=[]
    for alias,(_,col,kind) in ORG_FIELDS.items():
        values=f'o.{col},NULL,NULL,NULL' if kind!='date' else f'NULL,o.{col},NULL,NULL'
        extra.append(f"SELECT m.material_id,m.material_code,'org','{alias}','{'date' if kind=='date' else 'text'}',{values} FROM material m JOIN org o ON o.org_id=m.subject_org_id WHERE o.{col} IS NOT NULL AND o.{col}<>''")
    for col,kind in [('valid_from','date'),('long_term_valid','bool'),('issuing_authority','text'),('scope','text')]:
        values=f'NULL,c.{col},NULL,NULL' if kind=='date' else f'NULL,NULL,c.{col},NULL' if kind=='bool' else f'c.{col},NULL,NULL,NULL'
        extra.append(f"SELECT m.material_id,m.material_code,'certificate_detail','{col}','{kind}',{values} FROM material m JOIN certificate_detail c USING(material_id) WHERE c.{col} IS NOT NULL")
    extra.append("SELECT m.material_id,m.material_code,'power_license_item',p.activity||'_grade','int',NULL,NULL,p.grade,NULL FROM material m JOIN power_license_item p USING(material_id) WHERE p.grade IS NOT NULL")
    con.execute(original_view+' UNION ALL '+' UNION ALL '.join(extra))
    con.execute("UPDATE schema_meta SET meta_value=?,updated_at=? WHERE meta_key='schema_version'",(VERSION,now()))
    return True

def fact_trigger(con,name,table,op,condition,field,old,new,material,org=False):
    binding=" OR NOT EXISTS(SELECT 1 FROM material WHERE material_id=review_material() AND subject_org_id=NEW.org_id)" if org else f' OR {material} IS NOT review_material()'
    con.execute(f"""CREATE TRIGGER {name} BEFORE {op} ON {table} WHEN {condition} BEGIN
      SELECT CASE WHEN review_actor() IS NULL OR review_field() IS NOT ({field}) {binding}
        THEN RAISE(ABORT,'facts require explicit human review context') END;
      INSERT INTO verification_event(material_id,field_name,action,old_value,new_value,evidence_file_id,
        source_extracted_id,verification_method,verified_by,reason,occurred_at)
      VALUES({material},{field},CASE WHEN review_action()='retract' THEN 'retract' WHEN {old} IS NULL OR {old}='' THEN 'verify' ELSE 'correct' END,
        {old},CASE WHEN review_action()='retract' THEN NULL ELSE CAST({new} AS TEXT) END,
        review_evidence(),review_candidate(),'manual_original',review_actor(),review_reason(),review_time());
      END""")
