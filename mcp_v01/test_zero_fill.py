"""v0.7 Zero-Fill tests: dual-engine consensus, honest machine verification,
zero-fill import/UI, auto classification/title, exception UX, project intake.

All fixtures are synthetic (合成测试…); no production data touches this suite.
"""
import io
import json
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import zero_fill_support as zf
from zero_fill_support import FakeOcr
from jty_materials.common import Config, atomic_write, digest, now, safe_path
from jty_materials.store import Store
from jty_materials.workflow import import_material
from jty_materials.consensus import (dual_engine_process, disputes_for, resolve_exception,
                                     reclassify, machine_verify_allowed, fact_origin)
from jty_materials.classify import ocr_classify, combine
from jty_materials.normalize import uscc_valid, uscc_complete, uscc_check_digit, canonical_date
from jty_materials import providers
from jty_materials.projects import auto_intake, classify_bucket, _intake_tender_fields
from webui import api, pages, projects as project_pages


def make_config(label):
    base, c = zf.fixture_config(label)
    zf.init_store(c)
    return base, c


def stage_source(c, name, data):
    """Stage an upload source OUTSIDE repository/state, like the webui does."""
    out = c.base.parent / 'sources'
    out.mkdir(parents=True, exist_ok=True)
    path = out / (uuid.uuid4().hex[:8] + '-' + name)
    atomic_write(path, data)
    return path


def license_source(c, credit=None, name=None):
    from PIL import Image
    import io as _io
    path = stage_source(c, 'license.png', b'')
    zf.make_license(path, credit=credit, name=name)
    return path


class NormalizeAndUsccTests(unittest.TestCase):
    def test_uscc_standard_example(self):
        # The appendix example of GB 32100-2015.
        self.assertTrue(uscc_valid('91350100M000100Y43'))

    def test_uscc_generated_and_mutated(self):
        code = uscc_complete('91JTY000000000X01')
        self.assertTrue(uscc_valid(code))
        broken = code[:-1] + ('0' if code[-1] != '0' else '1')
        self.assertFalse(uscc_valid(broken))

    def test_uscc_legacy_fixture_fails_machine_check(self):
        # The weak pre-v0.7 candidate validator accepted TEST…; the machine
        # verification gate must not.
        self.assertFalse(uscc_valid('TEST0000012345678Y'))

    def test_check_digit_deterministic(self):
        self.assertEqual(uscc_check_digit('91350100M000100Y4'), '3')
        self.assertEqual(uscc_complete('91JTY000000000X01'), zf.CREDIT)

    def test_canonical_dates(self):
        self.assertEqual(canonical_date('２００１年０２月０３日'), '2001-02-03')
        self.assertEqual(canonical_date('2001/2/3'), '2001-02-03')
        with self.assertRaises(ValueError):
            canonical_date('2001-02-30')

    def test_names_never_fuzzy(self):
        from jty_materials.normalize import name_key
        self.assertNotEqual(name_key('山西XX建筑工程有限公司'), name_key('山西XX建筑工程有限责任公司'))
        self.assertEqual(name_key(' 山​西  公司 '), name_key('山西公司'))


class ClassifyTests(unittest.TestCase):
    def test_ocr_headers(self):
        self.assertEqual(ocr_classify('营业执照\n统一社会信用代码 X')[0], 'business_license')
        self.assertEqual(ocr_classify('安全生产许可证')[0], 'safety_prod_license')
        self.assertEqual(ocr_classify('中标通知书')[0], 'award_notice')
        self.assertIsNone(ocr_classify('随机内容没有信号')[0])

    def test_rival_headers_conflict(self):
        kind, basis = ocr_classify('营业执照\n安全生产许可证')
        self.assertIsNone(kind)
        self.assertTrue(basis.startswith('rival_headers'))

    def test_combine_agree_and_conflict(self):
        final, basis, conflict = combine(('business_license', 'ocr_header', 1.0),
                                         ('business_license', 'vision', 0.9))
        self.assertEqual((final, conflict), ('business_license', False))
        final, basis, conflict = combine(('business_license', 'ocr_header', 1.0),
                                         ('patent', 'vision', 0.9))
        self.assertIsNone(final)
        self.assertTrue(conflict)
        final, basis, conflict = combine(('business_license', 'ocr_header', 1.0), None)
        self.assertEqual(final, 'business_license')
        self.assertTrue(basis.startswith('ocr_only'))

    def test_machine_verify_policy_excludes(self):
        self.assertFalse(machine_verify_allowed('scope'))
        self.assertFalse(machine_verify_allowed('qualification:输变电'))
        self.assertTrue(machine_verify_allowed('unified_social_credit_code'))


