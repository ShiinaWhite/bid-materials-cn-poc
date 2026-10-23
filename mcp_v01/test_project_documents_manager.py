from contextlib import closing
import json
import sqlite3
from unittest import mock

from document_test_support import DocumentFixture, make_pdf, make_docx
from jty_materials import document_index, project_documents
from jty_materials.common import digest, FileLock
from jty_materials.project_documents import Documents
from jty_materials.projects import Projects
from jty_materials.store import Store
from jty_materials.doctor import check_documents
from jty_materials.backup import create_backup, restore_backup


class DocumentManagerTests(DocumentFixture):
    def test_registration_db_failure_never_leaves_a_ghost_job(self):
        fid=self.add(make_pdf(self.base/'registration.pdf'))
        with mock.patch.object(project_documents,'audit',side_effect=sqlite3.OperationalError('registration DB failure')):
            with self.assertRaises(sqlite3.Error):self.begin(fid)
        self.assertEqual(self.supervisor.documents.capabilities,{})
        self.queued.assert_not_called()
        with Store(self.c,readonly=True) as s:
            self.assertEqual(s.db.execute('SELECT count(*) FROM project_document_run').fetchone()[0],0)
        rid,entry=self.begin(fid);self.complete(rid,entry)

    def test_queued_idempotent_and_compute_outside_operation_maintenance(self):
        fid=self.add(make_pdf(self.base/'queue.pdf'))
        rid,entry=self.begin(fid)
        other=self.call('project.document.retry',project_id=self.pid,project_file_id=fid)
        self.assertEqual(other['run_id'],rid);self.assertTrue(other['duplicate'])
        with FileLock(self.c.repository_root/'db/operation.lock'):
            self.assertEqual(Projects(self.c).detail(self.pid)['project_id'],self.pid)
            self.assertFalse(self.c.maintenance_file.exists())
        self.supervisor.job('project.document._progress',dict(run_id=rid,capability=entry['capability'],processed=1,total=3,kind='page'))
        self.assertEqual(Documents(self.c).status(self.pid)[0]['processed_units'],1)
        self.complete(rid,entry)

    def test_staging_text_fsync_holds_no_manager_operation_lock(self):
        from jty_materials import document_extract
        fid=self.add(make_pdf(self.base/'stage.pdf',('native',)*3));rid,entry=self.begin(fid)
        real_write=document_extract.atomic_write
        calls=[]
        def stage_write(path,data,**kw):
            with FileLock(self.c.repository_root/'db/operation.lock'):
                self.assertFalse(self.c.maintenance_file.exists())
                self.assertEqual(Projects(self.c).detail(self.pid)['project_id'],self.pid)
            calls.append(path)
            return real_write(path,data,**kw)
        with mock.patch.object(document_extract,'atomic_write',side_effect=stage_write):
            self.complete(rid,entry)
        self.assertEqual(len(calls),3)

    def test_no_client_paths_or_text_and_foreign_project(self):
        fid=self.add(make_pdf(self.base/'identities.pdf'))
        for extra in ({'source':'C:/arbitrary.pdf'},{'path':'C:/arbitrary'},{'text':'injected'}):
            with self.assertRaises(ValueError):
                self.call('project.document.extract',project_id=self.pid,project_file_id=fid,**extra)
        with self.assertRaises(ValueError):self.begin(fid,self.create('other'))
        with self.assertRaises(ValueError):self.supervisor.job('project.document._finish',dict(run_id='forged',capability='forged'))

    def test_source_size_and_sha_mismatch_refused_before_and_after_compute(self):
        fid=self.add(make_pdf(self.base/'sha.pdf'))
        row=Projects(self.c).resolve_file(self.pid,fid);original=row['path'].read_bytes()
        row['path'].write_bytes(b'tamper')
        with self.assertRaises(ValueError):self.begin(fid)
        row['path'].write_bytes(original)
        rid,entry=self.begin(fid)
        from jty_materials.document_extract import extract
        folder=self.c.document_tmp/rid;folder.mkdir(parents=True)
        entry['result']=extract(entry['source'],folder/'units.jsonl',self.c)
        altered=bytearray(original);altered[-2]^=1;row['path'].write_bytes(altered)
        with self.assertRaises(ValueError):self.supervisor.job('project.document._finish',dict(run_id=rid,capability=entry['capability']))
        row['path'].write_bytes(original)
        self.assertEqual(Documents(self.c).status(self.pid)[0]['status'],'failed')
        self.assertFalse(self.c.maintenance_file.exists())

    def test_index_failure_does_not_close_material_reads_and_rebuilds_without_ocr(self):
        fid=self.add(make_pdf(self.base/'index-failure.pdf'));rid,entry=self.begin(fid)
        with mock.patch.object(document_index,'publish',side_effect=OSError('injected index failure')):
            self.complete(rid,entry)
        self.assertEqual(Documents(self.c).status(self.pid)[0]['index_status'],'failed')
        self.assertFalse(self.c.maintenance_file.exists())
        self.assertTrue(Projects(self.c).resolve_file(self.pid,fid))
        with self.assertRaises(RuntimeError):Documents(self.c).search(self.pid,'项目经理')
        self.call('project.document.index.rebuild')
        self.assertEqual(Documents(self.c).search(self.pid,'项目经理')['total'],1)

    def test_db_failure_rolls_back_units_preserves_original_and_retry(self):
        fid,_=self.process(make_pdf(self.base/'db-failure.pdf'))
        rid,entry=self.begin(fid)
        with closing(sqlite3.connect(self.c.db_path)) as db:
            db.execute("CREATE TRIGGER injected_document_failure BEFORE INSERT ON project_document_unit BEGIN SELECT RAISE(ABORT,'injected DB failure'); END")
        try:
            with self.assertRaises(sqlite3.Error):self.complete(rid,entry)
        finally:
            with closing(sqlite3.connect(self.c.db_path)) as db:db.execute('DROP TRIGGER injected_document_failure')
        self.assertEqual(Documents(self.c).status(self.pid)[0]['status'],'failed')
        self.assertEqual(Documents(self.c).search(self.pid,'项目经理')['total'],1)
        rid,entry=self.begin(fid,action='retry');self.complete(rid,entry)
        self.assertEqual(Documents(self.c).search(self.pid,'项目经理')['total'],1)

    def test_disk_full_projection_failure_can_retry(self):
        fid=self.add(make_pdf(self.base/'disk.pdf'));rid,entry=self.begin(fid)
        with mock.patch.object(project_documents.os,'rename',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.complete(rid,entry)
        self.assertEqual(Documents(self.c).status(self.pid)[0]['status'],'failed')
        rid,entry=self.begin(fid,action='retry');self.complete(rid,entry)
        self.assertEqual(Documents(self.c).search(self.pid,'项目经理')['total'],1)

    def test_restart_interrupts_persisted_job_cleanup_and_retry(self):
        fid=self.add(make_pdf(self.base/'restart.pdf'));rid,entry=self.begin(fid)
        temp=self.c.document_tmp/rid;temp.mkdir(parents=True);(temp/'stale.png').write_bytes(b'temporary')
        self.supervisor.documents.capabilities.clear()
        self.supervisor.documents.recover()
        self.assertFalse(temp.exists())
        self.assertEqual(Documents(self.c).status(self.pid)[0]['status'],'failed')
        rid,entry=self.begin(fid,action='retry');self.complete(rid,entry)

    def test_doctor_missing_corrupt_orphan_projection_and_index_then_rebuild(self):
        self.process(make_pdf(self.base/'doctor.pdf'))
        with Store(self.c,readonly=True) as s:self.assertTrue(check_documents(self.c,s.db)['healthy'])
        projection=next(self.c.document_root.rglob('*.txt'));projection.write_text('corrupt',encoding='utf-8')
        (self.c.document_root/'orphan.txt').write_text('orphan')
        self.c.document_index.write_bytes(b'broken SQLite index')
        with Store(self.c,readonly=True) as s:
            result=check_documents(self.c,s.db)
            self.assertFalse(result['healthy']);self.assertEqual(result['source_errors'],[])
            self.assertIn('rebuildable',str(result['derivative_errors']))
        self.call('project.document.index.rebuild')
        with Store(self.c,readonly=True) as s:self.assertTrue(check_documents(self.c,s.db)['healthy'])

    def test_backup_restore_keeps_raw_units_and_original_excludes_index(self):
        fid,_=self.process(make_docx(self.base/'backup.docx'))
        backup=create_backup(self.c)['backup']
        target=self.base/'restored'
        def runtime(umi,dowse,dest):dest.mkdir(parents=True);return 'http://127.0.0.1:1224'
        with mock.patch('jty_materials.setup.copy_runtime',runtime):result=restore_backup(backup,target)
        from jty_materials.common import Config
        c=Config(result['config'])
        self.assertTrue(result['project_document_rebuild_required'])
        self.assertFalse(c.document_index.exists())
        with Store(c,readonly=True) as s:
            self.assertEqual(s.db.execute('SELECT count(*) FROM project_document_unit').fetchone()[0],3)
            self.assertTrue(s.db.execute('SELECT text_raw FROM project_document_unit WHERE unit_index=3').fetchone()[0])
        self.assertEqual(digest(Projects(c).resolve_file(self.pid,fid)['path']),digest(self.base/'backup.docx'))
        from jty_materials.manager import Supervisor
        from test_p05 import MemoryDowse
        with mock.patch('jty_materials.manager.Dowse',MemoryDowse):restored=Supervisor(c)
        restored.job('project.document.index.rebuild',{})
        self.assertGreater(Documents(c).search(self.pid,'DOCX_SECTION_SENTINEL')['total'],0)
