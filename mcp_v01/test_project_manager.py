"""Failure recovery, actual manager RPC/disconnects and actual dowse isolation."""
from contextlib import closing
import hashlib
import json
from multiprocessing.connection import Client
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from unittest import mock
import uuid

from project_test_support import ProjectFixture
from jty_materials import projects, manager
from jty_materials.common import atomic_write, digest
from jty_materials.projects import Projects
from jty_materials.store import Store
from jty_materials.query import Query
from jty_materials.rpc import Rpc
from webui import pages, service


class ProjectManagerTests(ProjectFixture):
    def test_db_audit_and_filesystem_failure_remove_uncommitted_files_and_next_write(self):
        pid=self.create()
        for failure in ['copy','publish','db','audit']:
            payload=self.file_payload(pid,data=('%PDF-'+failure).encode())
            before=self.counts()
            if failure in ('db','audit'):
                table='project_file' if failure=='db' else 'audit_log'
                with closing(sqlite3.connect(self.c.db_path)) as db:
                    db.execute(f"CREATE TRIGGER fail_project BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'injected DB failure'); END")
                patch=mock.patch.object(projects,'validate_signature',wraps=projects.validate_signature)
            else:
                patch=mock.patch.object(projects.shutil,'copyfileobj',side_effect=OSError('injected copy failure')) if failure=='copy' else mock.patch.object(projects.os,'rename',side_effect=OSError('injected publish failure'))
            try:
                with self.subTest(failure=failure),patch,self.assertRaises((OSError,sqlite3.DatabaseError)):
                    self.call('project.file.add',**payload)
            finally:
                if failure in ('db','audit'):
                    with closing(sqlite3.connect(self.c.db_path)) as db:db.execute('DROP TRIGGER fail_project')
            self.assertEqual(self.counts(),before)
            self.assertFalse(list((self.c.repository_root/'projects').glob('*/files/*')))
            self.assert_clean()
            self.call('project.update',project_id=pid,name='failure recovered')

    def intention(self,pid,registered=False):
        p=Projects(self.c).detail(pid)
        if registered:
            result=self.call('project.file.add',**self.file_payload(pid))
            fid=result['project_file_id']
            sha=result['sha256']
        else:
            fid=uuid.uuid4().hex;sha=hashlib.sha256(b'%PDF-uncommitted').hexdigest()
        record=dict(file_id=fid,project_code=p['project_code'],extension='pdf',sha256=sha)
        staged,final,relative=projects.journal_paths(self.c,record)
        journal=self.c.state_dir/'project-file-journal'/f'{fid}.json'
        atomic_write(journal,json.dumps(record),immutable=True)
        return staged,final,journal

    def test_interrupted_file_publication_before_commit_after_commit_and_staging(self):
        pid=self.create()
        for state in ['staging','published','committed']:
            with self.subTest(state=state):
                staged,final,journal=self.intention(pid,state=='committed')
                if state!='committed':
                    target=staged if state=='staging' else final
                    atomic_write(target,b'%PDF-uncommitted',immutable=True)
                atomic_write(self.c.maintenance_file,'{"state":"interrupted"}')
                self.call('project.update',project_id=pid,name='recovered')
                self.assertEqual(final.exists(),state=='committed')
                self.assert_clean()
        self.assertEqual(self.counts()['project_file'],1)

    def test_failure_after_commit_retains_file_and_retry_is_duplicate(self):
        pid=self.create();payload=self.file_payload(pid)
        # Lost publish_generation happens after the DB and file are committed.
        with mock.patch.object(self.supervisor,'publish_generation',side_effect=[OSError('injected postcommit failure'),None]):
            with self.assertRaisesRegex(OSError,'postcommit'):self.call('project.file.add',**payload)
        self.assert_clean();self.assertEqual(self.counts()['project_file'],1)
        self.assertTrue(self.call('project.file.add',**payload)['duplicate'])

    def test_existing_uuid_destination_and_publication_collision_never_delete_bytes(self):
        pid=self.create();fid='f'*32
        record=dict(file_id=fid,project_code='PRJ-000001',extension='pdf',sha256='a'*64)
        staged,final,relative=projects.journal_paths(self.c,record)
        original=b'%PDF-preexisting-unregistered-evidence'
        atomic_write(final,original,immutable=True)
        with mock.patch.object(projects.uuid,'uuid4',return_value=mock.Mock(hex=fid)):
            with self.assertRaises(FileExistsError):self.call('project.file.add',**self.file_payload(pid))
        self.assertEqual(final.read_bytes(),original)
        self.assert_clean()
        # A collision appearing after the initial check must also be retained.
        fid='e'*32
        real_rename=projects.os.rename
        def collide(source,target):
            if Path(target)==final.parent/(fid+'.pdf'):
                Path(target).write_bytes(original)
                raise FileExistsError('injected publication collision')
            return real_rename(source,target)
        with mock.patch.object(projects.uuid,'uuid4',return_value=mock.Mock(hex=fid)), mock.patch.object(projects.os,'rename',side_effect=collide):
            with self.assertRaises(FileExistsError):self.call('project.file.add',**self.file_payload(pid,data=b'%PDF-collision'))
        self.assertEqual((final.parent/(fid+'.pdf')).read_bytes(),original)
        self.assertEqual(self.counts()['project_file'],0)
        self.assert_clean()

    def test_project_damage_keeps_maintenance_until_repaired(self):
        pid=self.create();added=self.call('project.file.add',**self.file_payload(pid))
        path=Projects(self.c).resolve_file(pid,added['project_file_id'])['path'];saved=path.read_bytes();path.write_bytes(b'corrupt')
        atomic_write(self.c.maintenance_file,'{"state":"interrupted"}')
        with self.assertRaisesRegex(RuntimeError,'MAINTENANCE'):self.call('project.update',project_id=pid,name='blocked')
        self.assertTrue(self.c.maintenance_file.exists())
        path.write_bytes(saved)
        self.call('project.update',project_id=pid,name='repaired');self.assert_clean()

    def test_generation_and_maintenance_guard_every_project_read(self):
        pid=self.create();mid=self.material();f=self.call('project.file.add',**self.file_payload(pid))
        read=Projects(self.c)
        atomic_write(self.c.maintenance_file,'{}')
        for method,args in [('list',()),('detail',(pid,)),('reverse',(mid,)),('resolve_file',(pid,f['project_file_id']))]:
            with self.subTest(method=method),self.assertRaisesRegex(RuntimeError,'MAINTENANCE'):getattr(read,method)(*args)
        self.c.maintenance_file.unlink()
        with mock.patch.object(self.c,'generation',side_effect=[1,2]):
            with self.assertRaisesRegex(RuntimeError,'RETRY'):read.detail(pid)

    def test_true_named_pipe_manager_disconnect_and_real_dowse_search_isolation(self):
        executable=Path(os.environ['P05_DOWSE_EXE'])
        self.assertTrue(executable.is_file(),'P05_DOWSE_EXE must point to a real runtime; never skip')
        runtime=self.base/'bin';runtime.mkdir();shutil.copy2(executable,runtime/'dowse.exe')
        raw=dict(self.c.raw,dowse_exe='bin/dowse.exe')
        atomic_write(self.c.path,json.dumps(raw))
        from jty_materials.common import Config
        self.c=Config(self.c.path)
        mid=self.material();pid=self.create()
        entry=Path(__file__).with_name('manage.py')
        with (self.base/'manager-console.log').open('wb') as log:
            child=subprocess.Popen([sys.executable,'-B',str(entry),'--config',str(self.c.path)],stdout=log,stderr=log,
                                   creationflags=subprocess.CREATE_NO_WINDOW,env=dict(os.environ,DOWSE_FORCE_SLOW_LANE='1'))
            def call(action,payload=None):return manager.request(self.c,action,payload,start=False,timeout=180)
            try:
                until=time.monotonic()+15
                while not call('status').get('running'):
                    if child.poll() is not None or time.monotonic()>until:self.fail('real manager failed to start')
                    time.sleep(.1)
                call('index')
                with Rpc([str(self.c.dowse_exe),'mcp'],env=dict(os.environ,DOWSE_INDEX_DIR=str(self.c.index_dir))) as rpc:
                    baseline=rpc.tool('index_status',{})['num_docs']
                counts=self.counts()
                from docx import Document
                source=self.base/'project-specific-file.docx';doc=Document();doc.add_paragraph('PROJECT_ONLY_SENTINEL_847291');doc.save(source)
                payload=dict(project_id=pid,filename=source.name,source=str(source),category='tender_docs',actor='real-fixture',expected_sha256=digest(source))
                result=call('project.file.add',payload)
                self.assertEqual(self.counts(),dict(counts,project_file=1))
                with Rpc([str(self.c.dowse_exe),'mcp'],env=dict(os.environ,DOWSE_INDEX_DIR=str(self.c.index_dir))) as rpc:
                    self.assertEqual(rpc.tool('index_status',{})['num_docs'],baseline)
                    self.assertEqual(rpc.tool('search',dict(query='PROJECT_ONLY_SENTINEL_847291',ext='txt',limit=10,offset=0))['total_hits'],0)
                self.assertEqual(Query(self.c).search_materials(query='PROJECT_ONLY_SENTINEL_847291')['total'],0)
                self.assertEqual(service.list_materials(self.c,q='PROJECT_ONLY_SENTINEL_847291')['total'],0)
                with mock.patch.object(service,'system_ok',return_value=True):
                    self.assertNotIn(source.name,pages.browse(self.c,dict(q='PROJECT_ONLY_SENTINEL_847291')))
                self.assertEqual(Projects(self.c).resolve_file(pid,result['project_file_id'])['path'].read_bytes(),source.read_bytes())
                # A client disconnects immediately after sending a manager write.
                with Client(manager.endpoint(self.c),family='AF_PIPE',authkey=manager.key(self.c)) as conn:
                    conn.send_bytes(json.dumps(dict(action='project.material.link',payload=dict(project_id=pid,material_id=mid,actor='disconnected-client'))).encode())
                # Queue a status behind the disconnected request, rather than retrying it.
                self.assertTrue(call('status')['running'])
                self.assertEqual(Projects(self.c).detail(pid)['references'][0]['material_id'],mid)
                self.assertEqual(self.counts(),dict(counts,project_file=1,project_material=1))
                disconnected = dict(self.file_payload(pid, 'disconnect.txt', b'disconnected project file'), actor='disconnected-uploader')
                with Client(manager.endpoint(self.c),family='AF_PIPE',authkey=manager.key(self.c)) as conn:
                    conn.send_bytes(json.dumps(dict(action='project.file.add',payload=disconnected)).encode())
                self.assertTrue(call('status')['running'])
                self.assertEqual(self.counts(),dict(counts,project_file=2,project_material=1))
                self.assertTrue(call('project.file.add',payload)['duplicate'])
                self.assert_clean()
            finally:
                if child.poll() is None:
                    try:call('shutdown')
                    finally:
                        try:child.wait(timeout=45)
                        except subprocess.TimeoutExpired:child.terminate();child.wait(timeout=5)