class ZeroFillImportTests(unittest.TestCase):
    def setUp(self):
        self.base, self.c = make_config('import-' + uuid.uuid4().hex[:4])

    def test_zero_fill_import_classifies_and_titles(self):
        source = license_source(self.c)
        receipt = import_material(self.c, source, actor='local_webui',
                                  ocr_factory=FakeOcr(zf.LICENSE_TEXT))
        self.assertEqual(receipt['classification']['final'], 'business_license')
        self.assertFalse(receipt['classification']['conflict'])
        with Store(self.c, readonly=True) as s:
            row = s.db.execute("""SELECT m.title,t.code FROM material m
                JOIN material_type t ON t.type_id=m.material_type_id""").fetchone()
        self.assertEqual(row['code'], 'business_license')
        self.assertIn('合成测试示例建筑工程有限公司', row['title'])

    def test_zero_fill_import_without_vision_uses_ocr_only(self):
        source = license_source(self.c)
        receipt = import_material(self.c, source, actor='local_webui',
                                  ocr_factory=FakeOcr(zf.LICENSE_TEXT), vision_provider=providers.NullProvider())
        self.assertEqual(receipt['classification']['basis'], 'ocr_only:ocr_header')

    def test_classification_conflict_imports_as_other(self):
        source = license_source(self.c)
        mv = zf.MockVision(classify_answer={'document_type': 'patent', 'confidence': 0.93})
        receipt = import_material(self.c, source, actor='local_webui',
                                  ocr_factory=FakeOcr(zf.LICENSE_TEXT), vision_provider=mv)
        self.assertEqual(receipt['classification']['final'], 'other')
        self.assertTrue(receipt['classification']['conflict'])
        with Store(self.c, readonly=True) as s:
            hits = s.db.execute(
                "SELECT count(*) FROM audit_log WHERE action='material.classification_conflict'").fetchone()[0]
        self.assertEqual(hits, 1)

    def test_explicit_cli_import_still_works(self):
        source = license_source(self.c)
        receipt = import_material(self.c, source, kind='certificate', subtype='business_license',
                                  title='手工导入', actor='维护员', ocr_factory=FakeOcr(zf.LICENSE_TEXT))
        self.assertIsNone(receipt['classification'])
        self.assertTrue(receipt['requires_human_review'])


def seed_candidates(c, material_id, original_file_id, rows):
    """Insert OCR-side candidates bound to the current original."""
    with Store(c) as s:
        with s.db:
            run = s.db.execute(
                """INSERT INTO extraction_run(material_id,source_file_id,extractor,extractor_version,params_json,run_at,status)
                   VALUES(?,?,?,?,?,?,?) RETURNING run_id""",
                (material_id, original_file_id, 'umi-ocr+typed-candidate-rules', '2.1.5/test',
                 json.dumps({'text_status': 'complete'}), now(), 'succeeded')).fetchone()[0]
            ids = {}
            for field, value in rows:
                span = json.dumps({'extractor': 'umi-ocr+typed-candidate-rules', 'human_verified': False,
                                   'page': 1, 'basis': 'synthetic',
                                   'original_file_id': original_file_id,
                                   'original_sha256': s.db.execute(
                                       'SELECT sha256 FROM material_file WHERE file_id=?',
                                       (original_file_id,)).fetchone()[0]})
                kind = 'credit'
                vt = 'text' if field == 'enterprise_name' else 'date'
                if field == 'unified_social_credit_code':
                    vt = 'text'
                ids[field] = s.db.execute(
                    """INSERT INTO extracted_fact(run_id,material_id,field_name,value_type,raw_value,normalized_value,page_no,evidence_span)
                       VALUES(?,?,?,?,?,?,?,?) RETURNING extracted_id""",
                    (run, material_id, field, vt, value, value, 1, span)).fetchone()[0]
    return ids


