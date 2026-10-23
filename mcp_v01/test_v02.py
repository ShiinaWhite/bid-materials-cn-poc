"""Meaningful regression of v0.2 evidence, entity and three-grade semantics."""
import datetime as dt,json,sqlite3,sys,unittest,io
from pathlib import Path
from unittest import mock
import test_v01 as old
from jty_materials.common import Config,atomic_write,digest,now
from jty_materials.store import Store,harden,release_schema
from jty_materials.workflow import import_material,review_fact
from jty_materials.query import Query
from jty_materials.rpc import Rpc

class V02Tests(unittest.TestCase):
 setUp=old.V01Tests.setUp
 def add(self,subtype):
  p=self.base/(subtype+'.pdf');p.write_bytes(('%PDF-'+subtype).encode())
  return import_material(self.c,p,kind='certificate',subtype=subtype,title=subtype,actor='fixture',ocr_factory=lambda c:old.FakeOcr(c,'企业证照 测试文本'))['material_id']
 def review(self,mid,field,value):
  m=Query(self.c).get_material(mid);f=next(f for f in m['files'] if f['role']=='original' and f['is_current'])
  return review_fact(self.c,dict(material_id=mid,field=field,value=value,actor='fixture human',reason='isolated manual-review test',evidence_file_id=f['file_id'],evidence_sha256=f['sha256']))
 def test_strict_kind_subtype_and_db_type_binding(self):
  for kind,sub in [('certificate',None),('certificate','unsupported_cert'),('certificate','contract'),('contract','business_license')]:
   with self.assertRaises(ValueError):import_material(self.c,self.source,kind=kind,subtype=sub,title='bad',actor='test',ocr_factory=mock.Mock())
  with self.assertRaises(ValueError):import_material(self.c,self.source,material_id=1,subtype='business_license',actor='test',ocr_factory=mock.Mock())
  mid=self.add('business_license');m=Query(self.c).get_material(mid)
  self.assertEqual((m['material_type'],m['certificate_kind']),('business_license','business_license'))
  with sqlite3.connect(self.c.db_path) as db:
   for sql in ["UPDATE material SET material_type_id=22 WHERE material_id=2","UPDATE material SET subject_org_id=NULL WHERE material_id=2","UPDATE certificate_detail SET certificate_kind='safety_production' WHERE material_id=2"]:
    with self.assertRaises(sqlite3.DatabaseError):db.execute(sql)
    db.rollback()
 def test_entity_review_correction_withdrawal_and_sha_audit(self):
  mid=self.add('business_license')
  self.review(mid,'enterprise_name','测试企业');self.review(mid,'enterprise_name','修正企业');self.review(mid,'enterprise_name',None);self.review(mid,'enterprise_name','修正企业');self.review(mid,'enterprise_name','修正企业')
  self.review(mid,'unified_social_credit_code','911401000000000000');self.review(mid,'established_on','2014-09-17')
  m=Query(self.c).get_material(mid);self.assertEqual(m['verified_facts']['enterprise_name']['value'],'修正企业')
  with Store(self.c,readonly=True) as s:
   ev=[dict(r) for r in s.db.execute('SELECT * FROM verification_event WHERE material_id=?',(mid,))]
   self.assertEqual(len(ev),7);self.assertIn('retract',[e['action'] for e in ev])
   audits=[dict(r) for r in s.db.execute("SELECT * FROM audit_log WHERE entity_type='org'")];self.assertEqual(len(audits),7)
   for a in audits:self.assertEqual(json.loads(a['detail_json'])['evidence_sha256'],m['verified_facts']['enterprise_name']['evidence_sha256'])
  with sqlite3.connect(self.c.db_path) as db:
   for sql in ["UPDATE org SET name='bad'","UPDATE org SET unified_social_credit_code='bad'","UPDATE org SET established_on='2000-01-01'","DELETE FROM org","INSERT OR REPLACE INTO org SELECT * FROM org"]:
    with self.assertRaises(sqlite3.DatabaseError):db.execute(sql)
    db.rollback()
 def test_three_grades_independent_filters_and_sql_guard(self):
  mid=self.add('power_facility_license')
  for f,v in [('install_grade',2),('repair_grade',3),('test_grade',4)]:self.review(mid,f,v)
  m=Query(self.c).get_material(mid)
  self.assertEqual([m['verified_facts'][f]['value'] for f in ('install_grade','repair_grade','test_grade')],[2,3,4])
  self.assertEqual(Query(self.c).search_materials(material_type='power_facility_license',repair_grade=3,test_grade=4)['total'],1)
  self.assertEqual(Query(self.c).search_materials(material_type='power_facility_license',install_grade=3)['total'],0)
  with sqlite3.connect(self.c.db_path) as db:
   for sql in ["UPDATE power_license_item SET grade=5","DELETE FROM power_license_item","INSERT OR REPLACE INTO power_license_item SELECT * FROM power_license_item"]:
    with self.assertRaises(sqlite3.DatabaseError):db.execute(sql)
    db.rollback()
 def test_unknown_long_term_dated_and_future_are_distinct(self):
  mid=self.add('business_license')
  for f,v in [('enterprise_name','测试企业'),('unified_social_credit_code','911401000000000000'),('established_on','2014-09-17')]:self.review(mid,f,v)
  self.assertEqual(Query(self.c).get_material(mid)['validity']['status'],'unknown')
  self.assertFalse(Query(self.c).get_material(mid)['eligible_for_bid'])
  self.assertEqual(Query(self.c).search_materials(material_type='business_license',long_term_valid=False)['total'],0)
  self.review(mid,'long_term_valid',False);self.assertEqual(Query(self.c).get_material(mid)['validity']['term'],'unknown')
  self.review(mid,'long_term_valid',True);self.assertTrue(Query(self.c).get_material(mid)['eligible_for_bid'])
  self.review(mid,'long_term_valid',None);self.assertEqual(Query(self.c).get_material(mid)['validity']['status'],'unknown')
  self.review(mid,'valid_until',old.EXPIRY);self.assertEqual(Query(self.c).get_material(mid)['validity']['term'],'dated')
  self.review(mid,'valid_from',(dt.date.today()+dt.timedelta(days=5)).isoformat());self.assertEqual(Query(self.c).get_material(mid)['validity']['status'],'not_yet_valid')
  self.review(mid,'valid_from',None);self.review(mid,'valid_until',(dt.date.today()-dt.timedelta(days=1)).isoformat());self.assertEqual(Query(self.c).get_material(mid)['validity']['status'],'expired')
 def test_versions_invalidate_entity_and_all_permission_facts(self):
  for subtype,values in [('business_license',{'enterprise_name':'测试企业','unified_social_credit_code':'911401000000000000','established_on':'2014-09-17'}),('power_facility_license',{'certificate_no':'test-001','issuing_authority':'测试发证机关','valid_from':'2026-01-01','valid_until':'2032-01-01','scope':'测试范围','install_grade':3,'repair_grade':3,'test_grade':3})]:
   mid=self.add(subtype)
   for f,v in values.items():self.review(mid,f,v)
   before=Query(self.c).get_material(mid);new=self.base/(subtype+'-v2.pdf');new.write_bytes(b'%PDF-new-version')
   import_material(self.c,new,material_id=mid,actor='test',ocr_factory=lambda c:old.FakeOcr(c,'新版测试'))
   after=Query(self.c).get_material(mid);self.assertEqual(after['verified_facts'],{});self.assertEqual(after['subject_org'],before['subject_org'])
   for f in before['files']:self.assertEqual(digest(self.c.repository_root/f['relative_path']),f['sha256'])
   for f,v in values.items():self.review(mid,f,v)
   self.assertEqual(set(Query(self.c).get_material(mid)['verified_facts']),set(values))
 def test_validation_and_wrong_entity_field_rejected(self):
  mid=self.add('business_license')
  for field,value in [('unified_social_credit_code','wrong'),('established_on','2026-02-30'),('long_term_valid',1),('enterprise_name',' '),('install_grade',3),('contract_amount',5)]:
   with self.assertRaises((ValueError,TypeError)):self.review(mid,field,value)
  power=self.add('power_facility_license')
  for v in [0,6,True,3.0]:
   with self.assertRaises(ValueError):self.review(power,'install_grade',v)
 def test_audit_failure_rolls_back_org_fact_and_verification(self):
  mid=self.add('business_license')
  with sqlite3.connect(self.c.db_path) as db:db.execute("CREATE TRIGGER injected_failure BEFORE INSERT ON audit_log BEGIN SELECT RAISE(ABORT,'simulated audit failure'); END")
  with self.assertRaises(sqlite3.DatabaseError):self.review(mid,'enterprise_name','测试企业')
  with Store(self.c,readonly=True) as s:
   self.assertEqual(s.db.execute('SELECT count(*) FROM verification_event WHERE material_id=?',(mid,)).fetchone()[0],0)
   self.assertEqual(s.db.execute('SELECT name FROM org').fetchone()[0],'')
 def test_forward_migration_preserves_old_fact_history_and_is_idempotent(self):
  raw=dict(self.c.raw);raw.update(repository_root='legacy',db_path='legacy/db/materials.db',backup_dir='legacy-backups')
  path=self.base/'legacy.json';atomic_write(path,json.dumps(raw));c=Config(path);c.db_path.parent.mkdir(parents=True)
  original=c.repository_root/'original.pdf';original.write_bytes(b'%PDF-legacy');sha=digest(original)
  db=sqlite3.connect(c.db_path);db.executescript(Path(__file__).with_name('jty_materials').joinpath('schema_v1.sql').read_text(encoding='utf-8'));harden(db);release_schema(db)
  for k in ('actor','field','evidence','candidate','reason','time'):db.create_function('review_'+k,0,lambda:None)
  db.execute("INSERT INTO material(title,material_type_id,created_at,updated_at) VALUES('legacy',22,?,?)",(now(),now()));db.execute("INSERT INTO certificate_detail(material_id,certificate_kind) VALUES(1,'safety_production')")
  db.execute("INSERT INTO material_file(material_id,role,relative_path,filename,extension,sha256,size_bytes,imported_at) VALUES(1,'original','original.pdf','original.pdf','pdf',?,?,?)",(sha,original.stat().st_size,now()))
  values={'actor':'fixture','field':'valid_until','evidence':1,'candidate':None,'reason':'legacy reviewed original','time':now()}
  for k,v in values.items():db.create_function('review_'+k,0,lambda value=v:value)
  db.execute('UPDATE certificate_detail SET valid_until=? WHERE material_id=1',(old.EXPIRY,));db.commit()
  before=[list(r) for r in db.execute('SELECT * FROM verification_event')];tables=set(r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'"));db.close()
  result=Store.migrate(c);self.assertTrue(result['changed']);self.assertFalse(Store.migrate(c)['changed'])
  with Store(c,readonly=True) as s:
   self.assertEqual([list(r) for r in s.db.execute('SELECT * FROM verification_event')],before)
   self.assertEqual(set(r[0] for r in s.db.execute("SELECT name FROM sqlite_schema WHERE type='table'"))-tables,{'power_license_item','construction_qual_item','material_relation','project','project_file','project_material','project_document','project_document_run','project_document_unit'})
  self.assertEqual(Query(c).get_material(1)['verified_facts']['valid_until']['value'],old.EXPIRY)
 def test_three_mcp_tools_expose_typed_business_filter(self):
  mid=self.add('business_license');self.review(mid,'enterprise_name','测试企业')
  with Rpc([sys.executable,'-B',str(Path(__file__).with_name('serve.py')),'--config',str(self.c.path)],cwd=self.base) as rpc:
   self.assertEqual(len(rpc.call('tools/list')['tools']),3)
   self.assertEqual(rpc.tool('search_materials',{'material_type':'business_license','enterprise_name':'测试企业'})['total'],1)
   self.assertEqual(rpc.tool('get_material',{'material_id':mid})['validity']['status'],'unknown')
   self.assertTrue(rpc.tool('get_material_text',{'material_id':mid})['complete'])
   with self.assertRaises(ValueError):rpc.tool('search_materials',{'material_type':'unsupported_cert'})

if __name__=='__main__':
 sys.stdout.reconfigure(encoding='utf-8');old.BASE=Path(__file__).resolve().parents[1]/'runs'/('v02-regression-'+dt.datetime.now().strftime('%Y%m%d-%H%M%S'));old.BASE.mkdir(parents=True)
 suite=unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(old.V01Tests),unittest.defaultTestLoader.loadTestsFromTestCase(V02Tests)])
 log=io.StringIO();result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite);atomic_write(old.BASE/'tests.log',log.getvalue());print(log.getvalue())
 report={'status':'PASS' if result.wasSuccessful() else 'FAIL','tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'artifacts':str(old.BASE)}
 atomic_write(old.BASE/'result.json',json.dumps(report,indent=2));print(json.dumps(report));raise SystemExit(not result.wasSuccessful())
