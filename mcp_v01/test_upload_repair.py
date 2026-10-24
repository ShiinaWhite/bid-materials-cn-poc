"""v0.6.1 extraction, trust boundaries, title and unchanged-original tests."""
import io, json, os, sys, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import test_v01 as old
from jty_materials.business import extract_candidates
from jty_materials.common import Config, atomic_write, digest, safe_path
from jty_materials.query import Query
from jty_materials.store import Store
from jty_materials.workflow import import_material, review_fact
from jty_materials.reextract import current_record, reextract, existing_ocr
from webui import api, uploads, pages, display, service, catalog
from upload_repair_support import TEXT, EXPECTED, NAME, CREDIT, make_license


class LicenseExtractorTests(unittest.TestCase):
    def values(self, text):
        return {f: n for f, vt, raw, n in extract_candidates(text, 'business_license', {})}

    def test_labeled_company_name(self):
        for label in ['名称 ', '名称：', '名称\n', '企业名称:']:
            with self.subTest(label=label):
                self.assertEqual(self.values(label+NAME)['enterprise_name'], NAME)

    def test_spaced_labels_and_values(self):
        self.assertEqual(self.values('名 称 ： 合 成 测 试 示 例 建 筑 工 程 有 限 公 司')['enterprise_name'], NAME)
        self.assertEqual(self.values('成 立 日 期 ： 2001 年 02 月 03 日')['established_on'], '2001-02-03')

    def test_credit_with_ocr_spaces(self):
        for code in [CREDIT, CREDIT[:-1]+' Y', ' '.join(CREDIT)]:
            self.assertEqual(self.values('统 一 社 会 信 用 代 码\n'+code)['unified_social_credit_code'], CREDIT)

    def test_invalid_credit_rejected(self):
        for code in [CREDIT[:-1], CREDIT+'1', ' '.join(CREDIT+'1'), CREDIT[:-1]+'?', CREDIT.lower()]:
            with self.subTest(code=code):
                self.assertNotIn('unified_social_credit_code', self.values('统一社会信用代码 '+code))

    def test_credit_requires_label(self):
        self.assertNotIn('unified_social_credit_code', self.values('其他编号 '+CREDIT))

    def test_established_date(self):
        self.assertEqual(self.values(TEXT)['established_on'], '2001-02-03')

    def test_unrelated_stamp_date_not_selected(self):
        self.assertNotIn('established_on', self.values('登记机关\n2031年12月19日'))
        self.assertEqual(self.values(TEXT)['established_on'], '2001-02-03')

    def test_invalid_date_not_selected(self):
        self.assertNotIn('established_on', self.values('成立日期 2001年02月30日\n登记机关 2031年12月19日'))

    def test_duplicate_candidates_deduped(self):
        rows = list(extract_candidates(TEXT+'\n'+TEXT, 'business_license', {}))
        self.assertEqual(len(rows), 3)

    def test_original_evidence_preserved_after_normalization(self):
        text = '名 称：'+NAME+'\n统一社会信用代码：ＴＥＳＴ０００００１２３４５６７８Ｙ\n成立日期：２００１年０２月０３日'
        for field, vt, raw, value in extract_candidates(text, 'business_license', {}):
            self.assertIn(raw, text)
            self.assertEqual(value, EXPECTED[field])

    def test_company_cannot_be_footer_or_license_heading(self):
        for word in ['营业执照', '登记机关', '国家企业信用信息公示系统网址', 'http://www.gsxt.gov.cn']:
            self.assertNotIn('enterprise_name', self.values('名称 '+word))

    def test_filename_is_not_enterprise_evidence(self):
        self.assertEqual(list(extract_candidates('', 'business_license', {'source_title':NAME, 'source_relative_path':CREDIT+'.jpg'})), [])

    def test_existing_types_share_extractor(self):
        for typ, text, field in [('contract','合同金额:123.00元','contract_amount'), ('patent','专利名称:测试保护装置','patent_name'),
            ('software_copyright','软件名称:工程管理软件','software_name'), ('audit_report','2024年度财务报表','fiscal_year'),
            ('tax_credit','2024年度 纳税信用A级','year'), ('person_registered_cert','证书编号:TEST-100','certificate_no')]:
            self.assertIn(field, {r[0] for r in extract_candidates(text, typ, {})})