class ConsensusTests(unittest.TestCase):
    def setUp(self):
        self.base, self.c = make_config('cons-' + uuid.uuid4().hex[:4])
        source = license_source(self.c)
        receipt = import_material(self.c, source, kind='certificate', subtype='business_license',
                                  title='共识测试', actor='local_webui', ocr_factory=FakeOcr(zf.LICENSE_TEXT))
        self.mid = receipt['material_id']
        self.original_file_id = receipt['files']['original']
        seed_candidates(self.c, self.mid, self.original_file_id,
                        [('enterprise_name', zf.NAME), ('unified_social_credit_code', zf.CREDIT),
                         ('established_on', zf.ESTABLISHED)])

    def _vision(self, name=None, credit=None, established=None):
        fields = {'enterprise_name': {'value': name or zf.NAME, 'page': 1},
                  'unified_social_credit_code': {'value': credit or zf.CREDIT, 'page': 1},
                  'established_on': {'value': established or zf.ESTABLISHED, 'page': 1}}
        return zf.MockVision(extract_answers=[{'fields': fields}])

    def test_agreement_machine_verifies_all_hard_conditions(self):
        receipt = dual_engine_process(self.c, self.mid, provider=self._vision())
        self.assertEqual(receipt['vision_status'], 'succeeded')
        self.assertEqual(set(receipt['verified']),
                         {'enterprise_name', 'unified_social_credit_code', 'established_on'})
        with Store(self.c, readonly=True) as s:
            org = s.db.execute("""SELECT o.name,o.unified_social_credit_code,o.established_on
                FROM material m JOIN org o ON o.org_id=m.subject_org_id""").fetchone()
            self.assertEqual(org['name'], zf.NAME)
            self.assertEqual(org['unified_social_credit_code'], zf.CREDIT)
            event = s.db.execute("""SELECT verification_method,verified_by FROM verification_event
                WHERE field_name='unified_social_credit_code'""").fetchone()
            status = s.db.execute('SELECT verification_status FROM material WHERE material_id=?',
                                  (self.mid,)).fetchone()[0]
            marks = s.db.execute("""SELECT count(*) FROM extracted_fact e JOIN extraction_run r USING(run_id)
                WHERE e.material_id=? AND e.verification='machine_verified'""", (self.mid,)).fetchone()[0]
        self.assertEqual(event['verification_method'], 'dual_engine_consensus')
        self.assertTrue(event['verified_by'].startswith('machine:'))
        self.assertEqual(status, 'ai_extracted')
        self.assertGreaterEqual(marks, 6)  # both engines' candidates marked
        # ledger records the human-honest actor, never a fake person
        self.assertNotEqual(event['verified_by'], '张三')

    def test_conflict_disputes_only_that_field(self):
        mv = self._vision(credit=zf.CREDIT[:-1] + ('0' if zf.CREDIT[-1] != '0' else '1'))
        receipt = dual_engine_process(self.c, self.mid, provider=mv)
        self.assertEqual(receipt['verified'], {'enterprise_name': zf.NAME, 'established_on': zf.ESTABLISHED})
        self.assertIn('unified_social_credit_code', receipt['disputed'])
        disputes = disputes_for(self.c, self.mid)
        self.assertEqual(len(disputes), 1)
        self.assertEqual(disputes[0]['field'], 'unified_social_credit_code')
        self.assertEqual({x['side'] for x in disputes[0]['sides']}, {'ocr', 'vision'})
        with Store(self.c, readonly=True) as s:
            code = s.db.execute("""SELECT o.unified_social_credit_code FROM material m
                JOIN org o ON o.org_id=m.subject_org_id""").fetchone()[0]
        self.assertIsNone(code)  # no auto write for the disputed field

    def test_broken_check_digit_never_verified(self):
        # Both engines read the same structurally-plausible but checksum-broken
        # code: consensus equality holds, validator must still refuse.
        base, c = make_config('cons-bad-' + uuid.uuid4().hex[:4])
        source = license_source(c)
        receipt = import_material(c, source, kind='certificate', subtype='business_license',
                                  title='坏校验位', actor='local_webui', ocr_factory=FakeOcr(zf.LICENSE_TEXT))
        bad = zf.CREDIT_BAD
        seed_candidates(c, receipt['material_id'], receipt['files']['original'],
                        [('unified_social_credit_code', bad)])
        mv = zf.MockVision(extract_answers=[{'fields': {
            'unified_social_credit_code': {'value': bad, 'page': 1}}}])
        result = dual_engine_process(c, receipt['material_id'], provider=mv)
        self.assertNotIn('unified_social_credit_code', result['verified'])

    def test_human_fact_wins_over_consensus(self):
        with Store(self.c) as s:
            s.verify(self.mid, 'enterprise_name', '另一家测试公司', actor='王审核',
                     reason='人工看原件', evidence_file_id=self.original_file_id)
        receipt = dual_engine_process(self.c, self.mid, provider=self._vision())
        self.assertNotIn('enterprise_name', receipt['verified'])
        self.assertEqual(receipt['disputed']['enterprise_name'].get('existing_human_fact'), True)
        with Store(self.c, readonly=True) as s:
            name = s.db.execute("""SELECT o.name FROM material m JOIN org o ON o.org_id=m.subject_org_id""").fetchone()[0]
        self.assertEqual(name, '另一家测试公司')

    def test_vision_failure_defers_without_touching_original(self):
        mv = zf.MockVision(fail='unavailable')
        receipt = dual_engine_process(self.c, self.mid, provider=mv)
        self.assertEqual(receipt['vision_status'], 'failed')
        with Store(self.c, readonly=True) as s:
            runs = s.db.execute("SELECT status FROM extraction_run WHERE extractor LIKE 'vision:%'").fetchall()
            files = s.db.execute('SELECT count(*) FROM material_file WHERE material_id=?', (self.mid,)).fetchone()[0]
        self.assertEqual(runs[0][0], 'failed')
        self.assertGreaterEqual(files, 4)  # original + derivatives intact

    def test_malformed_vision_json_recorded_as_failure(self):
        class BadJson(zf.MockVision):
            def extract(self, pages, type_code, field_list, *, purpose):
                from jty_materials.providers import ProviderError
                self.calls.append(purpose)
                raise ProviderError('bad_response', 'provider did not return valid JSON')
        receipt = dual_engine_process(self.c, self.mid, provider=BadJson())
        self.assertEqual(receipt['vision_status'], 'failed')

    def test_replace_clears_machine_marks(self):
        dual_engine_process(self.c, self.mid, provider=self._vision())
        source2 = license_source(self.c, name=zf.NAME + '乙')  # different bytes
        receipt = import_material(self.c, source2, material_id=self.mid, actor='local_webui',
                                  ocr_factory=FakeOcr(zf.LICENSE_TEXT))
        with Store(self.c, readonly=True) as s:
            marks = s.db.execute(
                "SELECT count(*) FROM extracted_fact WHERE material_id=? AND verification IS NOT NULL",
                (self.mid,)).fetchone()[0]
            status = s.db.execute('SELECT verification_status FROM material WHERE material_id=?',
                                  (self.mid,)).fetchone()[0]
        self.assertEqual(marks, 0)  # old-original machine marks invalidated
        self.assertEqual(status, 'unverified')


