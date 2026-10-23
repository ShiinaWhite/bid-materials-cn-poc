"""Regression tests use new repositories; no source/pilot records are mutated."""
import datetime as dt
from decimal import Decimal
import io
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock
import uuid

from jty_materials.common import Config, FileLock, atomic_write, digest, normalize, now, safe_path
from jty_materials.store import Store, SCHEMA_VERSION
from jty_materials.query import Query
from jty_materials.workflow import import_material, review_fact
from jty_materials.backup import create_backup, restore_backup
from jty_materials.rpc import Rpc
from jty_materials.ocr import Ocr


BASE = Path(__file__).resolve().parents[1] / "runs/large-document" / ("v01-regression-"+dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
EXPIRY = (dt.date.today()+dt.timedelta(days=60)).isoformat()


class FakeOcr:
    def __init__(self, config, text):
        self.text = text
    def start(self):
        pass
    def stop(self):
        pass
    def document(self, pdf):
        raw = pdf.parent / ".ocr-raw" / uuid.uuid4().hex / "raw.ocr.txt"
        jl = raw.with_suffix(".jsonl"); search = pdf.with_name("original.pdf.search.txt")
        atomic_write(raw, self.text, immutable=True); atomic_write(jl, '{}\n', immutable=True)
        atomic_write(search, normalize(self.text))
        return dict(original=str(pdf), raw=str(raw), jsonl=str(jl), search=str(search), options={})


class V01Tests(unittest.TestCase):
    def setUp(self):
        # Keep paths below Windows 1809 MAX_PATH without changing system policy.
        self.base = BASE / hashlib.sha256((self.id()+uuid.uuid4().hex).encode()).hexdigest()[:8]; self.base.mkdir(parents=True)
        project = Path(__file__).resolve().parents[1]
        raw = {"repository_root":"repository", "db_path":"repository/db/materials.db", "index_dir":"index/dowse-index",
               "state_dir":"state", "logs_dir":"logs", "backup_dir":"backups", "umi_url":"http://127.0.0.1:1224",
               "umi_exe":str(project/'local/runtime/umi/Umi-OCR.exe'), "dowse_exe":str(project/'local/runtime/dowse/dowse.exe'), "fixture_only":True}
        atomic_write(self.base/'config.json',json.dumps(raw))
        self.c=Config(self.base/'config.json'); self.c.materials.mkdir(parents=True); Store.initialize(self.c)
        self.source=self.base/'source.pdf';self.source.write_bytes(b'%PDF-fixture-version-one')
        self.text='安全生产许可证\n有效期限：2024年01月01日至'+EXPIRY.replace('-', '年',1).replace('-', '月')+'日\n旧版独有关键词\n３５ｋＶ'
        self.item=import_material(self.c,self.source,kind='certificate',subtype='safety_prod_license',title='fixture',actor='test',ocr_factory=lambda c:FakeOcr(c,self.text))

    def confirm(self, c=None, value=EXPIRY):
        c=c or self.c
        with Store(c,readonly=True) as s:
            f=s.db.execute("SELECT file_id,sha256 FROM material_file WHERE material_id=1 AND role='original' AND is_current=1").fetchone()
        return review_fact(c,dict(material_id=1,field='valid_until',value=value,actor='fixture-reviewer',reason='isolated regression review',evidence_file_id=f[0],evidence_sha256=f[1]))

    def test_import_has_four_versioned_files_and_relative_mapping(self):
        with Store(self.c,readonly=True) as s:
            fs=s.db.execute('SELECT * FROM material_file').fetchall()
            self.assertEqual(len(fs),4)
            self.assertEqual(len({f['version_id'] for f in fs}),1)
            self.assertTrue(all(':' not in f['relative_path'] and '..' not in f['relative_path'].split('/') for f in fs))
            self.assertEqual(s.db.execute('SELECT count(*) FROM search_projection').fetchone()[0],1)
        m=Query(self.c).get_material(1);self.assertEqual(m['verified_facts'],{});self.assertFalse(m['eligible_for_bid'])

    def test_human_review_and_same_value_reverification(self):
        self.confirm();self.confirm()
        with Store(self.c,readonly=True) as s:
            self.assertEqual(s.db.execute('SELECT count(*) FROM verification_event').fetchone()[0],2)
        self.assertTrue(Query(self.c).get_material(1)['eligible_for_bid'])

    def test_changed_original_invalidates_facts_preserves_id_history_and_old_bytes(self):
        self.confirm();old=Query(self.c).get_material(1);oldfiles={f['relative_path']:digest(safe_path(self.c.repository_root,f['relative_path'])) for f in old['files']}
        v2=self.base/'new.pdf';v2.write_bytes(b'%PDF-different-high-resolution-version')
        new=import_material(self.c,v2,material_id=1,actor='test',ocr_factory=lambda c:FakeOcr(c,self.text.replace('旧版独有关键词','新版独有关键词')))
        self.assertEqual(new['material_id'],1);m=Query(self.c).get_material(1)
        self.assertEqual(m['verified_facts'],{});self.assertEqual(m['verification_status'],'unverified')
        self.assertEqual(len(m['files']),8);self.assertEqual(sum(f['is_current'] for f in m['files']),4)
        for rel,sha in oldfiles.items():self.assertEqual(digest(safe_path(self.c.repository_root,rel)),sha)
        with Store(self.c,readonly=True) as s:
            self.assertEqual(s.db.execute('SELECT count(*) FROM verification_event').fetchone()[0],1)
        self.confirm();self.assertEqual(Query(self.c).get_material(1)['verified_facts']['valid_until']['value'],EXPIRY)
        projection=next(self.c.materials.rglob('current.search.txt')).read_text(encoding='utf-8')
        self.assertIn('新版独有关键词',projection);self.assertNotIn('旧版独有关键词',projection)

    def test_same_sha_replace_is_noop(self):
        result=import_material(self.c,self.source,material_id=1,actor='test',ocr_factory=lambda c:FakeOcr(c,'unused'))
        self.assertTrue(result['unchanged'])
        self.assertEqual(len(Query(self.c).get_material(1)['files']),4)

    def test_changed_search_projection_is_rejected(self):
        path=self.c.materials/'MAT-000001/current.search.txt'
        path.write_text('injected search hit',encoding='utf-8')
        with mock.patch.object(Query,'text_hits',return_value=[{'path':str(path)}]):
            with self.assertRaises(RuntimeError):Query(self.c).search_materials(query='injected')

    def test_old_evidence_review_rejected_after_replace(self):
        old=self.item['files']['original'];sha=digest(self.source)
        self.source.write_bytes(b'%PDF-v2')
        import_material(self.c,self.source,material_id=1,actor='test',ocr_factory=lambda c:FakeOcr(c,self.text))
        with self.assertRaises(ValueError):
            review_fact(self.c,dict(material_id=1,field='valid_until',value=EXPIRY,actor='x',reason='x',evidence_file_id=old,evidence_sha256=sha))

    def test_failed_ocr_keeps_current_material(self):
        self.confirm();self.source.write_bytes(b'%PDF-v2')
        class Broken(FakeOcr):
            def document(self,pdf):raise RuntimeError('OCR failed')
        with self.assertRaises(RuntimeError):
            import_material(self.c,self.source,material_id=1,actor='test',ocr_factory=lambda c:Broken(c,''))
        self.assertTrue(Query(self.c).get_material(1)['eligible_for_bid'])
        self.assertEqual(len(Query(self.c).get_material(1)['files']),4)

    def test_reject_paths_in_python_and_database(self):
        for rel in ['../outside.pdf','D:/outside.pdf','/outside.pdf','x/../y','x\\y']:
            with self.subTest(path=rel):
                with self.assertRaises(ValueError):safe_path(self.c.repository_root,rel)
                with Store(self.c) as s:
                    with self.assertRaises(sqlite3.DatabaseError):
                        s.db.execute("INSERT INTO material_file(material_id,role,relative_path,filename,extension,sha256,size_bytes,imported_at,is_current) VALUES(1,'attachment',?,'x','pdf',?,1,?,0)",(rel,'a'*64,now()))
                    s.db.rollback()

    def test_fact_update_and_detail_replace_are_rejected(self):
        self.confirm()
        with sqlite3.connect(self.c.db_path) as db:
            for sql in ["UPDATE certificate_detail SET valid_until='2099-01-01'", "INSERT OR REPLACE INTO certificate_detail(material_id,certificate_kind) VALUES(1,'safety_production')", "UPDATE certificate_detail SET material_id=2"]:
                with self.assertRaises(sqlite3.DatabaseError):db.execute(sql)
                db.rollback()

    def test_ledger_and_candidate_cannot_be_changed(self):
        self.confirm()
        with Store(self.c) as s:
            for sql in ["DELETE FROM verification_event", "UPDATE verification_event SET reason='changed'", "INSERT OR REPLACE INTO verification_event SELECT * FROM verification_event", "UPDATE audit_log SET note='changed'", "DELETE FROM audit_log", "UPDATE extracted_fact SET raw_value='changed'", "DROP TRIGGER protect_audit_log_DELETE"]:
                with self.assertRaises(sqlite3.DatabaseError):s.db.execute(sql)
                s.db.rollback()

    def test_review_failure_rolls_back_history(self):
        with sqlite3.connect(self.c.db_path) as db:db.execute("CREATE TRIGGER fail_review BEFORE UPDATE ON material BEGIN SELECT RAISE(ABORT,'test rollback'); END")
        with self.assertRaises(sqlite3.DatabaseError):self.confirm()
        with Store(self.c,readonly=True) as s:
            self.assertEqual(s.db.execute('SELECT count(*) FROM verification_event').fetchone()[0],0)
            self.assertIsNone(s.db.execute('SELECT valid_until FROM certificate_detail').fetchone()[0])

    def test_unknown_expired_and_changed_evidence_are_not_eligible(self):
        self.assertFalse(Query(self.c).get_material(1)['eligible_for_bid'])
        self.confirm(value=(dt.date.today()-dt.timedelta(days=1)).isoformat());self.assertFalse(Query(self.c).get_material(1)['eligible_for_bid'])
        self.confirm();m=Query(self.c).get_material(1);f=next(x for x in m['files'] if x['role']=='original')
        safe_path(self.c.repository_root,f['relative_path']).write_bytes(b'changed')
        self.assertFalse(Query(self.c).get_material(1)['eligible_for_bid'])

    def test_dates_use_runtime_today(self):
        self.confirm();actual=dt.date
        class Future(actual):
            @classmethod
            def today(cls):return actual.today()+dt.timedelta(days=3650)
        with mock.patch('jty_materials.query.dt.date',Future):self.assertFalse(Query(self.c).get_material(1)['eligible_for_bid'])

    def test_sqlite_settings_and_readonly(self):
        with Store(self.c,readonly=True) as s:
            self.assertEqual(s.db.execute('PRAGMA foreign_keys').fetchone()[0],1)
            self.assertEqual(s.db.execute('PRAGMA busy_timeout').fetchone()[0],5000)
            self.assertEqual(s.db.execute('PRAGMA journal_mode').fetchone()[0],'wal')
            with self.assertRaises(sqlite3.DatabaseError):s.db.execute("UPDATE material SET title='x'")

    def test_database_init_never_overwrites(self):
        sha=digest(self.c.db_path)
        with self.assertRaises(FileExistsError):Store.initialize(self.c)
        self.assertEqual(digest(self.c.db_path),sha)

    def test_maintenance_and_generation_change_fail_closed(self):
        atomic_write(self.c.maintenance_file,'{}')
        with self.assertRaises(RuntimeError):Query(self.c).get_material(1)
        self.c.maintenance_file.unlink()
        # Change generation during hashing, after the read guard captured it.
        real_digest=digest
        def changed(path):
            atomic_write(self.c.state_dir/'generation.json',str(self.c.generation()+1));return real_digest(path)
        with mock.patch('jty_materials.query.digest',changed),self.assertRaises(RuntimeError):Query(self.c).get_material(1)

    def test_atomic_publish_failure_preserves_prior_content(self):
        p=self.base/'atomic.txt';atomic_write(p,'complete')
        with mock.patch('jty_materials.common.os.replace',side_effect=OSError('simulated crash')):
            with self.assertRaises(OSError):atomic_write(p,'replacement')
        self.assertEqual(p.read_text(),'complete');self.assertEqual(list(self.base.glob('.pending-*')),[])

    def test_backup_restore_relative_paths_and_facts(self):
        self.confirm();backup=create_backup(self.c)['backup'];target=self.base/'restored'
        def runtime(umi,dowse,dest):Path(dest).mkdir(parents=True);return 'http://127.0.0.1:1224'
        with mock.patch('jty_materials.setup.copy_runtime',runtime):restored=restore_backup(backup,target)
        result=Query(Config(restored['config'])).get_material(1)
        self.assertEqual(result['material_id'],1);self.assertEqual(result['verified_facts']['valid_until']['value'],EXPIRY)
        with self.assertRaises(ValueError):restore_backup(backup,target)

    def test_backup_corruption_rejected_before_restore(self):
        backup=Path(create_backup(self.c)['backup']);(backup/'database.db').write_bytes(b'corrupted')
        target=self.base/'restore-rejected'
        with self.assertRaises(ValueError):restore_backup(backup,target)
        self.assertFalse(target.exists())

    def test_corrupted_registered_file_never_produces_complete_backup(self):
        original=next(f for f in Query(self.c).get_material(1)['files'] if f['role']=='original')
        safe_path(self.c.repository_root,original['relative_path']).write_bytes(b'tampered evidence')
        with self.assertRaises(ValueError):create_backup(self.c)
        self.assertEqual(list(self.c.backup_dir.rglob('COMPLETE')),[])

    def test_failed_and_incomplete_ocr_rejected(self):
        for value in [dict(is_done=True,state='error',data='x'),dict(is_done=True,state='success',data='',pages_count=1,processed_count=1),dict(is_done=True,state='success',data='x',pages_count=2,processed_count=1)]:
            with self.assertRaises(RuntimeError):Ocr.validate(value)

    def test_three_tool_mcp_from_other_working_directory(self):
        self.confirm()
        with Rpc([sys.executable,'-B',str(Path(__file__).with_name('serve.py')),'--config',str(self.c.path)],cwd=self.base) as rpc:
            tools=rpc.call('tools/list')['tools'];self.assertEqual({t['name'] for t in tools},{'search_materials','get_material','get_material_text'})
            self.assertTrue(all(t['annotations']['readOnlyHint'] for t in tools))
            self.assertEqual(rpc.tool('get_material',{'material_id':1})['verified_facts']['valid_until']['value'],EXPIRY)
            self.assertTrue(rpc.tool('get_material_text',{'material_id':1})['complete'])
            self.assertEqual(rpc.tool('search_materials',{'eligible_only':True})['total'],1)
            for name,args in [('verify_fact',{}),('get_material',{'material_id':True}),('get_material_text',{'material_id':1,'path':'../outside.pdf'})]:
                with self.assertRaises(ValueError):rpc.tool(name,args)
        self.assertIsNotNone(rpc.p.returncode)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');BASE.mkdir(parents=True)
    log=io.StringIO();result=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(V01Tests))
    atomic_write(BASE/'tests.log',log.getvalue());print(log.getvalue())
    report={'status':'PASS' if result.wasSuccessful() else 'FAIL','tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'artifacts':str(BASE)}
    atomic_write(BASE/'result.json',json.dumps(report,indent=2));print(json.dumps(report));raise SystemExit(not result.wasSuccessful())