class UploadRepairTests(unittest.TestCase):
    setUp = old.V01Tests.setUp

    def add(self, title='', text=TEXT, typ='business_license'):
        path=self.base/('license-'+str(time.time_ns())+'.jpg');path.write_bytes(path.name.encode())
        params=dict(actor='fixture', subtype=typ, title=title)
        job=uploads.UploadJob('fixture','import','营业执照-新.jpg',path.stat().st_size,params)
        def rpc(c, action, payload, **kw):
            return import_material(c, **payload, ocr_factory=lambda c: old.FakeOcr(c,text))
        with mock.patch.object(uploads,'request',side_effect=rpc):
            uploads.UploadManager(self.c)._run(job,path)
        self.assertEqual(job.state,'done',job.error)
        return Query(self.c).get_material(job.result['material_id'])

    def test_web_upload_enhanced_context_and_evidence_pending(self):
        m=self.add();self.assertEqual({f:v[0]['value'] for f,v in m['candidate_fields'].items()},EXPECTED)
        self.assertEqual(m['source_context']['source_origin'],'webui')
        self.assertEqual(m['source_context']['source_filename'],'营业执照-新.jpg')
        self.assertEqual(m['verified_facts'],{})
        with Store(self.c,readonly=True) as s:
            self.assertEqual(s.db.execute('select name,unified_social_credit_code,established_on from org where org_id=(select subject_org_id from material where material_id=?)',(m['material_id'],)).fetchone()[0],'')
            self.assertEqual(s.db.execute('select count(*) from verification_event').fetchone()[0],0)
            for r in s.db.execute('select e.*,x.source_file_id from extracted_fact e join extraction_run x using(run_id) where e.material_id=?',(m['material_id'],)):
                self.assertEqual(r['review_status'],'pending');self.assertTrue(r['raw_value']);self.assertTrue(r['evidence_span']);self.assertTrue(r['run_id']);self.assertTrue(r['source_file_id'])

    def test_supplied_title_respected(self):
        m=self.add('我的自定义标题');self.assertEqual(m['title'],'我的自定义标题')
        self.assertEqual(display.display_from_detail(m),'我的自定义标题')
        self.assertEqual(service.summarize(self.c,[m['material_id']])[0]['display'],'我的自定义标题')

    def test_empty_whitespace_title_fallback_and_suggestion(self):
        for title in ['', '  \t ']:
            m=self.add(title);self.assertEqual(m['title'],NAME+' 营业执照')  # v0.7 auto title
            self.assertEqual(display.display_from_detail(m),NAME+' 营业执照')
            self.assertEqual(service.summarize(self.c,[m['material_id']])[0]['display'],NAME+' 营业执照')

    def test_ambiguous_name_keeps_filename(self):
        m=self.add(text=TEXT+'\n名称 另一家测试建筑有限公司')
        self.assertEqual(display.display_from_detail(m),'营业执照-新')
        self.assertEqual(service.summarize(self.c,[m['material_id']])[0]['display'],'营业执照-新')

    def test_non_license_fallback_nonempty(self):
        m=self.add(typ='software_copyright',text='无可识别字段')
        self.assertTrue(m['title']);self.assertTrue(display.display_from_detail(m))

    def test_web_upload_existing_types_reuse_enhanced_extraction(self):
        for typ,text,field in [('contract','合同金额:123.00元','contract_amount'),('patent','专利名称:测试装置','patent_name'),
            ('software_copyright','软件名称:测试软件','software_name'),('audit_report','2024年度财务报表','fiscal_year'),
            ('tax_credit','2024年度 纳税信用A级','year'),('person_registered_cert','证书编号:TEST-100','certificate_no')]:
            self.assertIn(field,self.add(typ=typ,text=text)['candidate_fields'])

    def test_api_optional_title_and_no_trusted_metadata_injection(self):
        instance=api.Api(self.c)
        for title in [None,'',' \t ','旧前端自填标题']:
            q=dict(filename='C:/untrusted/营业执照.jpg',subtype='business_license',actor='fixture',source_metadata={'project_name':'fake'},
                path='C:/secret',ocr_text=TEXT,candidates=json.dumps(EXPECTED))
            if title is not None:q['title']=title
            req=SimpleNamespace(query=q,headers={'Content-Length':'3'},rfile=io.BytesIO(b'png'))
            with mock.patch.object(instance.uploader,'submit',return_value=uploads.UploadJob('x','import','营业执照.jpg',3,{})) as submit:
                code,result=instance.upload(req)
                self.assertEqual(code,202)
                args=submit.call_args.args
                self.assertEqual(args[2],'营业执照.jpg')
                self.assertEqual(set(args[4]),{'actor','subtype','title','material_id'})
                args[1].unlink()

    def test_worker_context_is_server_constructed(self):
        context=uploads.source_context('C:/fake/营业执照-新.jpg','  ')
        self.assertEqual(context['source_relative_path'],'营业执照-新.jpg')
        self.assertEqual(context['source_title'],'营业执照-新')
        self.assertEqual(context['title_origin'],'filename_fallback')

    def test_reextract_same_original_manual_facts_and_pending_candidates(self):
        m=self.add();mid=m['material_id'];o=next(f for f in m['files'] if f['role']=='original')
        review_fact(self.c,dict(material_id=mid,field='enterprise_name',value=NAME,actor='fixture human',reason='test manual review',evidence_file_id=o['file_id'],evidence_sha256=o['sha256'],candidate_id=m['candidate_fields']['enterprise_name'][0]['extracted_id']))
        before=Query(self.c).get_material(mid)
        with Store(self.c,readonly=True) as s:
            events=[tuple(r) for r in s.db.execute('select * from verification_event')]
            material_count=s.db.execute('select count(*) from material').fetchone()[0]
        record=current_record(self.c,dict(material_id=mid,actor='fixture'))
        reextract(self.c,record,ocr_factory=lambda c:old.FakeOcr(c,TEXT))
        after=Query(self.c).get_material(mid)
        self.assertEqual(before['verified_facts'],after['verified_facts'])
        self.assertEqual([f for f in before['files'] if f['role']=='original'],[f for f in after['files'] if f['role']=='original'])
        self.assertEqual(digest(safe_path(self.c.repository_root,o['relative_path'])),o['sha256'])
        with Store(self.c,readonly=True) as s:
            self.assertEqual([tuple(r) for r in s.db.execute('select * from verification_event')],events)
            self.assertEqual(s.db.execute('select count(*) from material').fetchone()[0],material_count)
            self.assertEqual(s.db.execute("select count(distinct version_id) from material_file where material_id=?",(mid,)).fetchone()[0],1)
            self.assertEqual(s.db.execute("select count(*) from extracted_fact where run_id=(select max(run_id) from extraction_run) and review_status='pending'").fetchone()[0],3)
            self.assertEqual(s.db.execute("select count(*) from extracted_fact where material_id=? and review_status='superseded'",(mid,)).fetchone()[0],2)

    def test_reextract_accepts_only_id_actor_and_checks_sha(self):
        m=self.add();payload=dict(material_id=m['material_id'],actor='fixture')
        for key in ['path','ocr_text','candidates','source_metadata','evidence_sha256']:
            with self.assertRaises(ValueError):current_record(self.c,dict(payload,**{key:'untrusted'}))
        record=current_record(self.c,payload)
        record['evidence_sha256']='0'*64
        with self.assertRaises(ValueError):reextract(self.c,record,ocr_factory=lambda c:self.fail('must not OCR'))
        instance=api.Api(self.c)
        for value in [dict(payload,path='C:/secret'),dict(payload,material_id=True),dict(payload,actor=' ')]:
            self.assertEqual(instance.reextract(SimpleNamespace(json=lambda:value))[0],400)

    def test_cli_reextract_sends_only_id_and_actor(self):
        import cli
        args=['cli.py','--config',str(self.c.path),'reextract','--material-id','1','--actor','fixture']
        with mock.patch.object(sys,'argv',args),mock.patch.object(cli,'request',return_value={'ok':True}) as rpc:
            self.assertEqual(cli.main(),{'ok':True})
            self.assertEqual(rpc.call_args.args[1:],('reextract_current',dict(material_id=1,actor='fixture')))
        with mock.patch.object(sys,'argv',args+['--record','untrusted.json']),mock.patch('sys.stderr',io.StringIO()):
            with self.assertRaises(SystemExit):cli.main()

    def test_legacy_upload_without_source_context_can_reextract(self):
        path=self.base/'legacy.jpg';path.write_bytes(b'legacy original')
        with mock.patch('jty_materials.business.extract_candidates',return_value=[]):
            receipt=import_material(self.c,path,kind='material',subtype='business_license',title='旧资料标题',actor='fixture',ocr_factory=lambda c:old.FakeOcr(c,TEXT))
        mid=receipt['material_id'];before=Query(self.c).get_material(mid)
        self.assertEqual(before['candidate_fields'],{});self.assertIsNone(before['source_context'])
        reextract(self.c,current_record(self.c,dict(material_id=mid,actor='fixture')),ocr_factory=lambda c:old.FakeOcr(c,TEXT))
        after=Query(self.c).get_material(mid)
        self.assertEqual({f:v[0]['value'] for f,v in after['candidate_fields'].items()},EXPECTED)
        self.assertEqual(after['title'],before['title'])
        self.assertEqual(receipt['original_sha256'],digest(path))

    def test_reextract_reuses_current_ocr_with_checked_provenance(self):
        m=self.add();record=current_record(self.c,dict(material_id=m['material_id'],actor='fixture'))
        cached=existing_ocr(self.c,record)
        with mock.patch('jty_materials.reextract.PhaseOcr',side_effect=AssertionError('existing OCR requires no engine restart')):
            receipt=reextract(self.c,record,ocr_factory=lambda _:cached)
        with Store(self.c,readonly=True) as s:
            params=json.loads(s.db.execute('select params_json from extraction_run where run_id=?',(receipt['run_id'],)).fetchone()[0])
            self.assertFalse(params['ocr_engine_rerun']);self.assertEqual(len(params['reused_files']),3)

    def test_reextract_rejects_changed_cached_ocr_bytes(self):
        m=self.add();record=current_record(self.c,dict(material_id=m['material_id'],actor='fixture'))
        cached=existing_ocr(self.c,record)
        raw=next(f for f in m['files'] if f['role']=='ocr_raw_text')
        safe_path(self.c.repository_root,raw['relative_path']).write_text('tampered fixture',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'checksum'):
            reextract(self.c,record,ocr_factory=lambda _:cached)

    def test_partial_ocr_is_not_promoted_to_complete_by_reuse(self):
        class Partial(old.FakeOcr):
            def document(self,path):
                result=super().document(path)
                result['options']=dict(text_status='partial',errors=['fixture unreadable page'])
                return result
        path=self.base/'partial-license.pdf';path.write_bytes(b'partial fixture')
        r=import_material(self.c,path,kind='material',subtype='business_license',title='partial',actor='fixture',ocr_factory=lambda c:Partial(c,TEXT))
        record=current_record(self.c,dict(material_id=r['material_id'],actor='fixture'))
        self.assertIsNone(existing_ocr(self.c,record))

    def test_web_replace_preserves_custom_title_and_demotes_person_relation(self):
        path=self.base/'person.pdf';path.write_bytes(b'person original')
        r=import_material(self.c,path,kind='material',subtype='person_registered_cert',title='自填人员证书标题',actor='fixture',
            source_metadata=dict(person_label='测试人员',person_source_key='fixture-person',person_relation_status='candidate'),
            ocr_factory=lambda c:old.FakeOcr(c,'证书编号:TEST-100'))
        path=self.base/'replacement.jpg';path.write_bytes(b'person replacement')
        job=uploads.UploadJob('replace','replace','新文件.jpg',18,dict(actor='fixture',material_id=r['material_id']))
        def rpc(c,action,payload,**kw):return import_material(c,**payload,ocr_factory=lambda c:old.FakeOcr(c,'证书编号:TEST-101'))
        with mock.patch.object(uploads,'request',side_effect=rpc):uploads.UploadManager(self.c)._run(job,path)
        self.assertEqual(job.state,'done',job.error)
        after=Query(self.c).get_material(r['material_id'])
        self.assertEqual(after['title'],'自填人员证书标题')
        self.assertEqual(after['persons'][0]['relation_status'],'relation_pending')
        self.assertEqual(after['source_context']['title_origin'],'preserved')

    def test_replace_and_import_use_same_enhanced_extractor(self):
        m=self.add();source=self.base/'replacement.jpg';source.write_bytes(b'new original bytes')
        import_material(self.c,source,material_id=m['material_id'],actor='fixture',ocr_factory=lambda c:old.FakeOcr(c,TEXT))
        m=Query(self.c).get_material(m['material_id'])
        self.assertEqual({f:v[0]['value'] for f,v in m['candidate_fields'].items()},EXPECTED)
        self.assertEqual(m['verified_facts'],{})
        # v0.7: the deterministic title built at first import persists; stale
        # candidate hints are never re-applied after a replace
        self.assertEqual(display.display_from_detail(m),NAME+' 营业执照')

    def test_detail_pending_title_note_no_expiry_warning_and_optional_form(self):
        m=self.add()
        with mock.patch.object(service,'system_ok',return_value=True):
            html=pages.material_page(self.c,m['material_id'])
            form=pages.upload_page(self.c,{})
        for value in EXPECTED.values():self.assertIn(value,html)
        self.assertIn('待完善',html);self.assertIn('名称由识别内容自动生成',html);self.assertIn('重新识别',html)
        self.assertNotIn('有效期未记录',html);self.assertNotIn('暂未录入企业名称',html)
        # v0.7 zero-fill: name/category are default-hidden advanced overrides
        self.assertIn('自定义显示名称',form);self.assertNotIn('name="actor"',form)
        self.assertEqual(catalog.UPLOAD_EXTS,('.pdf','.png','.jpg','.jpeg'))