class ExceptionResolutionTests(unittest.TestCase):
    def setUp(self):
        self.base, self.c = make_config('exc-' + uuid.uuid4().hex[:4])
        source = license_source(self.c)
        receipt = import_material(self.c, source, kind='certificate', subtype='business_license',
                                  title='异常测试', actor='local_webui', ocr_factory=FakeOcr(zf.LICENSE_TEXT))
        self.mid = receipt['material_id']
        self.original_file_id = receipt['files']['original']
        seed_candidates(self.c, self.mid, self.original_file_id,
                        [('enterprise_name', zf.NAME), ('unified_social_credit_code', zf.CREDIT)])
        mv = zf.MockVision(extract_answers=[{'fields': {
            'enterprise_name': {'value': zf.NAME, 'page': 1},
            'unified_social_credit_code': {'value': zf.CREDIT[:-1] + ('0' if zf.CREDIT[-1] != '0' else '1'), 'page': 1}}}])
        dual_engine_process(self.c, self.mid, provider=mv)

    def test_select_ocr_candidate_is_honest_human_event(self):
        result = resolve_exception(self.c, {'material_id': self.mid,
                                            'field': 'unified_social_credit_code', 'choice': 'ocr'})
        self.assertEqual(result['resolved'], 'ocr')
        with Store(self.c, readonly=True) as s:
            event = s.db.execute("""SELECT verification_method,verified_by,reason FROM verification_event
                WHERE field_name='unified_social_credit_code' ORDER BY event_id DESC LIMIT 1""").fetchone()
            disputes = s.db.execute(
                "SELECT count(*) FROM extracted_fact WHERE material_id=? AND verification='disputed'",
                (self.mid,)).fetchone()[0]
            code = s.db.execute("""SELECT o.unified_social_credit_code FROM material m
                JOIN org o ON o.org_id=m.subject_org_id""").fetchone()[0]
        self.assertEqual(event['verification_method'], 'manual_original')
        self.assertEqual(event['verified_by'], 'local_webui')
        self.assertEqual(event['reason'], 'human_selected_ocr_candidate')
        self.assertEqual(disputes, 0)
        self.assertEqual(code, zf.CREDIT)

    def test_manual_correction(self):
        good = zf.CREDIT
        resolve_exception(self.c, {'material_id': self.mid, 'field': 'unified_social_credit_code',
                                   'choice': 'manual', 'value': good})
        with Store(self.c, readonly=True) as s:
            event = s.db.execute("""SELECT reason FROM verification_event
                WHERE field_name='unified_social_credit_code' ORDER BY event_id DESC LIMIT 1""").fetchone()
        self.assertEqual(event['reason'], 'human_corrected_conflict')


