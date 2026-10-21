import io,json,shutil,sqlite3,sys,unittest
from pathlib import Path
from unittest import mock
import test_v01 as old
from jty_materials.common import Config,atomic_write,digest,now
from jty_materials.store import Store,SCHEMA_VERSION
from jty_materials.workflow import import_material,review_fact
from jty_materials.query import Query
from jty_materials.fields import ALLOWED
from jty_materials.phase_ocr import PhaseOcr

class PhaseTests(unittest.TestCase):
 setUp=old.V01Tests.setUp
 def add(self,typ,text,meta=None):
  source=self.base/(typ+'.pdf');source.write_bytes(('%PDF-fixture-'+typ).encode())
  return import_material(self.c,source,kind='material',subtype=typ,title=typ,actor='fixture',source_metadata=meta or {'source_relative_path':'2024/'+typ},ocr_factory=lambda c:old.FakeOcr(c,text))
 def test_all_business_types_allow_pending_originals(self):
  for typ in ['award_notice','acceptance_doc','person_title_cert','person_document','patent','software_copyright','audit_report','tax_credit','honor_cert','other']:
   r=self.add(typ,'测试原文 专利证书 有效期至2028年10月07日')
   m=Query(self.c).get_material(r['material_id']);self.assertEqual(m['verified_facts'],{});self.assertFalse(m['eligible_for_bid'])
   self.assertTrue(all(f['integrity_ok'] for f in m['files']))
 def test_contract_candidate_query_is_explicit_not_human_verified(self):
  r=self.add('contract','工程10kV 项目\n发包人:测试建设有限公司\n合同编号:TEST-100\n合同金额:695000.00元',{'source_relative_path':'projects/a.pdf','project_name':'示例10kV工程'})
  q=Query(self.c);self.assertEqual(q.search_materials(material_type='contract',voltage_level_kv=10)['total'],0)
  self.assertEqual(q.search_materials(material_type='contract',voltage_level_kv=10,fact_status='candidate')['total'],1)
  self.assertEqual(q.search_materials(material_type='contract',project_name='10kV',fact_status='any')['total'],1)
 def test_person_material_chain_is_candidate_with_current_evidence(self):
  r=self.add('person_registered_cert','证书编号:TEST-ZC-100\n注册专业:机电工程\n有效期至2028年10月07日',dict(source_relative_path='人员/测试人/注册.pdf',person_label='测试人',person_source_key='人员/测试人',person_relation_status='candidate'))
  q=Query(self.c);m=q.get_material(r['material_id']);self.assertEqual(m['persons'][0]['name'],'测试人');self.assertEqual(m['persons'][0]['identity_status'],'candidate')
  self.assertEqual(q.search_materials(person_name='测试人',fact_status='any')['total'],1)
  self.assertEqual(q.search_materials(person_name='测试人')['total'],0)
 def test_person_filter_literal_name_same_holder_and_current_evidence(self):
  a=self.add('person_registered_cert','证书编号:TEST-A',dict(source_relative_path='a.pdf',person_label='测试%甲',person_source_key='a',person_relation_status='candidate'))
  b=self.add('person_title_cert','证书编号:TEST-B',dict(source_relative_path='b.pdf',person_label='测试乙',person_source_key='b',person_relation_status='candidate'))
  q=Query(self.c);pa=q.get_material(a['material_id'])['persons'][0]['person_id'];pb=q.get_material(b['material_id'])['persons'][0]['person_id']
  self.assertEqual(q.search_materials(person_name='%',fact_status='any')['total'],1)
  with Store(self.c) as s:
   s.db.execute("INSERT INTO material_person(material_id,person_id,role_on_material,is_primary,relation_status,evidence_file_id) VALUES(?,?,'holder',0,'candidate',?)",(a['material_id'],pb,a['files']['original']));s.db.commit()
  self.assertEqual(q.search_materials(person_name='测试%甲',person_id=pb,fact_status='any')['total'],0)
  self.assertEqual(q.search_materials(person_id=pa,fact_status='verified')['total'],0)
  source=self.base/'person-new.pdf';source.write_bytes(b'new person scan')
  import_material(self.c,source,material_id=a['material_id'],actor='fixture',ocr_factory=lambda c:old.FakeOcr(c,'新证据'))
  with Store(self.c) as s:
   s.db.execute('UPDATE material_person SET evidence_file_id=? WHERE material_id=?',(a['files']['original'],a['material_id']));s.db.commit()
  self.assertEqual(q.search_materials(person_id=pa,fact_status='any')['total'],0)
 def test_patent_and_software_fields_without_legal_inference(self):
  p=self.add('patent','实用新型专利\n专利名称:示例电缆保护器\n专利权人:测试公司\nZL202123454066.2')
  self.assertEqual(Query(self.c).get_material(p['material_id'])['legal_status']['value'],'unknown')
  self.assertEqual(Query(self.c).search_materials(material_type='patent',patent_name='电缆',fact_status='any')['total'],1)
  r=self.add('software_copyright','软件名称:工程管理软件\n登记号:2024SR1234567\n著作权人:测试公司\n登记日期:2024年01月03日')
  self.assertEqual(Query(self.c).search_materials(material_type='software_copyright',registration_no='2024SR1234567',fact_status='any')['total'],1)
 def test_person_version_keeps_id_and_demotes_relation_to_pending(self):
  r=self.add('person_registered_cert','证书编号:TEST-ZC-100',dict(source_relative_path='人员/测试人/注册.pdf',person_label='测试人',person_source_key='人员/测试人',person_relation_status='candidate'))
  before=Query(self.c).get_material(r['material_id']);source=self.base/'person-next.pdf';source.write_bytes(b'changed person original')
  newer=import_material(self.c,source,material_id=r['material_id'],actor='fixture',ocr_factory=lambda c:old.FakeOcr(c,'证书编号:TEST-ZC-100'))
  after=Query(self.c).get_material(r['material_id'])
  self.assertEqual(after['persons'][0]['person_id'],before['persons'][0]['person_id'])
  self.assertEqual(after['persons'][0]['relation_status'],'relation_pending')
  self.assertEqual(after['persons'][0]['evidence_file_id'],newer['files']['original'])
  self.assertEqual(len(after['files']),8)
 def test_audit_year_query_and_reviewed_financial_values(self):
  r=self.add('audit_report','2024年度财务报表\n金额单位:元\n营业收入:123456.00',dict(source_relative_path='财务/2024/a.pdf',fiscal_year_hint=2024))
  q=Query(self.c);self.assertEqual(q.search_materials(material_type='audit_report',fiscal_year=2024,fact_status='candidate')['total'],1)
  for field,value in [('fiscal_year',2024),('revenue',12345600)]:
   review_fact(self.c,dict(material_id=r['material_id'],field=field,value=value,actor='fixture human',reason='fixture only review',evidence_file_id=r['files']['original'],evidence_sha256=r['original_sha256']))
  self.assertEqual(q.get_material(r['material_id'])['verified_facts']['revenue']['value'],12345600)
  with Store(self.c) as s:
   with self.assertRaises(sqlite3.DatabaseError):s.db.execute('UPDATE audit_report_detail SET revenue_cents=1 WHERE material_id=?',(r['material_id'],))
 def test_upgrade_v03_preserves_manual_history_and_is_idempotent(self):
  from jty_materials.store import harden,release_schema
  from jty_materials.schema_v03 import upgrade as v03
  from jty_materials.fields import org_norm
  target=self.c.db_path.with_name('old-copy.db');self.assertFalse(target.exists())
  original=self.c.materials/'v03-fixture.pdf';original.write_bytes(b'%PDF-legacy-v03-fixture')
  db=sqlite3.connect(target)
  try:
   db.execute('PRAGMA foreign_keys=ON');db.executescript(Path(__file__).with_name('jty_materials').joinpath('schema_v1.sql').read_text(encoding='utf-8'));harden(db);release_schema(db);v03(db)
   context=dict(actor='fixture human',field='valid_until',evidence=1,candidate=None,reason='isolated v03 manual event',time=now(),material=1,sha256=digest(original),action='review')
   for k,v in context.items():db.create_function('review_'+k,0,lambda value=v:value)
   db.create_function('org_norm',1,org_norm)
   db.execute("INSERT INTO material(title,material_type_id,created_at,updated_at) VALUES('legacy fixture',(SELECT type_id FROM material_type WHERE code='safety_prod_license'),?,?)",(now(),now()))
   db.execute("INSERT INTO certificate_detail(material_id,certificate_kind) VALUES(1,'safety_production')")
   db.execute("INSERT INTO material_file(material_id,role,relative_path,filename,extension,sha256,size_bytes,imported_at) VALUES(1,'original','materials/v03-fixture.pdf','v03-fixture.pdf','pdf',?,?,?)",(digest(original),original.stat().st_size,now()))
   db.execute('UPDATE certificate_detail SET valid_until=? WHERE material_id=1',(old.EXPIRY,));db.commit()
   history=list(db.execute('SELECT * FROM verification_event'))
  finally:db.close()
  raw=dict(self.c.raw,db_path='repository/db/old-copy.db');cp=self.base/'old-config.json';atomic_write(cp,json.dumps(raw));c=Config(cp)
  first=Store.migrate(c);second=Store.migrate(c);self.assertTrue(first['changed']);self.assertFalse(second['changed'])
  with Store(c,readonly=True) as s:
   self.assertEqual([tuple(r) for r in s.db.execute('SELECT * FROM verification_event')],history)
   self.assertFalse(s.db.execute('PRAGMA foreign_key_check').fetchall());self.assertEqual(s.db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
  self.assertEqual(Query(c).get_material(1)['verified_facts']['valid_until']['value'],old.EXPIRY)
 def test_native_office_extraction_preserves_original_bytes(self):
  from docx import Document
  p=self.base/'source.docx';doc=Document();doc.add_paragraph('软件名称:工程管理软件');doc.save(p);sha=digest(p)
  r=import_material(self.c,p,kind='material',subtype='software_copyright',title='native docx',actor='fixture',source_metadata={'source_relative_path':'source.docx'},ocr_factory=PhaseOcr)
  self.assertEqual(r['original_sha256'],sha);self.assertEqual(digest(p),sha);self.assertIn('工程管理软件',Query(self.c).get_material_text(r['material_id'])['text'])
 def test_unreadable_container_is_preserved_and_marked(self):
  p=self.base/'unknown.aip';p.write_bytes(b'opaque container');sha=digest(p)
  r=import_material(self.c,p,kind='material',subtype='other',title='unknown container',actor='fixture',source_metadata={'source_relative_path':'unknown.aip'},ocr_factory=PhaseOcr)
  self.assertEqual(r['original_sha256'],sha)
  with Store(self.c,readonly=True) as s:self.assertEqual(s.db.execute('SELECT status FROM extraction_run WHERE material_id=?',(r['material_id'],)).fetchone()[0],'partial')
 def test_financial_candidates_require_page_units_and_column_evidence(self):
  from jty_materials.enrichment import financial_candidates
  def block(text,x,y,w=100):return dict(text=text,box=[[x,y],[x+w,y],[x+w,y+20],[x,y+20]])
  page=dict(page=2,response=dict(data=[block('单位:元',0,0),block('本年金额',300,40),block('上年金额',500,40),block('营业收入',0,100),block('123,456.00',300,100),block('98,000.00',500,100)]))
  found=financial_candidates([page],None);self.assertEqual(found[0]['value'],12345600);self.assertEqual(found[0]['page'],2)
  no_unit=dict(page=3,response=dict(data=page['response']['data'][1:]));self.assertEqual(financial_candidates([no_unit],None),[])
  no_header=dict(page=4,response=dict(data=[page['response']['data'][0]]+page['response']['data'][3:]));self.assertEqual(financial_candidates([no_header],None),[])
  for data in ['No text found',None,{},['not a block',dict(text='bad coordinates',box=[[0,'x']]*4)]]:
   with self.subTest(umi_data=data):self.assertEqual(financial_candidates([dict(page=5,response=dict(code=101,data=data))],None),[])
 def test_voltage_candidates_do_not_confuse_kva_or_fractional_kv(self):
  from jty_materials.business import extract_candidates
  values=[v for f,t,r,v in extract_candidates('1000kVA箱变 0.4kV线路 10kV工程','contract',{}) if f=='voltage_level_kv']
  self.assertEqual(values,['10'])
 def test_machine_visual_candidate_never_promotes_to_verified(self):
  from jty_materials.candidates import append_candidates
  r=self.add('audit_report','2024年度财务报告',dict(source_relative_path='2024/a.pdf',fiscal_year_hint=2024))
  record=dict(material_id=r['material_id'],evidence_file_id=r['files']['original'],evidence_sha256=r['original_sha256'],actor='fixture machine',extractor='machine-visual-candidate',candidates=[dict(field='revenue',value=12345600,page=7,evidence='本年累计数、单位元；机器识读，未人工核验')])
  self.assertEqual(append_candidates(self.c,record)['added'],1);self.assertEqual(append_candidates(self.c,record)['added'],0)
  q=Query(self.c);self.assertEqual(q.get_material(r['material_id'])['verified_facts'],{});self.assertEqual(q.search_materials(revenue=12345600,fact_status='candidate')['total'],1)
  record['evidence_sha256']='0'*64
  with self.assertRaises(ValueError):append_candidates(self.c,record)
 def test_candidate_long_term_never_changes_unknown_validity(self):
  from jty_materials.candidates import append_candidates
  r=self.add('business_license','测试营业执照')
  record=dict(material_id=r['material_id'],evidence_file_id=r['files']['original'],evidence_sha256=r['original_sha256'],actor='fixture machine',extractor='fixture-only-candidate',candidates=[dict(field='long_term_valid',value=True,page=1,evidence='fixture candidate only')])
  append_candidates(self.c,record);q=Query(self.c)
  self.assertEqual(q.search_materials(long_term_valid=True,fact_status='candidate')['total'],1)
  m=q.get_material(r['material_id']);self.assertEqual(m['validity']['status'],'unknown');self.assertFalse(m['eligible_for_bid'])
 def test_opened_types_manual_review_version_and_audit_guards(self):
  from jty_materials.fields import field_spec
  cases={'person_registered_cert':{'certificate_no':'REG-TEST','grade':'二级'},'patent':{'patent_no':'ZL202123454066.2','patent_type':'utility_model'},'software_copyright':{'registration_no':'2024SR1234567','owner':'测试公司'},'tax_credit':{'year':2024,'rating':'A'},'honor_cert':{'title':'测试荣誉','issue_date':'2024-01-01'}}
  for typ,fields in cases.items():
   with self.subTest(material_type=typ):
    r=self.add(typ,'测试机器候选 原件')
    for field,value in fields.items():
     review_fact(self.c,dict(material_id=r['material_id'],field=field,value=value,actor='fixture human',reason='isolated type audit review',evidence_file_id=r['files']['original'],evidence_sha256=r['original_sha256']))
    q=Query(self.c);self.assertEqual({f:v['value'] for f,v in q.get_material(r['material_id'])['verified_facts'].items()},fields)
    with Store(self.c) as s:
     field=next(iter(fields));table,col,_=field_spec(field,typ)
     with self.assertRaises(sqlite3.DatabaseError):s.db.execute('UPDATE '+table+' SET '+col+'=NULL WHERE material_id=?',(r['material_id'],))
    source=self.base/(typ+'-new.pdf');source.write_bytes(('new version '+typ).encode())
    newer=import_material(self.c,source,material_id=r['material_id'],actor='fixture',ocr_factory=lambda c:old.FakeOcr(c,'新版本 同证测试'))
    self.assertEqual(q.get_material(r['material_id'])['verified_facts'],{})
    field,value=next(iter(fields.items()))
    review_fact(self.c,dict(material_id=r['material_id'],field=field,value=value,actor='fixture human',reason='same-value review on new evidence',evidence_file_id=newer['files']['original'],evidence_sha256=newer['original_sha256']))
    self.assertEqual(q.get_material(r['material_id'])['verified_facts'][field]['value'],value)
    with Store(self.c,readonly=True) as s:
     self.assertEqual(s.db.execute('SELECT count(*) FROM verification_event WHERE material_id=?',(r['material_id'],)).fetchone()[0],len(fields)+1)
     audited=s.db.execute("SELECT count(*) FROM verification_event v WHERE v.material_id=? AND EXISTS(SELECT 1 FROM audit_log a WHERE json_extract(a.detail_json,'$.event_id')=v.event_id AND json_extract(a.detail_json,'$.evidence_file_id')=v.evidence_file_id AND a.action='fact.'||v.action)",(r['material_id'],)).fetchone()[0]
     self.assertEqual(audited,len(fields)+1)
 def test_projection_repair_preserves_version_and_avoids_unchanged_rewrite(self):
  from jty_materials.workflow import rebuild_projections
  p=self.c.materials/'MAT-000001/current.search.txt';p.write_text('damaged derivative',encoding='utf-8')
  rebuild_projections(self.c);before=p.stat().st_mtime_ns
  self.assertEqual(p.read_text(encoding='utf-8'),old.normalize(self.text))
  rebuild_projections(self.c);self.assertEqual(p.stat().st_mtime_ns,before)
  self.assertTrue(all(f['integrity_ok'] for f in Query(self.c).get_material(1)['files']))

if __name__=='__main__':unittest.main(verbosity=2)
