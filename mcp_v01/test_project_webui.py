"""Actual HTTP routing/origin/downloads, async upload and responsive HTML."""
import http.client
import io
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest import mock

from project_test_support import ProjectFixture
from jty_materials.projects import Projects, MAX_BYTES
from webui import app, projects as ui, service, uploads, catalog


class ProjectWebuiTests(ProjectFixture):
    def setUp(self):
        super().setUp()
        self.rpc=mock.patch.object(ui,'request',side_effect=lambda c,a,p,**kw:self.supervisor.job(a,p))
        self.rpc.start();self.addCleanup(self.rpc.stop)
        self.system=mock.patch.object(service,'system_ok',return_value=True)
        self.system.start();self.addCleanup(self.system.stop)
        context=app.AppContext(self.c)
        handler=type('ProjectFixtureHandler',(app.Handler,),dict(app=context))
        self.server=app.ThreadingHTTPServer(('127.0.0.1',0),handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown();self.server.server_close();self.thread.join()

    def http(self,method,path,body=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=20)
        conn.request(method,path,body=body,headers=headers or {})
        response=conn.getresponse();data=response.read();status=response.status;heads=dict(response.getheaders());conn.close()
        return status,data,heads

    def post(self,action,**payload):
        status,body,_=self.http('POST','/api/project/'+action,json.dumps(dict(actor='web-fixture',**payload)),{'Content-Type':'application/json'})
        return status,json.loads(body)

    def upload(self,pid,name='web.txt',data=b'project evidence',category='other'):
        return self.http('POST','/api/project/file?'+urlencode(dict(project_id=pid,filename=name,category=category,actor='web-fixture')),data)

    def wait_job(self,data):
        jobid=json.loads(data)['job_id']
        until=time.monotonic()+20
        while time.monotonic()<until:
            status,data,_=self.http('GET','/api/project/jobs/'+jobid);self.assertEqual(status,200)
            job=json.loads(data)
            if job['state'] in ('done','error'):return job
            time.sleep(.05)
        self.fail('upload worker timeout')

    def test_http_create_edit_reference_upload_download_archive_and_restore(self):
        status,result=self.post('create',name='HTTP项目');self.assertEqual(status,200);pid=result['result']['project_id']
        self.assertEqual(self.post('update',project_id=pid,name='HTTP更新',tenderer='测试招标人')[0],200)
        mid=self.material();before=self.counts()
        self.assertEqual(self.post('material/link',project_id=pid,material_id=mid)[0],200)
        code,data,_=self.upload(pid);self.assertEqual(code,202)
        job=self.wait_job(data);self.assertEqual(job['state'],'done')
        fid=job['result']['project_file_id']
        status,body,headers=self.http('GET',f'/project-file/{pid}/{fid}')
        self.assertEqual(status,200);self.assertEqual(body,b'project evidence')
        self.assertTrue(headers['Content-Disposition'].startswith('attachment;'))
        self.assertEqual(headers['Cache-Control'],'no-store')
        status,body,_=self.http('GET',f'/material/{mid}')
        self.assertEqual(status,200);self.assertIn('用于 1 个项目',body.decode())
        self.assertEqual(self.post('material/unlink',project_id=pid,material_id=mid)[0],200)
        self.assertEqual(self.counts(),dict(before,project_file=1,project_material=1))
        self.assertEqual(self.post('archive',project_id=pid)[0],200)
        self.assertEqual(self.http('GET',f'/project-file/{pid}/{fid}')[0],200)
        self.assertEqual(self.http('GET',f'/project/{pid}/upload')[0],400)
        self.assertEqual(self.post('restore',project_id=pid)[0],200)
        self.assert_clean()

    def test_post_cross_site_rejected_for_every_concrete_project_action_before_read(self):
        actions=['create','update','archive','restore','file','material/link','material/unlink']
        with mock.patch.object(uploads,'stage_body') as stage:
            for action in actions:
                with self.subTest(action=action):
                    code,body,headers=self.http('POST','/api/project/'+action,b'unread',{'Origin':'https://evil.example'})
                    self.assertEqual(code,403);self.assertEqual(headers['Connection'],'close')
            stage.assert_not_called()
        self.assertEqual(self.counts()['project'],0)

    def test_rejections_bad_project_format_size_and_corrupt_download(self):
        pid=self.create()
        for filename,data in [('file.exe',b'exe'),('../file.pdf',b'%PDF-'),('fake.pdf',b'MZ')]:
            with self.subTest(filename=filename):self.assertEqual(self.upload(pid,filename,data)[0],400)
        self.assertEqual(self.upload(999)[0],400)
        self.assertEqual(self.upload(pid,category='material')[0],400)
        req=SimpleNamespace(query=dict(project_id=str(pid),filename='large.pdf',category='other',actor='fixture'),
                            headers={'Content-Length':str(MAX_BYTES+1)},rfile=mock.Mock())
        self.assertEqual(ui.upload_api(self.c,mock.Mock(),req)[0],413);req.rfile.read.assert_not_called()
        code,data,_=self.upload(pid,'preview.pdf',b'%PDF-web-preview');self.assertEqual(code,202)
        result=self.wait_job(data)['result'];fid=result['project_file_id']
        url=f'/project-file/{pid}/{fid}'
        self.assertEqual(self.http('GET',url)[2]['Content-Disposition'].split(';')[0],'inline')
        self.assertEqual(self.http('GET',url+'?download=1')[2]['Content-Disposition'].split(';')[0],'attachment')
        other=self.create('其他项目');self.assertEqual(self.http('GET',f'/project-file/{other}/{fid}')[0],404)
        row=Projects(self.c).resolve_file(pid,fid);row['path'].write_bytes(b'broken')
        self.assertEqual(self.http('GET',url)[0],409)

    def test_short_body_worker_manager_failure_and_archived_write(self):
        with self.assertRaises(ValueError):uploads.stage_body(self.c,io.BytesIO(b'short'),100)
        self.assertFalse(list(uploads.staging_root(self.c).glob('tmp-*')))
        pid=self.create()
        with mock.patch.object(ui,'request',side_effect=RuntimeError('manager unavailable')):
            code,data,_=self.upload(pid)
            self.assertEqual(code,202);job=self.wait_job(data)
            self.assertEqual(job['state'],'error');self.assertIn('manager unavailable',job['error'])
        self.assertFalse(list(uploads.staging_root(self.c).glob('tmp-*')))
        self.call('project.archive',project_id=pid)
        self.assertEqual(self.upload(pid)[0],400)
        self.assertEqual(self.counts()['project_file'],0)

    def test_server_html_mobile_cards_forms_escape_and_global_material_boundary(self):
        pid=self.create('<script>alert(1)</script> 项目')
        for url in ['/projects','/projects/new',f'/project/{pid}',f'/project/{pid}/edit',f'/project/{pid}/upload']:
            with self.subTest(url=url):
                status,body,_=self.http('GET',url);html=body.decode()
                self.assertEqual(status,200);self.assertIn('name="viewport"',html)
                self.assertIn('href="/projects"',html)
                self.assertNotIn('<script>alert(1)</script>',html)
        html=self.http('GET','/projects')[1].decode()
        self.assertIn('project-card',html)
        html=self.http('GET','/projects/new')[1].decode()
        self.assertIn('name="name"',html);self.assertNotIn('name="project_code"',html)
        self.assertEqual(catalog.UPLOAD_EXTS,('.pdf','.png','.jpg','.jpeg'))
        html=self.http('GET','/upload')[1].decode()
        self.assertIn('accept=".pdf,.png,.jpg,.jpeg"',html)
        self.assertNotIn('accept=".pdf,.png,.jpg,.jpeg,.doc',html)
