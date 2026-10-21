"""Forward compatible phase-one business vocabulary; old evidence remains immutable."""
from .common import now
from .schema_v03 import upgrade as upgrade_v03
from .schema_v02 import fact_trigger
from .fields import CERTIFICATES,TYPE_TABLE,DETAIL_FIELDS
VERSION='jty-mcp-0.4.0'

def upgrade(con):
    version=con.execute("SELECT meta_value FROM schema_meta WHERE meta_key='schema_version'").fetchone()[0]
    if version==VERSION:return False
    if version in ('jty-mcp-0.1.0','jty-mcp-0.2.0'):upgrade_v03(con)
    elif version!='jty-mcp-0.3.0':raise ValueError('v0.4 requires v0.1/v0.2/v0.3')
    for code,name,parent in [('tax_credit','纳税信用',4),('person_document','其他人员资格/业务证明',6)]:
        con.execute('INSERT INTO material_type(code,name_zh,parent_id) SELECT ?,?,? WHERE NOT EXISTS(SELECT 1 FROM material_type WHERE code=?)',(code,name,parent,code))
    for col in ['grade']:
        con.execute('ALTER TABLE person_certificate_detail ADD COLUMN '+col+' TEXT')
    for col,kind in [('report_type','TEXT'),('revenue_cents','INTEGER'),('net_profit_cents','INTEGER'),('total_assets_cents','INTEGER'),('total_liabilities_cents','INTEGER')]:
        con.execute('ALTER TABLE audit_report_detail ADD COLUMN '+col+' '+kind)
    for col,kind in [('title','TEXT'),('tax_year','INTEGER'),('rating','TEXT'),('authority_source','TEXT')]:
        con.execute('ALTER TABLE certificate_detail ADD COLUMN '+col+' '+kind)
    con.execute("ALTER TABLE person ADD COLUMN identity_status TEXT NOT NULL DEFAULT 'candidate' CHECK(identity_status IN ('candidate','human_verified','unknown'))")
    con.execute("ALTER TABLE material_person ADD COLUMN relation_status TEXT NOT NULL DEFAULT 'candidate' CHECK(relation_status IN ('candidate','human_verified','relation_pending'))")
    con.execute('ALTER TABLE material_person ADD COLUMN evidence_file_id INTEGER REFERENCES material_file(file_id)')
    con.execute('''CREATE TABLE material_relation(
       relation_id INTEGER PRIMARY KEY,material_id INTEGER NOT NULL REFERENCES material(material_id),
       related_material_id INTEGER NOT NULL REFERENCES material(material_id),relation_type TEXT NOT NULL,
       relation_status TEXT NOT NULL DEFAULT 'relation_pending',detail_json TEXT NOT NULL,created_at TEXT NOT NULL,
       CHECK(material_id<>related_material_id),UNIQUE(material_id,related_material_id,relation_type)) STRICT''')
    for op in ('UPDATE','DELETE'):
        con.execute(f"CREATE TRIGGER v04_relation_{op} BEFORE {op} ON material_relation BEGIN SELECT RAISE(ABORT,'append relation history'); END")
    for table in ['person_certificate_detail','patent_detail','software_copyright_detail','audit_report_detail']:
        for op in ('INSERT','UPDATE','DELETE'):con.execute(f'DROP TRIGGER unsupported_{table}_{op}')
        con.execute(f"CREATE TRIGGER v04_no_delete_{table} BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT,'retain typed evidence'); END")
        con.execute(f"CREATE TRIGGER v04_no_replace_{table} BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE material_id=NEW.material_id) BEGIN SELECT RAISE(ABORT,'no replacing business detail'); END")
        con.execute(f"CREATE TRIGGER v04_stable_{table} BEFORE UPDATE ON {table} WHEN NEW.material_id<>OLD.material_id BEGIN SELECT RAISE(ABORT,'stable material binding'); END")
    # Rebuild only the obsolete type/kind mapping, preserving all existing fact guards.
    con.execute('DROP TRIGGER v03_certificate_type')
    mapping="(SELECT code FROM material_type WHERE type_id=(SELECT material_type_id FROM material WHERE material_id=NEW.material_id))"
    combos=' OR '.join(f"({mapping}='{t}' AND NEW.certificate_kind='{k}')" for t,k in CERTIFICATES.items())
    con.execute(f"CREATE TRIGGER v04_certificate_type BEFORE INSERT ON certificate_detail WHEN NOT ({combos}) BEGIN SELECT RAISE(ABORT,'certificate type/kind mismatch'); END")
    fields_by_table={t:dict(f) for t,f in DETAIL_FIELDS.items() if t!='performance_detail'}
    fields_by_table['certificate_detail']={'title':('title','text'),'year':('tax_year','year'),'rating':('rating','text'),'authority_source':('authority_source','text')}
    extra=[]
    oldview=con.execute("SELECT sql FROM sqlite_schema WHERE name='v_current_facts'").fetchone()[0]
    for table,fields in fields_by_table.items():
        for alias,(col,kind) in fields.items():
            default='unknown' if (table,col) in [('patent_detail','patent_type'),('audit_report_detail','audit_opinion')] else None
            for op in ('INSERT','UPDATE'):
                condition=f'NEW.{col} IS NOT NULL'+(f" AND NEW.{col}<>'{default}'" if default else '') if op=='INSERT' else f'NEW.{col} IS NOT OLD.{col}'
                fact_trigger(con,f'v04_review_{table}_{col}_{op}',table,op,condition,"'"+alias+"'",'NULL' if op=='INSERT' else f'CAST(OLD.{col} AS TEXT)',f'NEW.{col}','NEW.material_id')
        if table not in ('certificate_detail',):
            mapped={col for col,kind in fields.values()}
            for _,col,_,_,default,_ in con.execute(f'PRAGMA table_info({table})').fetchall():
                if col in mapped or col in ('material_id','notes'):continue
                if col=='certificate_kind':
                    con.execute(f"CREATE TRIGGER v04_fixed_{table}_{col} BEFORE UPDATE ON {table} WHEN NEW.{col} IS NOT OLD.{col} BEGIN SELECT RAISE(ABORT,'detail kind is immutable'); END")
                else:
                    con.execute(f"CREATE TRIGGER v04_unsupported_{table}_{col} BEFORE UPDATE ON {table} WHEN NEW.{col} IS NOT OLD.{col} BEGIN SELECT RAISE(ABORT,'field not opened for unreviewed writes'); END")
    con.execute("CREATE TRIGGER v04_person_review_status BEFORE UPDATE ON person WHEN NEW.identity_status='human_verified' AND OLD.identity_status<>'human_verified' BEGIN SELECT RAISE(ABORT,'person identity is still a candidate; no automatic human verification'); END")
    con.execute("CREATE TRIGGER v04_person_relation_review BEFORE UPDATE ON material_person WHEN NEW.relation_status='human_verified' AND OLD.relation_status<>'human_verified' BEGIN SELECT RAISE(ABORT,'relationship requires a future explicit review entry'); END")
    # Existing performance columns already have guarded writes; add view aliases without duplicate fields.
    for table,fields in {**DETAIL_FIELDS,'certificate_detail':fields_by_table['certificate_detail']}.items():
        for alias,(col,kind) in fields.items():
            if f"'{table}', '{alias}'" in oldview or f"'{table}','{alias}'" in oldview:continue
            vt='int' if kind in ('int','year') else 'amount' if kind in ('amount','signed_amount') else kind
            values=['NULL']*4;values[{'text':0,'date':1,'int':2,'amount':3}[vt]]=f'd.{col}'
            extra.append(f"SELECT m.material_id,m.material_code,'{table}','{alias}','{vt}',{','.join(values)} FROM material m JOIN {table} d USING(material_id) WHERE d.{col} IS NOT NULL")
    con.execute('DROP VIEW v_current_facts');con.execute(oldview+' UNION ALL '+' UNION ALL '.join(extra))
    # New field aliases must be audited under their public name, not their internal column name.
    for alias,(col,kind) in DETAIL_FIELDS['performance_detail'].items():
        if alias==col or alias=='contract_amount':continue
        for op in ('INSERT','UPDATE'):
            name=f'v02_review_performance_detail_{col}_{op}'
            con.execute('DROP TRIGGER IF EXISTS '+name)
            fact_trigger(con,'v04_'+name,'performance_detail',op,f'NEW.{col} IS NOT NULL' if op=='INSERT' else f'NEW.{col} IS NOT OLD.{col}',"'"+alias+"'",'NULL' if op=='INSERT' else f'CAST(OLD.{col} AS TEXT)',f'NEW.{col}','NEW.material_id')
    con.execute("UPDATE schema_meta SET meta_value=?,updated_at=? WHERE meta_key='schema_version'",(VERSION,now()))
    return True
