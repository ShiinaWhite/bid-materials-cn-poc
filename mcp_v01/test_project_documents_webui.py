import json
from unittest import mock
from urllib.parse import quote

import test_project_webui
from document_test_support import make_pdf, make_docx, make_xlsx
from jty_materials.project_documents import Documents
from jty_materials.document_extract import extract
from jty_materials.store import Store


class DocumentWebuiTests(test_project_webui.ProjectHttpFixture):
    def setUp(self):
        super().setUp()
        self.pid=self.create()
        self.enqueue=mock.patch.object(self.supervisor.documents,'enqueue')
        self.enqueue.start();self.addCleanup(self.enqueue.stop)

    def process(self,path):
        from jty_materials.common import digest
        fid=self.call('project.file.add',project_id=self.pid,source=str(path),filename=path.name,category='other',expected_sha256=digest(path))['project_file_id']
        code,result=self.post('document/extract',project_id=self.pid,project_file_id=fid)
        self.assertEqual(code,200)
        rid=result['result']['run_id'];entry=self.supervisor.documents.capabilities[rid]
        folder=self.c.document_tmp/rid;folder.mkdir(parents=True)
        entry['result']=extract(entry['source'],folder/'units.jsonl',self.c)
        self.supervisor.job('project.document._finish',dict(run_id=rid,capability=entry['capability']))
        return fid

    def test_document_status_api_post_same_origin_and_no_path_injection(self):
        from jty_materials.common import digest
        path=make_pdf(self.base/'pending.pdf')
        fid=self.call('project.file.add',project_id=self.pid,source=str(path),filename=path.name,category='other',expected_sha256=digest(path))['project_file_id']
        status,body,_=self.http('GET',f'/api/project/{self.pid}/documents')
        self.assertEqual(status,200);self.assertEqual(json.loads(body)['items'][0]['status'],'queued')  # v0.8: upload auto-queues
        for action in ('extract','retry','rebuild'):
            self.assertEqual(self.http('POST','/api/project/document/'+action,b'unread',{'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.post('document/extract',project_id=self.pid,project_file_id=fid,source='C:/secret.pdf')[0],400)
        self.assertEqual(self.post('document/extract',project_id=self.pid,project_file_id=fid)[0],200)
        status,body,_=self.http('GET',f'/api/project/{self.pid}/documents')
        self.assertEqual(json.loads(body)['items'][0]['status'],'queued')
        self.assertIn('等待自动整理',self.http('GET',f'/project/{self.pid}')[1].decode())

    def test_search_results_pages_sections_sheets_escaped_and_download_trace(self):
        fid=self.process(make_pdf(self.base/'招标文件.pdf'))
        self.process(make_docx(self.base/'历史投标.docx'))
        self.process(make_xlsx(self.base/'工程量清单.xlsx'))
        for term,expected in [('项目经理','#page=1'),('DOCX_SECTION_SENTINEL','（一）项目经理'),('SPARSE_ROW_SENTINEL','第 280–280 行')]:
            status,body,_=self.http('GET',f'/project/{self.pid}/search?q='+quote(term))
            self.assertEqual(status,200);self.assertIn(expected,body.decode())
        html=self.http('GET',f'/project/{self.pid}')[1].decode()
        self.assertIn('全文搜索已就绪',html);self.assertIn('搜索项目文件',html)
        self.assertEqual(self.http('GET',f'/project-file/{self.pid}/{fid}')[0],200)

    def test_partial_failed_processing_and_index_unavailable_ui(self):
        fid=self.process(make_pdf(self.base/'states.pdf'))
        with Store(self.c) as s:
            s.document_write=True
            for state,label in [('processing','正在读取文档'),('partial','字正文'),('failed','内容提取失败')]:
                with s.db:s.db.execute('UPDATE project_document SET status=?',(state,))
                html=self.http('GET',f'/project/{self.pid}')[1].decode()
                self.assertIn(label,html)
        self.c.document_index.unlink()
        html=self.http('GET',f'/project/{self.pid}/search?q='+quote('项目经理'))[1].decode()
        self.assertIn('项目搜索暂不可用',html)
