"""Mandatory actual Umi and actual named-pipe manager/dowse integration."""
import json
from multiprocessing.connection import Client
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from unittest import mock

from document_test_support import DocumentFixture, make_pdf, make_docx
from jty_materials.common import Config, atomic_write, digest
from jty_materials.document_extract import PageOcr
from jty_materials.project_documents import Documents
from jty_materials import manager
from jty_materials.setup import copy_runtime
from jty_materials.query import Query
from jty_materials.store import Store
from jty_materials.workflow import import_material, rebuild_projections
from test_v01 import FakeOcr
from jty_materials.rpc import Rpc
from webui import service, pages


class DocumentIntegrationTests(DocumentFixture):
    def test_real_umi_image_only_pdf_becomes_searchable(self):
        umi=Path(os.environ.get('LARGE_DOC_UMI_EXE','missing-Umi-OCR.exe'))
        dowse=Path(os.environ.get('P05_DOWSE_EXE','missing-dowse.exe'))
        self.assertTrue(umi.is_file(),'BLOCKED: supply real LARGE_DOC_UMI_EXE; mock/skip forbidden')
        self.assertTrue(dowse.is_file(),'BLOCKED: supply real P05_DOWSE_EXE')
        url=copy_runtime(umi,dowse,self.base/'runtime')
        raw=dict(self.c.raw,umi_exe='runtime/umi/Umi-OCR.exe',dowse_exe='runtime/dowse/dowse.exe',umi_url=url)
        atomic_write(self.c.path,json.dumps(raw));self.c=Config(self.c.path)
        self.supervisor.c=self.c;self.supervisor.documents.c=self.c
        fid=self.add(make_pdf(self.base/'real-scan.pdf',('scan',)))
        rid,entry=self.begin(fid)
        result=self.complete(rid,entry,PageOcr)
        self.assertEqual(result['status'],'complete',result)
        self.assertGreater(Documents(self.c).search(self.pid,'项目经理')['total'],0)
        self.assertGreater(Documents(self.c).search(self.pid,'998877')['total'],0)
        with Store(self.c,readonly=True) as s:
            self.assertEqual(s.db.execute('SELECT page_status FROM project_document_unit').fetchone()[0],'ocr')
        self.assertFalse(list(self.c.document_tmp.rglob('*.png')))
        atomic_write(self.base/'real-umi-evidence.json',json.dumps(dict(status='PASS',result=result,
            hits=Documents(self.c).search(self.pid,'项目经理'),umi_version='2.1.5'),ensure_ascii=False,indent=2))

    def test_real_manager_disconnect_restart_and_dowse_hard_isolation(self):
        executable=Path(os.environ.get('P05_DOWSE_EXE','missing-dowse.exe'))
        self.assertTrue(executable.is_file(),'BLOCKED: real dowse required')
        (self.base/'bin').mkdir();shutil.copy2(executable,self.base/'bin/dowse.exe')
        raw=dict(self.c.raw,dowse_exe='bin/dowse.exe')
        atomic_write(self.c.path,json.dumps(raw));self.c=Config(self.c.path)
        self.supervisor.c=self.c;self.supervisor.documents.c=self.c
        material=self.base/'material.pdf';material.write_bytes(b'%PDF-material-sentinel')
        import_material(self.c,material,kind='certificate',subtype='safety_prod_license',title='Material sentinel',actor='fixture',
            ocr_factory=lambda c:FakeOcr(c,'MATERIAL_SENTINEL_123456'))
        path=make_docx(self.base/'history.docx','PROJECT_DOC_SENTINEL_654321 HISTORICAL_FAKE_CERTIFICATE_998877')
        fid=self.add(path)
        rid,_=self.begin(fid)  # durable queued job from an interrupted manager
        before=self.counts()
        with Store(self.c,readonly=True) as s:
            facts={t:[tuple(r) for r in s.db.execute('SELECT * FROM '+t)] for t in ('extracted_fact','verification_event','certificate_detail')}
        log=(self.base/'manager.log').open('ab')
        child=subprocess.Popen([sys.executable,'-B',str(Path(__file__).with_name('manage.py')),'--config',str(self.c.path)],stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        def call(action,payload=None):return manager.request(self.c,action,payload,start=False,timeout=120)
        try:
            until=time.monotonic()+20
            while time.monotonic()<until:
                if call('status').get('running'):break
                if child.poll() is not None:self.fail('manager failed; inspect fixture manager.log')
                time.sleep(.1)
            self.assertEqual(Documents(self.c).status(self.pid)[0]['status'],'failed')
            call('index')
            def material_index():
                with Rpc([str(self.c.dowse_exe),'mcp'],env=dict(os.environ,DOWSE_INDEX_DIR=str(self.c.index_dir))) as rpc:
                    return rpc.tool('index_status',{})['num_docs']
            count=material_index()
            with Client(manager.endpoint(self.c),family='AF_PIPE',authkey=manager.key(self.c)) as conn:
                conn.send_bytes(json.dumps(dict(action='project.document.retry',payload=dict(project_id=self.pid,project_file_id=fid))).encode())
                # Deliberately disconnect without reading the extraction receipt.
            until=time.monotonic()+40
            seen=[]
            while time.monotonic()<until:
                try:
                    d=Documents(self.c).status(self.pid)[0];seen.append(d['status'])
                    self.assertFalse(self.c.maintenance_file.exists())
                    self.assertEqual(Query(self.c).get_material(material_id=1)['material_id'],1)
                    if d['status']=='complete' and d['index_status']=='ready':break
                except RuntimeError as error:
                    if 'RETRY' not in str(error):raise
                time.sleep(.1)
            else:self.fail('asynchronous manager extraction did not complete')
            self.assertTrue(call('status')['running'])
            self.assertGreater(Documents(self.c).search(self.pid,'PROJECT_DOC_SENTINEL_654321')['total'],0)
            self.assertGreater(Documents(self.c).search(self.pid,'HISTORICAL_FAKE_CERTIFICATE_998877')['total'],0)
            self.assertEqual(Documents(self.c).search(self.pid,'MATERIAL_SENTINEL_123456')['total'],0)
            self.assertGreater(Query(self.c).search_materials(query='MATERIAL_SENTINEL_123456')['total'],0)
            for term in ('PROJECT_DOC_SENTINEL_654321','HISTORICAL_FAKE_CERTIFICATE_998877'):
                self.assertEqual(Query(self.c).search_materials(query=term)['total'],0)
                self.assertEqual(service.list_materials(self.c,q=term)['total'],0)
                with mock.patch.object(service,'system_ok',return_value=True):self.assertNotIn('history.docx',pages.browse(self.c,dict(q=term)))
            self.assertEqual(self.counts(),before)
            self.assertEqual(material_index(),count)
            with Store(self.c,readonly=True) as s:
                for table,rows in facts.items():self.assertEqual(rows,[tuple(r) for r in s.db.execute('SELECT * FROM '+table)])
            atomic_write(self.base/'isolation-evidence.json',json.dumps(dict(status='PASS',material_index_before=count,
                material_index_after=count,states=seen,client_disconnect=True,manager_restart=True),indent=2))
        finally:
            if child.poll() is None:
                try:call('shutdown')
                finally:
                    try:child.wait(timeout=45)
                    except subprocess.TimeoutExpired:child.terminate();child.wait(timeout=5)
            log.close()