class ReclassifyTests(unittest.TestCase):
    def test_other_to_business_license_then_verify(self):
        base, c = make_config('recl-' + uuid.uuid4().hex[:4])
        source = license_source(c)
        mv = zf.MockVision(classify_answer={'document_type': 'patent', 'confidence': 0.93})
        receipt = import_material(c, source, actor='local_webui',
                                  ocr_factory=FakeOcr(zf.LICENSE_TEXT), vision_provider=mv)
        self.assertEqual(receipt['classification']['final'], 'other')
        self.assertTrue(receipt['classification']['conflict'])
        mid = receipt['material_id']
        reclassify(c, {'material_id': mid, 'subtype': 'business_license'})
        with Store(c, readonly=True) as s:
            code = s.db.execute("""SELECT t.code FROM material m JOIN material_type t
                ON t.type_id=m.material_type_id WHERE material_id=?""", (mid,)).fetchone()[0]
            detail = s.db.execute('SELECT certificate_kind FROM certificate_detail WHERE material_id=?', (mid,)).fetchone()
        self.assertEqual(code, 'business_license')
        self.assertEqual(detail['certificate_kind'], 'business_license')
        # after reclassification the consensus can run and verify org facts
        original = {'file_id': None}
        with Store(c, readonly=True) as s:
            original['file_id'] = s.db.execute(
                "SELECT file_id FROM material_file WHERE material_id=? AND role='original' AND is_current=1", (mid,)).fetchone()[0]
        seed_candidates(c, mid, original['file_id'],
                        [('enterprise_name', zf.NAME), ('unified_social_credit_code', zf.CREDIT),
                         ('established_on', zf.ESTABLISHED)])
        result = dual_engine_process(c, mid, provider=zf.MockVision(extract_answers=[{'fields': {
            'enterprise_name': {'value': zf.NAME, 'page': 1},
            'unified_social_credit_code': {'value': zf.CREDIT, 'page': 1},
            'established_on': {'value': zf.ESTABLISHED, 'page': 1}}}]))
        self.assertEqual(len(result['verified']), 3)

    def test_reclassify_blocked_after_human_facts(self):
        base, c = make_config('recl2-' + uuid.uuid4().hex[:4])
        source = license_source(c)
        receipt = import_material(c, source, kind='material', subtype='other',
                                  title='手动其他', actor='local_webui', ocr_factory=FakeOcr(zf.LICENSE_TEXT))
        mid = receipt['material_id']
        # a project reference blocks reclassification (same precondition family)
        from jty_materials.projects import execute
        made = execute(c, 'project.create', dict(name='引用项目', actor='local_webui'))
        execute(c, 'project.material.link', dict(project_id=made['project_id'], material_id=mid,
                                                 actor='local_webui'))
        with self.assertRaises(ValueError):
            reclassify(c, {'material_id': mid, 'subtype': 'business_license'})


