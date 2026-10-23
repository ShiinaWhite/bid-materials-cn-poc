from unittest import mock

from document_test_support import DocumentFixture, make_docx, make_pdf
from jty_materials.project_documents import Documents
from jty_materials.query import Query
from jty_materials.store import Store
from jty_materials import document_index
from jty_materials.workflow import rebuild_projections
from webui import service, pages


class DocumentSearchTests(DocumentFixture):
    def test_historical_fact_and_cross_project_material_isolation(self):
        mid=self.material()
        before=self.counts()
        with Store(self.c,readonly=True) as s:
            facts={t:[tuple(r) for r in s.db.execute('SELECT * FROM '+t)] for t in ('extracted_fact','verification_event','certificate_detail')}
        a='PROJECT_A_SECRET_TERM_111'
        b='PROJECT_B_SECRET_TERM_222'
        self.process(make_docx(self.base/'history.docx',a+' PROJECT_DOC_SENTINEL_654321 HISTORICAL_FAKE_CERTIFICATE_998877'))
        other=self.create('项目 B')
        self.process(make_docx(self.base/'other.docx',b),other)
        self.assertEqual(Documents(self.c).search(self.pid,b)['total'],0)
        self.assertEqual(Documents(self.c).search(other,a)['total'],0)
        self.assertGreater(Documents(self.c).search(self.pid,a)['total'],0)
        self.assertEqual(Documents(self.c).search(self.pid,'GLOBAL_MATERIAL')['total'],0)
        with Store(self.c,readonly=True) as s:
            for t,rows in facts.items():self.assertEqual(rows,[tuple(r) for r in s.db.execute('SELECT * FROM '+t)])
        self.assertEqual(self.counts()['material'],before['material'])
        for term in ('PROJECT_DOC_SENTINEL_654321','HISTORICAL_FAKE_CERTIFICATE_998877'):
            # Mock only the Material search provider's empty hits here; the
            # integration test repeats this gate against actual dowse.
            with mock.patch('jty_materials.query.Rpc') as rpc:
                rpc.return_value.__enter__.return_value.tool.return_value = {'hits': [], 'total_hits': 0}
                self.assertEqual(Query(self.c).search_materials(query=term)['total'],0)
                self.assertEqual(service.list_materials(self.c,q=term)['total'],0)
                with mock.patch.object(service,'system_ok',return_value=True):
                    self.assertNotIn('history.docx',pages.browse(self.c,dict(q=term)))

    def test_literal_chinese_short_terms_nfkc_and_query_syntax(self):
        self.process(make_docx(self.base/'terms.docx','３５ｋＶ MATERIAL_PERCENT_% abc"def abc_def 项目经理'))
        for term in ('35kV','工','项目经理','abc"def','abc_def','MATERIAL_PERCENT_%'):
            if term=='工': continue
            with self.subTest(term=term):self.assertGreater(Documents(self.c).search(self.pid,term)['total'],0)
        self.assertEqual(Documents(self.c).search(self.pid,'质量')['total'],1)
        self.assertEqual(Documents(self.c).search(self.pid,'合')['total'],1)
        self.assertEqual(Documents(self.c).search(self.pid,'%')['total'],1)
        for term in ('','x'*201):
            with self.assertRaises(ValueError):Documents(self.c).search(self.pid,term)

    def test_pagination_and_rebuild_replace_units_without_duplicates(self):
        fid,_=self.process(make_pdf(self.base/'pages.pdf',('native',)*25))
        self.assertEqual(Documents(self.c).search(self.pid,'项目经理')['total'],25)
        self.assertEqual(len(Documents(self.c).search(self.pid,'项目经理',2)['items']),5)
        rid,entry=self.begin(fid,action='rebuild');self.complete(rid,entry)
        self.assertEqual(Documents(self.c).search(self.pid,'项目经理')['total'],25)
        self.assertFalse(self.c.maintenance_file.exists())

    def test_stale_or_foreign_index_identity_never_returns_text(self):
        self.process(make_pdf(self.base/'own.pdf'))
        with __import__('contextlib').closing(document_index.connect(self.c)) as index:
            with index:
                index.execute('INSERT INTO units(project_id,document_id,run_id,unit_id,text_norm) VALUES(?,?,?,?,?)',
                    (self.pid,'forged','forged','forged','foreign secret term'))
        self.assertEqual(Documents(self.c).search(self.pid,'foreign secret term')['total'],0)
