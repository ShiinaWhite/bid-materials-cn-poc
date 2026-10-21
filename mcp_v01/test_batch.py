"""Batch safety tests, using isolated repositories and deterministic OCR faults."""
import datetime as dt,io,json,sqlite3,sys,unittest
from pathlib import Path
from unittest import mock
import test_v01 as old
from test_v02 import V02Tests
from test_v03 import V03Tests
from jty_materials import batch
from jty_materials.common import Config,atomic_write,digest,normalize,now
from jty_materials.store import Store
from jty_materials.workflow import import_material,review_fact
from jty_materials.query import Query
from jty_materials.backup import create_backup,restore_backup
from jty_materials.ocr import Ocr

class BatchTests(unittest.TestCase):
 setUp=old.V01Tests.setUp
 review=V02Tests.review
 def make(self,names=('new.jpeg',),batch_id='B-test',type_code='iso_cert'):
  queue=self.base/('queue-'+batch_id);queue.mkdir()
  for name in names:(queue/name).write_bytes(b'original-'+name.encode())
  inv=batch.inventory(self.c,queue);ip=self.base/(batch_id+'-inventory.json');batch.write(ip,inv)
  required=['certificate_no','standard','scope','issuing_authority','valid_until','issue_date']
  if type_code=='business_license':required=['enterprise_name','unified_social_credit_code','established_on']
  decisions=[dict(item_id=r['item_id'],action='new_material',material_type=type_code,title=r['filename'],reason='fixture human identity decision',original_confirmed=True,
      identity_review={'relation':'different_certificate','reviewer':'fixture human'},required_verified_fields=required,mcp_search={'material_type':type_code}) for r in inv['items']]
  if type_code=='business_license':
   for d in decisions:d.update(review_unknown_fields=['valid_until'],unknown_term_reason='human sees no term printed on this unchanged original')
  plan=dict(batch_id=batch_id,inventory_sha256=digest(ip),approved_by='fixture human',approved_at=now(),approval_evidence='isolated fixture human confirmation',items=decisions)
  pp=self.base/(batch_id+'-plan.json');batch.write(pp,plan);batch.create(self.c,ip,pp)
  return queue,ip,pp
 def transport(self,c,action,payload=None):
  if action=='backup':return create_backup(c,**(payload or {}))
  if action=='index':return {'healthy':True}
  if action=='batch_import':return import_material(c,**payload,ocr_factory=lambda c:old.FakeOcr(c,'体系测试 ３５ｋＶ'))
  raise AssertionError(action)
 def fake_rpc(self,*args,**kw):
  c=self.c
  class Wire:
   def __enter__(self):return self
   def __exit__(self,*args):pass
   def tool(self,name,args):
    q=Query(c)
    if name=='get_material':return q.get_material(**args)
    if name=='get_material_text':return q.get_material_text(**args)
    return q.search_materials(**args)
  return Wire()
 def test_inventory_readonly_and_sensitive_filename_not_deferred(self):
  q=self.base/'inventory-source';q.mkdir();(q/'安全生产_联系电话_银行账户.pdf').write_bytes(self.source.read_bytes());(q/'其他身份证明.png').write_bytes(b'not inspected')
  before=digest(self.c.db_path)
  with mock.patch.object(Ocr,'start',side_effect=AssertionError('inventory must not OCR')):inv=batch.inventory(self.c,q)
  self.assertEqual(digest(self.c.db_path),before)
  duplicate=next(r for r in inv['items'] if r['possible_already_registered']);self.assertEqual(duplicate['suggested_action'],'duplicate')
  self.assertTrue(all(r['sensitive_information']=='unknown' and r['suggested_action']!='defer' for r in inv['items']))
  outside=self.base/'outside';outside.mkdir()
  with self.assertRaises(ValueError):batch.inventory(self.c,q,limit=1)
 def test_strict_plan_gate_types_required_fields_and_tampering(self):
  _,ip,pp=self.make();p=batch.read(pp);p.update(batch_id='B-unapproved',approved_by=None);bad=self.base/'bad.json';batch.write(bad,p)
  with self.assertRaises(ValueError):batch.create(self.c,ip,bad)
  p['approved_by']='fixture';p['items'][0]['material_type']='contract';batch.write(bad,p)
  with self.assertRaises(ValueError):batch.create(self.c,ip,bad)
  p['items'][0]['material_type']='iso_cert';p['items'][0]['required_verified_fields']=['valid_until'];batch.write(bad,p)
  with self.assertRaises(ValueError):batch.create(self.c,ip,bad)
  directory=batch.batch_dir(self.c,'B-test');(directory/'plan.json').write_text('{}')
  with self.assertRaises(ValueError):batch.execute(self.c,'B-test',self.transport)
 def test_one_failed_ocr_does_not_pollute_another_item(self):
  self.make(('a.jpeg','b.jpeg'))
  def transport(c,action,payload=None):
   if action=='batch_import' and payload['batch_context']['item_id']=='I01':
    class Broken(old.FakeOcr):
     def document(self,p):raise RuntimeError('injected OCR failure before transaction')
    return import_material(c,**payload,ocr_factory=lambda c:Broken(c,''))
   return self.transport(c,action,payload)
  m=batch.execute(self.c,'B-test',transport);self.assertEqual(m['status'],'partial');self.assertEqual(m['failed_items'],1)
  self.assertEqual([i['status'] for i in m['items']],['failed','awaiting_review'])
  with Store(self.c,readonly=True) as s:self.assertEqual(s.db.execute('SELECT count(*) FROM material').fetchone()[0],2)
  m=batch.execute(self.c,'B-test',self.transport);self.assertEqual(m['failed_items'],0);self.assertEqual(m['failed_attempts'],1)
  with Store(self.c,readonly=True) as s:self.assertEqual(s.db.execute('SELECT count(*) FROM material').fetchone()[0],3)
 def test_lost_commit_reply_is_reconciled_without_duplicate_id(self):
  self.make()
  def transport(c,action,payload=None):
   value=self.transport(c,action,payload)
   if action=='batch_import':raise TimeoutError('reply lost AFTER SQLite commit')
   return value
  m=batch.execute(self.c,'B-test',transport);self.assertEqual(m['items'][0]['status'],'registered_needs_index')
  m=batch.execute(self.c,'B-test',self.transport);self.assertEqual(m['items'][0]['status'],'awaiting_review')
  with Store(self.c,readonly=True) as s:
   self.assertEqual(s.db.execute('SELECT count(*) FROM material').fetchone()[0],2)
   self.assertEqual(s.db.execute("SELECT count(*) FROM audit_log WHERE action='batch.item.commit'").fetchone()[0],1)
 def test_process_death_after_sql_commit_before_manifest_publish(self):
  self.make();real=batch.save
  def crash(directory,m,event,**kw):
   if event=='item.registered':raise SystemExit('simulated process death before state publish')
   return real(directory,m,event,**kw)
  with mock.patch.object(batch,'save',crash),self.assertRaises(SystemExit):batch.execute(self.c,'B-test',self.transport)
  self.assertEqual(batch.status(self.c,'B-test')['items'][0]['status'],'committing')
  m=batch.execute(self.c,'B-test',self.transport);self.assertEqual(m['items'][0]['status'],'awaiting_review')
  with Store(self.c,readonly=True) as s:self.assertEqual(s.db.execute('SELECT count(*) FROM material').fetchone()[0],2)
 def test_same_sha_in_batch_deduplicates_after_first_commit(self):
  q,ip,pp=self.make(('a.jpeg','b.jpeg'))
  # Rebuild a separate approved inventory after making byte-identical source copies.
  (q/'b.jpeg').write_bytes((q/'a.jpeg').read_bytes());inv=batch.inventory(self.c,q);batch.write(ip,inv)
  plan=batch.read(pp);plan.update(batch_id='B-dups',inventory_sha256=digest(ip));batch.write(pp,plan);batch.create(self.c,ip,pp)
  m=batch.execute(self.c,'B-dups',self.transport);self.assertEqual(m['skipped_items'],1)
  self.assertEqual(m['items'][0]['receipt']['material_id'],m['items'][1]['receipt']['material_id'])
  with Store(self.c,readonly=True) as s:self.assertEqual(s.db.execute('SELECT count(*) FROM material').fetchone()[0],2)
 def test_changed_source_and_original_bytes_preserved(self):
  q,_,_=self.make();original=q/'new.jpeg';sha=digest(original)
  original.write_bytes(b'changed after approval');m=batch.execute(self.c,'B-test',self.transport);self.assertEqual(m['failed_items'],1)
  with Store(self.c,readonly=True) as s:self.assertEqual(s.db.execute('SELECT count(*) FROM material').fetchone()[0],1)
  original.write_bytes(b'original-new.jpeg');self.assertEqual(digest(original),sha)
  m=batch.execute(self.c,'B-test',self.transport);mid=m['items'][0]['receipt']['material_id'];f=next(f for f in Query(self.c).get_material(mid)['files'] if f['role']=='original')
  self.assertEqual(f['sha256'],sha);self.assertTrue(f['relative_path'].endswith('original.jpeg'))
  self.assertEqual((self.c.repository_root/f['relative_path']).read_bytes(),original.read_bytes())
 def test_native_image_ocr_requires_success_and_keeps_raw_nfkc_separate(self):
  source=self.base/'image.png';source.write_bytes(b'unchanged image fixture');sha=digest(source);o=Ocr(self.c)
  with mock.patch.object(o,'request',return_value={'code':100,'data':[{'text':'ＡＢＣ ３５ｋＶ'}]}):result=o.document(source)
  self.assertEqual(Path(result['raw']).read_text(encoding='utf-8'),'ＡＢＣ ３５ｋＶ')
  self.assertEqual(Path(result['search']).read_text(encoding='utf-8'),'ABC 35kV');self.assertEqual(digest(source),sha)
  for result in [{'code':101,'data':[]},{'code':100,'data':[]},{'code':100,'data':[{'text':''}]},{'code':100,'data':[{}]}]:
   with mock.patch.object(o,'request',return_value=result),self.assertRaises(RuntimeError):o.document(source)
 def test_close_requires_review_and_backup_and_restores_batch_metadata(self):
  self.make();m=batch.execute(self.c,'B-test',self.transport);mid=m['items'][0]['receipt']['material_id']
  with self.assertRaises(ValueError):batch.close(self.c,'B-test',transport=self.transport)
  for f,v in dict(certificate_no='TEST',standard='ISO 14001:2015',scope='工程施工',issuing_authority='fixture',issue_date='2025-09-24',valid_until=old.EXPIRY).items():self.review(mid,f,v)
  with mock.patch('jty_materials.rpc.Rpc',self.fake_rpc):m=batch.validate_items(self.c,'B-test')
  self.assertEqual(m['completed_items'],1)
  m=batch.close(self.c,'B-test',transport=self.transport);self.assertEqual(m['status'],'completed')
  backup=Path(m['backup_after']['backup']);manifest=batch.read(backup/'manifest.json')
  relative='batches/B-test/manifest.json';self.assertIn(relative,[f['relative_path'] for f in manifest['files']])
  self.assertEqual(batch.read(backup/'files'/relative),m)
  with self.assertRaises(ValueError):batch.execute(self.c,'B-test',self.transport)
  with self.assertRaises(ValueError):batch.validate_items(self.c,'B-test')
  def runtime(umi,dowse,dest):Path(dest).mkdir(parents=True);return 'http://127.0.0.1:1224'
  with mock.patch('jty_materials.setup.copy_runtime',runtime):restored=restore_backup(backup,self.base/'restore')
  c=Config(restored['config']);self.assertEqual(Query(c).get_material(mid)['verified_facts']['standard']['value'],'ISO 14001:2015')
  self.assertEqual(batch.read(c.repository_root/relative),m)
 def test_unknown_business_term_completes_intake_without_becoming_valid(self):
  self.make(type_code='business_license');m=batch.execute(self.c,'B-test',self.transport);mid=m['items'][0]['receipt']['material_id']
  for f,v in dict(enterprise_name='测试企业',unified_social_credit_code='911401000000000000',established_on='2014-09-17').items():self.review(mid,f,v)
  with mock.patch('jty_materials.rpc.Rpc',self.fake_rpc):m=batch.validate_items(self.c,'B-test')
  self.assertEqual(m['completed_items'],1);current=Query(self.c).get_material(mid);self.assertFalse(current['eligible_for_bid']);self.assertEqual(current['validity']['status'],'unknown')

 def test_post_backup_failure_never_reports_completed_and_can_resume_close(self):
  self.make();m=batch.execute(self.c,'B-test',self.transport);mid=m['items'][0]['receipt']['material_id']
  for f,v in dict(certificate_no='TEST',standard='ISO 14001',scope='施工',issuing_authority='fixture',issue_date='2025-09-24',valid_until=old.EXPIRY).items():self.review(mid,f,v)
  with mock.patch('jty_materials.rpc.Rpc',self.fake_rpc):batch.validate_items(self.c,'B-test')
  def failed(c,action,payload=None):
   if action=='backup' and payload:raise OSError('post-backup disk fault')
   return self.transport(c,action,payload)
  with self.assertRaises(OSError):batch.close(self.c,'B-test',transport=failed)
  m=batch.status(self.c,'B-test');self.assertEqual(m['status'],'partial');self.assertIn('disk fault',m['close_error'])
  m=batch.close(self.c,'B-test',transport=self.transport);self.assertEqual(m['status'],'completed');self.assertNotIn('close_error',m)

 def test_historical_sha_duplicate_does_not_reactivate_old_version(self):
  oldsha=digest(self.source);new=self.base/'v2.pdf';new.write_bytes(b'%PDF-new-current')
  import_material(self.c,new,material_id=1,actor='fixture',ocr_factory=lambda c:old.FakeOcr(c,'new'))
  queue=self.base/'duplicate-queue';queue.mkdir();(queue/'copy.pdf').write_bytes(self.source.read_bytes())
  inv=batch.inventory(self.c,queue);self.assertEqual(inv['items'][0]['suggested_action'],'duplicate');self.assertFalse(inv['items'][0]['exact_original_matches'][0]['is_current'])
  ip=self.base/'duplicate-inventory.json';batch.write(ip,inv);pp=self.base/'duplicate-plan.json'
  batch.write(pp,dict(batch_id='B-duplicate',inventory_sha256=digest(ip),approved_by='fixture',approved_at=now(),approval_evidence='fixture',items=[dict(item_id='I01',action='duplicate',reason='exact historical original')]))
  batch.create(self.c,ip,pp);m=batch.execute(self.c,'B-duplicate',self.transport);self.assertEqual(m['skipped_items'],1)
  current=next(f for f in Query(self.c).get_material(1)['files'] if f['role']=='original' and f['is_current']);self.assertEqual(current['sha256'],digest(new));self.assertNotEqual(current['sha256'],oldsha)

def load_tests(loader,tests,pattern):return loader.loadTestsFromTestCase(BatchTests)

if __name__=='__main__':
 sys.stdout.reconfigure(encoding='utf-8');old.BASE=Path(__file__).resolve().parents[1]/'runs'/('cb-tests-'+dt.datetime.now().strftime('%Y%m%d-%H%M%S'));old.BASE.mkdir(parents=True)
 suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls) for cls in [old.V01Tests,V02Tests,V03Tests,BatchTests])
 log=io.StringIO();result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite);atomic_write(old.BASE/'tests.log',log.getvalue());print(log.getvalue())
 report=dict(status='PASS' if result.wasSuccessful() else 'FAIL',tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),artifacts=str(old.BASE))
 atomic_write(old.BASE/'result.json',json.dumps(report,indent=2));print(json.dumps(report));raise SystemExit(not result.wasSuccessful())