class ProjectIntakeTests(unittest.TestCase):
    def test_bucket_classification(self):
        self.assertEqual(classify_bucket('某某项目招标文件.pdf'), 'tender_docs')
        self.assertEqual(classify_bucket('中标通知书.pdf'), 'bid_outputs')
        self.assertEqual(classify_bucket('验收报告.pdf'), 'result')
        self.assertEqual(classify_bucket('随机文件.dat'), 'other')

    def test_tender_fields(self):
        fields = _intake_tender_fields(zf.TENDER_TEXT)
        self.assertEqual(fields['name'], '合成测试市政道路工程项目')
        self.assertTrue(fields['tender_no'])
        self.assertEqual(fields['tenderer'], '合成测试城市建设开发有限公司')
        self.assertEqual(fields['lot_name'], '一标段')

    def test_auto_intake_creates_project_and_links_material(self):
        base, c = make_config('intake-' + uuid.uuid4().hex[:4])
        # an existing material whose org credit code appears in the tender text
        source = license_source(c)
        receipt = import_material(c, source, kind='certificate', subtype='business_license',
                                  title='被引用素材', actor='local_webui', ocr_factory=FakeOcr(zf.LICENSE_TEXT))
        mid = receipt['material_id']
        with Store(c) as s:
            s.verify(mid, 'unified_social_credit_code', zf.CREDIT, actor='王审核', reason='人工',
                     evidence_file_id=receipt['files']['original'])
        text = zf.TENDER_TEXT + '\n统一社会信用代码：' + zf.CREDIT + '\n'
        doc = stage_source(c, 'tender.txt', text.encode('utf-8'))
        result = auto_intake(c, dict(source=str(doc), filename='合成测试项目招标文件.txt',
                                     expected_sha256=digest(doc)), 'local_webui')
        self.assertTrue(result['created_project'])
        self.assertTrue(result['saved'])
        self.assertEqual(result['fields']['name'], '合成测试市政道路工程项目')
        self.assertEqual(result['auto_linked_material'], mid)
        with Store(c, readonly=True) as s:
            file_row = s.db.execute('SELECT category FROM project_file WHERE project_id=?',
                                    (result['project_id'],)).fetchone()
        self.assertEqual(file_row['category'], 'tender_docs')

    def test_auto_intake_joins_existing_project(self):
        base, c = make_config('intake2-' + uuid.uuid4().hex[:4])
        from jty_materials.projects import execute
        made = execute(c, 'project.create', dict(name='合成测试市政道路工程项目',
                                                 tender_no='JTYZB-2026-EXIST', actor='local_webui'))
        doc = stage_source(c, 'tender.txt', zf.TENDER_TEXT.encode('utf-8'))
        result = auto_intake(c, dict(source=str(doc), filename='招标文件.txt',
                                     expected_sha256=digest(doc)), 'local_webui')
        self.assertFalse(result['created_project'])
        self.assertEqual(result['project_id'], made['project_id'])
        self.assertEqual(result['matched_by'], 'name')


