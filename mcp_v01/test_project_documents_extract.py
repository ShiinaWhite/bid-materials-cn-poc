import json
import sys
from unittest import mock

from document_test_support import DocumentFixture, StubOcr, make_pdf, make_docx, make_xlsx
from jty_materials.document_extract import extract, Unavailable
from jty_materials.project_documents import Documents
from jty_materials.store import Store
from jty_materials.projects import Projects


class DocumentExtractTests(DocumentFixture):
    def units(self):
        with Store(self.c, readonly=True) as s:
            return [dict(r) for r in s.db.execute('SELECT * FROM project_document_unit ORDER BY unit_index')]

    def test_real_native_pdf_pages_no_ocr_raw_norm_and_trace(self):
        path = make_pdf(self.base/'native.pdf', ('native',)*3)
        with mock.patch.object(StubOcr, 'recognize', side_effect=AssertionError('native PDF must not OCR')):
            fid, result = self.process(path)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual([u['page_no'] for u in self.units()], [1,2,3])
        self.assertTrue(all(u['page_status']=='native' for u in self.units()))
        hit = Documents(self.c).search(self.pid, '项目经理')['items'][0]
        for key in ('project_id','project_file_id','document_id','unit_id','page_no'):
            self.assertTrue(hit[key])
        self.assertEqual(hit['project_file_id'], fid)
        self.assertEqual(Projects(self.c).resolve_file(self.pid, fid)['path'].read_bytes(), path.read_bytes())

    def test_real_mixed_pdf_only_scanned_page_falls_back(self):
        calls = []
        class Ocr(StubOcr):
            def recognize(self, data): calls.append(data); return super().recognize(data)
        fid = self.add(make_pdf(self.base/'mixed.pdf', ('native','scan','native')))
        rid, entry = self.begin(fid); self.complete(rid, entry, Ocr)
        self.assertEqual([u['page_status'] for u in self.units()], ['native','ocr','native'])
        self.assertEqual(len(calls),1)
        self.assertEqual(Documents(self.c).search(self.pid,'SCANNED_OCR_SENTINEL')['items'][0]['page_no'],2)
        self.assertFalse(list(self.c.document_tmp.rglob('*.png')))

    def test_page_ocr_failure_preserves_other_pages_and_retry(self):
        class Ocr(StubOcr):
            def recognize(self, data): raise OSError('Umi unavailable/crashed')
        fid = self.add(make_pdf(self.base/'partial.pdf', ('native','scan','native')))
        rid, entry = self.begin(fid)
        self.assertEqual(self.complete(rid, entry, Ocr)['status'], 'partial')
        self.assertEqual(self.units()[1]['page_status'], 'failed')
        self.assertEqual(Documents(self.c).search(self.pid,'PROJECT_DOC_SENTINEL_654321')['total'],2)
        rid, entry = self.begin(fid, action='retry')
        self.assertEqual(self.complete(rid, entry)['status'], 'complete')
        self.assertEqual(len(self.units()),3)

    def test_empty_pdf_page_state(self):
        class Ocr(StubOcr):
            def recognize(self, data): return ''
        fid = self.add(make_pdf(self.base/'empty.pdf', ('blank',)))
        rid, entry = self.begin(fid); self.complete(rid, entry, Ocr)
        self.assertEqual(self.units()[0]['page_status'],'empty')

    def test_ocr_cleanup_crash_retains_extracted_pages_as_partial(self):
        class Ocr(StubOcr):
            def close(self): raise OSError('OCR process crashed during close')
        fid=self.add(make_pdf(self.base/'close-crash.pdf',('native','scan')))
        rid,entry=self.begin(fid)
        self.assertEqual(self.complete(rid,entry,Ocr)['status'],'partial')
        self.assertEqual(len(self.units()),2)
        self.assertGreater(Documents(self.c).search(self.pid,'SCANNED_OCR_SENTINEL')['total'],0)

    def test_real_docx_heading_hierarchy_and_tables(self):
        _, result = self.process(make_docx(self.base/'structured.docx'))
        self.assertEqual(result['status'],'complete')
        hit = Documents(self.c).search(self.pid, '付款方式')['items'][0]
        self.assertIn('五、项目管理机构 → （一）项目经理',hit['section_path'])
        self.assertIn('按月支付',hit['snippet'])
        self.assertIsNone(hit['page_no'])

    def test_docx_tiny_decorative_image_skipped_without_ocr(self):
        from docx import Document
        from PIL import Image
        path=self.base/'image.docx'; image=self.base/'image.png'
        Image.new('RGB',(50,50),'white').save(image)
        doc=Document();doc.add_picture(str(image));doc.save(path)
        with mock.patch.object(StubOcr,'recognize',side_effect=AssertionError('decorative image must not OCR')):
            _, result=self.process(path)
        self.assertEqual(result['status'],'complete')
        with Store(self.c, readonly=True) as s:
            rows=[dict(r) for r in s.db.execute('SELECT * FROM project_document_image')]
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['ocr_status'],'skipped')
        self.assertIn('decorative',rows[0]['ocr_error'])
        self.assertEqual(result['summary']['embedded_images']['skipped'],1)

    def test_real_xlsx_sheet_sparse_rows_and_inert_formulas(self):
        _, result=self.process(make_xlsx(self.base/'bill.xlsx'))
        self.assertEqual(result['status'],'complete')
        hit=Documents(self.c).search(self.pid,'SPARSE_ROW_SENTINEL')['items'][0]
        self.assertEqual(hit['row_start'],280)
        self.assertEqual(hit['row_end'],280)
        self.assertTrue(hit['cell_range'].startswith('A280:'))
        self.assertIn('分部分项工程量清单',hit['sheet_name'])
        self.assertEqual(Documents(self.c).search(self.pid,'=B2*2')['total'],2)
        self.assertFalse(any(u['row_start']<20 and u['row_end']>22 for u in self.units()))

    def test_unsupported_old_office_and_pptx_keep_original(self):
        for ext in ('doc','xls','ppt','pptx','zip'):
            path=self.base/('old.'+ext)
            path.write_bytes(b'PK\x05\x06' if ext in ('pptx','zip') else b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1')
            fid,result=self.process(path)
            self.assertEqual(result['status'],'unsupported')
            self.assertEqual(Projects(self.c).resolve_file(self.pid,fid)['path'].read_bytes(),path.read_bytes())

    def test_bad_pdf_docx_xlsx_fail_without_touching_original(self):
        for ext in ('pdf','docx','xlsx'):
            path=self.base/('bad.'+ext);path.write_bytes(b'%PDF-bad' if ext=='pdf' else b'PK\x03\x04bad')
            fid=self.add(path);rid,entry=self.begin(fid)
            folder=self.c.document_tmp/rid;folder.mkdir(parents=True)
            with self.assertRaises(Exception):extract(entry['source'],folder/'units.jsonl',self.c)
            self.supervisor.documents.fail(dict(run_id=rid,capability=entry['capability']),RuntimeError('malformed source'))
            self.assertEqual(Documents(self.c).status(self.pid)[0]['status'],'failed')
            self.assertEqual(Projects(self.c).resolve_file(self.pid,fid)['path'].read_bytes(),path.read_bytes())

    def test_optional_pdf_parser_does_not_break_core(self):
        fid=self.add(make_pdf(self.base/'optional.pdf'));rid,entry=self.begin(fid)
        with mock.patch.dict(sys.modules, {'pymupdf':None}):
            with self.assertRaises(Unavailable):self.complete(rid,entry)
            self.assertEqual(Projects(self.c).detail(self.pid)['project_id'],self.pid)
