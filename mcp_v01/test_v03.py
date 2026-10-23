"""Construction rows and ISO reuse: evidence, same-item filtering and migration."""
import datetime as dt,io,json,sqlite3,sys,unittest
from pathlib import Path
import test_v01 as old
from test_v02 import V02Tests
from jty_materials.common import Config,atomic_write,digest
from jty_materials.store import Store
from jty_materials.workflow import import_material
from jty_materials.query import Query
from jty_materials.rpc import Rpc

class V03Tests(unittest.TestCase):
 setUp=old.V01Tests.setUp
 add=V02Tests.add
 review=V02Tests.review
 def test_multiple_items_independent_same_row_filter_and_history(self):
  mid=self.add('construction_qual')
  for f,v in [('certificate_no','D-test'),('issuing_authority','测试厅'),('valid_until',old.EXPIRY),('qualification:电力工程施工总承包','贰级'),('qualification:机电工程施工总承包','叁级')]:self.review(mid,f,v)
  q=Query(self.c);m=q.get_material(mid)
  self.assertEqual((m['material_type'],m['certificate_kind']),('construction_qual','construction_qualification'))
  self.assertTrue(m['eligible_for_bid']);self.assertEqual(len(m['construction_qual_items']),2)
  self.assertEqual(q.search_materials(material_type='construction_qual',qualification_name='电力工程施工总承包',qualification_grade='贰级')['total'],1)
  self.assertEqual(q.search_materials(qualification_name='电力工程施工总承包',qualification_grade='叁级')['total'],0)
  for v in ['壹级',None,'贰级','贰级']:self.review(mid,'qualification:电力工程施工总承包',v)
  with Store(self.c,readonly=True) as s:
   events=list(s.db.execute("SELECT * FROM verification_event WHERE material_id=? AND field_name LIKE 'qualification:%'",(mid,)))
   self.assertEqual(len(events),6);self.assertIn('retract',[r['action'] for r in events])
   for ev in events:
    a=json.loads(s.db.execute("SELECT detail_json FROM audit_log WHERE json_extract(detail_json,'$.event_id')=?",(ev['event_id'],)).fetchone()[0]);self.assertEqual(a['evidence_sha256'],m['construction_qual_items'][0]['evidence_sha256'])
  with sqlite3.connect(self.c.db_path) as db:
   for sql in ["UPDATE construction_qual_item SET grade='特级'","UPDATE construction_qual_item SET qualification_name='bad'","DELETE FROM construction_qual_item","INSERT OR REPLACE INTO construction_qual_item SELECT * FROM construction_qual_item","INSERT INTO construction_qual_item VALUES(1,'非法类别',NULL)","UPDATE verification_event SET new_value='bad'","DELETE FROM audit_log"]:
    with self.assertRaises(sqlite3.DatabaseError):db.execute(sql)
    db.rollback()
 def test_iso_separate_materials_reuses_scope_and_date_filters(self):
  ids=[]
  for i,std in enumerate(['GB/T 19001-2016 / ISO 9001:2015','GB/T 45001-2020 / ISO 45001:2018']):
   mid=self.add('iso_cert');ids.append(mid)
   for f,v in [('certificate_no','CERT-'+str(i)),('issuing_authority','同一认证机构'),('standard',std),('scope','电力工程施工'),('issue_date','2025-09-24'),('valid_until',old.EXPIRY)]:self.review(mid,f,v)
  q=Query(self.c)
  self.assertNotEqual(*ids);self.assertEqual(q.search_materials(material_type='iso_cert',standard_contains='9001',certification_scope_contains='施工',valid_until=old.EXPIRY,eligible_only=True)['total'],1)
  self.assertEqual(q.search_materials(material_type='iso_cert',standard_contains='45001')['materials'][0]['material_id'],ids[1])
  self.assertEqual(q.search_materials(material_type='certificate')['total'],3)
  m=q.get_material(ids[0]);self.assertEqual(m['certificate_kind'],'iso');self.assertEqual(m['certification_scope'],m['verified_facts']['scope']);self.assertNotIn('valid_from',m['verified_facts'])
  self.review(ids[0],'valid_until',None);self.assertFalse(q.get_material(ids[0])['eligible_for_bid'])
  self.assertEqual(q.search_materials(material_type='iso_cert',validity_status='unknown')['total'],1)
  with sqlite3.connect(self.c.db_path) as db:
   with self.assertRaises(sqlite3.DatabaseError):db.execute("UPDATE certificate_detail SET standard='injected'")
 def test_all_new_facts_invalidate_on_version_and_rereview(self):
  for subtype,values in [('construction_qual',{'qualification:电力工程施工总承包':'贰级','qualification:机电工程施工总承包':'贰级'}),('iso_cert',{'standard':'ISO 9001:2015','scope':'施工','issue_date':'2025-09-24'})]:
   mid=self.add(subtype)
   for f,v in values.items():self.review(mid,f,v)
   before=Query(self.c).get_material(mid);src=self.base/(subtype+'-v2.pdf');src.write_bytes(b'%PDF-second-version')
   import_material(self.c,src,material_id=mid,actor='fixture',ocr_factory=lambda c:old.FakeOcr(c,'新版'))
   m=Query(self.c).get_material(mid);self.assertEqual(m['verified_facts'],{});self.assertEqual(m['construction_qual_items'],[])
   self.assertEqual(Query(self.c).search_materials(**({'qualification_grade':'贰级'} if subtype=='construction_qual' else {'standard_contains':'9001'}))['total'],0)
   for f in before['files']:self.assertEqual(digest(self.c.repository_root/f['relative_path']),f['sha256'])
   for f,v in values.items():self.review(mid,f,v)
   m=Query(self.c).get_material(mid);self.assertEqual({f:v['value'] for f,v in m['verified_facts'].items()},values)
   self.assertTrue(all(v['evidence_file_id'] not in {f['file_id'] for f in before['files']} for v in m['verified_facts'].values()))
 def test_v02_forward_migration_preserves_history_and_one_table(self):
  # Build the real pinned v0.2 schema without requiring acceptance-run artifacts.
  from jty_materials.store import harden,release_schema
  from jty_materials.schema_v02 import upgrade as v02
  from jty_materials.common import now
  from jty_materials.fields import org_norm
  raw=dict(self.c.raw);raw.update(repository_root='legacy',db_path='legacy/db/materials.db',backup_dir='legacy-backups')
  cp=self.base/'legacy.json';atomic_write(cp,json.dumps(raw));c=Config(cp)
  c.db_path.parent.mkdir(parents=True);original=c.repository_root/'original.pdf';original.write_bytes(b'%PDF-v02-original')
  with sqlite3.connect(c.db_path) as db:
   db.execute('PRAGMA foreign_keys=ON');db.execute('PRAGMA journal_mode=WAL')
   db.executescript(Path(__file__).with_name('jty_materials').joinpath('schema_v1.sql').read_text(encoding='utf-8'));harden(db);release_schema(db);v02(db)
   context=dict(actor='fixture human',field='valid_until',evidence=1,candidate=None,reason='v02 reviewed original',time=now(),material=1,sha256=digest(original),action='review')
   for k,v in context.items():db.create_function('review_'+k,0,lambda value=v:value)
   db.create_function('org_norm',1,org_norm)
   db.execute("INSERT INTO material(title,material_type_id,created_at,updated_at) VALUES('v02',22,?,?)",(now(),now()));db.execute("INSERT INTO certificate_detail(material_id,certificate_kind) VALUES(1,'safety_production')")
   db.execute("INSERT INTO material_file(material_id,role,relative_path,filename,extension,sha256,size_bytes,imported_at) VALUES(1,'original','original.pdf','original.pdf','pdf',?,?,?)",(digest(original),original.stat().st_size,now()))
   db.execute('UPDATE certificate_detail SET valid_until=? WHERE material_id=1',(old.EXPIRY,))
   tables={r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")};history=list(db.execute('SELECT * FROM verification_event'));audits=list(db.execute('SELECT * FROM audit_log'))
  r=Store.migrate(c);self.assertTrue(r['changed']);self.assertFalse(Store.migrate(c)['changed'])
  with Store(c,readonly=True) as s:
   self.assertEqual({r[0] for r in s.db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}-tables,{'construction_qual_item','material_relation','project','project_file','project_material'})
   self.assertEqual(s.db.execute('PRAGMA foreign_key_check').fetchall(),[])
   self.assertEqual([tuple(r) for r in s.db.execute('SELECT * FROM verification_event')],history)
   self.assertEqual([tuple(r) for r in s.db.execute('SELECT * FROM audit_log ORDER BY log_id LIMIT ?',(len(audits),))],audits)
  self.assertEqual(Query(c).get_material(1)['verified_facts']['valid_until']['value'],old.EXPIRY)
 def test_new_protocol_filters_and_wrong_types_rejected(self):
  construction=self.add('construction_qual');iso=self.add('iso_cert')
  for mid,f,v in [(construction,'qualification:电力工程施工总承包','贰级'),(iso,'standard','ISO 9001:2015'),(iso,'scope','施工')]:self.review(mid,f,v)
  for mid,f,v in [(iso,'qualification:电力工程施工总承包','贰级'),(construction,'standard','ISO 9001'),(iso,'qualification:','贰级'),(construction,'qualification: bad','贰级')]:
   with self.assertRaises(ValueError):self.review(mid,f,v)
  with Rpc([sys.executable,'-B',str(Path(__file__).with_name('serve.py')),'--config',str(self.c.path)],cwd=self.base) as rpc:
   self.assertEqual(len(rpc.call('tools/list')['tools']),3)
   self.assertEqual(rpc.tool('search_materials',dict(material_type='construction_qual',qualification_name='电力工程施工总承包',qualification_grade='贰级'))['total'],1)
   self.assertEqual(rpc.tool('search_materials',dict(material_type='iso_cert',standard_contains='9001',certification_scope_contains='施工'))['total'],1)
   self.assertEqual(rpc.tool('get_material',dict(material_id=construction))['construction_qual_items'][0]['grade'],'贰级')
   self.assertTrue(rpc.tool('get_material_text',dict(material_id=iso))['complete'])

def load_tests(loader,tests,pattern):return loader.loadTestsFromTestCase(V03Tests)

if __name__=='__main__':
 sys.stdout.reconfigure(encoding='utf-8');old.BASE=Path(__file__).resolve().parents[1]/'runs'/('v03-tests-'+dt.datetime.now().strftime('%Y%m%d-%H%M%S'));old.BASE.mkdir(parents=True)
 suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls) for cls in [old.V01Tests,V02Tests,V03Tests])
 log=io.StringIO();result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite);atomic_write(old.BASE/'tests.log',log.getvalue());print(log.getvalue())
 report=dict(status='PASS' if result.wasSuccessful() else 'FAIL',tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),artifacts=str(old.BASE))
 atomic_write(old.BASE/'result.json',json.dumps(report,indent=2));print(json.dumps(report));raise SystemExit(not result.wasSuccessful())