class ProviderTests(unittest.TestCase):
    def test_config_requires_credential(self):
        base, c = make_config('prov-' + uuid.uuid4().hex[:4])
        with mock.patch.dict('os.environ', {}, clear=False):
            import os
            os.environ.pop('OPENCODE_GO_API_KEY', None)
            self.assertIsNone(providers.provider_config(c))
            self.assertIsInstance(providers.vision_provider(c), providers.NullProvider)

    def test_extract_json_tolerant(self):
        self.assertEqual(providers.extract_json('前缀 {"a":1} 后缀'), {'a': 1})
        self.assertEqual(providers.extract_json('```json\n{"b":[1]}\n```'), {'b': [1]})
        self.assertIsNone(providers.extract_json('完全不是 JSON'))

    def test_provider_logs_never_contain_key(self):
        base, c = make_config('prov2-' + uuid.uuid4().hex[:4])
        key = 'oc_sk_test_secret_value'
        import os
        with mock.patch.dict('os.environ', {}, clear=False):
            os.environ.pop('OPENCODE_GO_API_KEY', None)
            c.raw['vision_provider'] = {'api_key': key}
            p = providers.OpenCodeGoProvider(c)
            self.assertEqual(p.api_key, key)
        zf.assert_no_secret(c, key)


class ZeroFillUITests(unittest.TestCase):
    def setUp(self):
        self.base, self.c = make_config('ui-' + uuid.uuid4().hex[:4])

    def test_upload_page_has_no_required_fill(self):
        html = pages.upload_page(self.c, {})
        self.assertNotIn('name="actor"', html)
        self.assertNotIn('required', html.split('upload-advanced')[0].split('<form')[1].split('</form>')[0])
        self.assertIn('data-mode="import"', html)

    def test_replace_page_has_no_actor(self):
        source = license_source(self.c)
        receipt = import_material(self.c, source, kind='certificate', subtype='business_license',
                                  title='UI测试', actor='local_webui', ocr_factory=FakeOcr(zf.LICENSE_TEXT))
        html = pages.upload_page(self.c, {'material': str(receipt['material_id'])})
        self.assertNotIn('name="actor"', html)

    def test_detail_page_original_first_and_exception_first(self):
        source = license_source(self.c)
        receipt = import_material(self.c, source, kind='certificate', subtype='business_license',
                                  title='详情测试', actor='local_webui', ocr_factory=FakeOcr(zf.LICENSE_TEXT))
        mid = receipt['material_id']
        seed_candidates(self.c, mid, receipt['files']['original'],
                        [('enterprise_name', zf.NAME), ('unified_social_credit_code', zf.CREDIT)])
        mv = zf.MockVision(extract_answers=[{'fields': {
            'enterprise_name': {'value': zf.NAME, 'page': 1},
            'unified_social_credit_code': {'value': zf.CREDIT[:-1] + '0', 'page': 1}}}])
        dual_engine_process(self.c, mid, provider=mv)
        html = pages.material_page(self.c, mid)
        self.assertIn('exception-banner', html)
        self.assertIn('有 1 项需要处理', html)
        self.assertIn('data-resolve-choice="ocr"', html)
        self.assertIn('data-resolve-choice="vision"', html)
        self.assertIn('original-img', html)  # big original is the body
        self.assertNotIn('完善资料', html.split('more-menu')[0])
        self.assertNotIn('确认说明', html)
        self.assertNotIn('经办人', html)

    def test_detail_page_machine_verified_chip(self):
        source = license_source(self.c)
        receipt = import_material(self.c, source, kind='certificate', subtype='business_license',
                                  title='核验徽标测试', actor='local_webui', ocr_factory=FakeOcr(zf.LICENSE_TEXT))
        mid = receipt['material_id']
        seed_candidates(self.c, mid, receipt['files']['original'],
                        [('enterprise_name', zf.NAME), ('unified_social_credit_code', zf.CREDIT),
                         ('established_on', zf.ESTABLISHED)])
        mv = zf.MockVision(extract_answers=[{'fields': {
            'enterprise_name': {'value': zf.NAME, 'page': 1},
            'unified_social_credit_code': {'value': zf.CREDIT, 'page': 1},
            'established_on': {'value': zf.ESTABLISHED, 'page': 1}}}])
        dual_engine_process(self.c, mid, provider=mv)
        html = pages.material_page(self.c, mid)
        self.assertIn('✓ 已自动核验', html)
        self.assertNotIn('有 1 项需要处理', html)

    def test_new_project_page_intake_first(self):
        html = project_pages.new_page(self.c)
        self.assertIn('project.auto_intake', html)
        self.assertNotIn('name="actor"', html)
        self.assertNotIn('name="category"', html)

    def test_project_upload_page_zero_fill(self):
        from jty_materials.projects import execute
        made = execute(self.c, 'project.create', dict(name='项目A', actor='local_webui'))
        html = project_pages.upload_page(self.c, made['project_id'])
        self.assertNotIn('name="actor"', html)
        self.assertNotIn('name="category"', html)

    def test_archive_form_has_no_actor(self):
        from jty_materials.projects import execute
        made = execute(self.c, 'project.create', dict(name='项目B', actor='local_webui'))
        html = project_pages.detail_page(self.c, made['project_id'], {})
        self.assertNotIn('经办人', html)


class ApiZeroFillTests(unittest.TestCase):
    def setUp(self):
        self.base, self.c = make_config('api-' + uuid.uuid4().hex[:4])
        # Jobs must not actually run: a real import/reextract would spawn a
        # serialized manager for this throwaway fixture config.
        patcher = mock.patch.object(api.uploads.UploadManager, '_run', lambda self, job, path: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = api.Api(self.c)

    def _request(self, query, body=b'', length=None):
        import json as _json

        class FakeRequest:
            def json(self, _body=body):
                return _json.loads(_body.decode('utf-8')) if _body else None
        r = FakeRequest()
        r.query = query
        r.rfile = io.BytesIO(body)
        r.headers = {'Content-Length': str(length if length is not None else len(body))}
        return r

    def test_upload_rejects_missing_body_but_accepts_zero_fill_query(self):
        request = self._request({'mode': 'import', 'filename': 'a.png'})
        request.headers = {}  # no Content-Length at all
        response = self.api.upload(request)
        self.assertEqual(response[0], 411)  # no body → length error, NOT an actor error

    def test_upload_without_subtype_starts_job(self):
        body = b'not-a-real-image'  # staging only checks size; classification happens in the job
        response = self.api.upload(self._request({'mode': 'import', 'filename': 'b.png'}, body))
        self.assertEqual(response[0], 202)
        job = self.api.uploader.get(response[1]['job_id'])
        self.assertEqual(job.params['actor'], 'local_webui')
        self.assertEqual(job.params['subtype'], '')

    def test_reextract_requires_no_actor(self):
        response = self.api.reextract(self._request({}, json.dumps({'material_id': 1}).encode()))
        self.assertEqual(response[0], 404)  # material missing; actor no longer validated


if __name__ == '__main__':
    unittest.main(verbosity=1)
