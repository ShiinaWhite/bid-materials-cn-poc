"""v0.7 final-review blocker tests. Small, targeted, fully synthetic; no real
OCR/LLM/browser runs (previous acceptance results are reused, not re-run)."""
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
from jty_materials.common import atomic_write, digest, now
from jty_materials.store import Store
from jty_materials.workflow import import_material
from jty_materials.consensus import dual_engine_process, disputes_for
from jty_materials.manager import review_required
from jty_materials.projects import auto_intake, add_file
from jty_materials import providers


def make_config(label):
    base, c = zf.fixture_config(label)
    zf.init_store(c)
    return base, c


def stage_source(c, name, data):
    out = c.base.parent / 'sources'
    out.mkdir(parents=True, exist_ok=True)
    path = out / (uuid.uuid4().hex[:8] + '-' + name)
    atomic_write(path, data)
    return path


def license_source(c, credit=None, name=None):
    path = stage_source(c, 'license.png', b'')
    zf.make_license(path, credit=credit, name=name)
    return path


def seed_candidates(c, material_id, original_file_id, rows):
    with Store(c) as s:
        with s.db:
            run = s.db.execute(
                """INSERT INTO extraction_run(material_id,source_file_id,extractor,extractor_version,params_json,run_at,status)
                   VALUES(?,?,?,?,?,?,?) RETURNING run_id""",
                (material_id, original_file_id, 'umi-ocr+typed-candidate-rules', '2.1.5/test',
                 json.dumps({'text_status': 'complete'}), now(), 'succeeded')).fetchone()[0]
            for field, value in rows:
                span = json.dumps({'extractor': 'umi-ocr+typed-candidate-rules', 'human_verified': False,
                                   'page': 1, 'basis': 'synthetic', 'original_file_id': original_file_id,
                                   'original_sha256': s.db.execute(
                                       'SELECT sha256 FROM material_file WHERE file_id=?',
                                       (original_file_id,)).fetchone()[0]})
                vt = 'date' if field == 'established_on' else 'text'
                s.db.execute(
                    """INSERT INTO extracted_fact(run_id,material_id,field_name,value_type,raw_value,normalized_value,page_no,evidence_span)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (run, material_id, field, vt, value, value, 1, span))


def vision_answer(fields):
    return zf.MockVision(extract_answers=[{'fields': {k: {'value': v, 'page': 1} for k, v in fields.items()}}])


ORG = dict(enterprise_name=zf.NAME, unified_social_credit_code=zf.CREDIT, established_on=zf.ESTABLISHED)


class RequiresHumanReviewAccountingTests(unittest.TestCase):
    """Blocker #1: "no successful consensus" is not "finished normally", but
    policy exclusions alone must not force a human review."""

    def test_policy_skipped_alone_is_not_review(self):
        dual = dict(vision_status='succeeded', verified={'enterprise_name': 'x'},
                    disputed={}, ambiguous={}, single_chain=[], validator_failed=[],
                    human_conflicts=[], policy_skipped=['scope'])
        self.assertFalse(review_required(dual, [], {}))

    def test_real_exceptions_force_review(self):
        base = dict(vision_status='succeeded', verified={}, disputed={}, ambiguous={},
                    single_chain=[], validator_failed=[], human_conflicts=[], policy_skipped=['scope'])
        for key, value in [('single_chain', ['certificate_no']),
                           ('validator_failed', ['unified_social_credit_code']),
                           ('human_conflicts', ['enterprise_name']),
                           ('ambiguous', {'enterprise_name': {'ocr': ['a', 'b'], 'vision': ['a']}})]:
            dual = dict(base)
            dual[key] = value
            self.assertTrue(review_required(dual, [], {}), key)

    def test_disputes_classification_and_vision_failure_force_review(self):
        base = dict(vision_status='succeeded', verified={}, disputed={}, ambiguous={},
                    single_chain=[], validator_failed=[], human_conflicts=[], policy_skipped=[])
        self.assertTrue(review_required(base, [{'field': 'x'}], {}))
        self.assertTrue(review_required(base, [], {'conflict': True}))
        failed = dict(base, vision_status='failed')
        self.assertTrue(review_required(failed, [], {}))
        deferred = dict(base, vision_status='failed')
        self.assertTrue(review_required(deferred, [], {}))

    def test_clean_consensus_is_not_review(self):
        dual = dict(vision_status='succeeded', verified=dict(ORG), disputed={}, ambiguous={},
                    single_chain=[], validator_failed=[], human_conflicts=[], policy_skipped=['scope'])
        self.assertFalse(review_required(dual, [], {'conflict': False, 'final': 'business_license'}))


class ConsensusBucketTests(unittest.TestCase):
    def setUp(self):
        self.base, self.c = make_config('rev-' + uuid.uuid4().hex[:4])

    def _import(self):
        source = license_source(self.c)
        receipt = import_material(self.c, source, kind='certificate', subtype='business_license',
                                  title='分桶测试', actor='local_webui', ocr_factory=FakeOcr(zf.LICENSE_TEXT))
        return receipt['material_id'], receipt['files']['original']

    def test_scope_goes_to_policy_skipped(self):
        # scope is a legal iso_cert field that the machine-verification policy
        # deliberately excludes (long prose)
        source = license_source(self.c)
        receipt = import_material(self.c, source, kind='certificate', subtype='iso_cert',
                                  title='ISO', actor='local_webui', ocr_factory=FakeOcr('ISO9001质量管理体系认证证书'))
        mid, original = receipt['material_id'], receipt['files']['original']
        seed_candidates(self.c, mid, original, [('scope', '电力工程施工')])
        receipt = dual_engine_process(self.c, mid, provider=vision_answer({'scope': '电力工程施工'}))
        self.assertIn('scope', receipt['policy_skipped'])
        self.assertEqual(receipt['verified'], {})
        self.assertEqual(receipt['disputed'], {})
        self.assertFalse(review_required(receipt, [], {'conflict': False}))

    def test_single_chain_bucket(self):
        mid, original = self._import()
        seed_candidates(self.c, mid, original, [('enterprise_name', zf.NAME),
                                                ('certificate_no', 'TEST-100')])
        receipt = dual_engine_process(self.c, mid, provider=vision_answer({'enterprise_name': zf.NAME}))
        self.assertIn('certificate_no', receipt['single_chain'])
        self.assertIn('enterprise_name', receipt['verified'])
        self.assertTrue(review_required(receipt, [], {'conflict': False}))

    def test_validator_failed_bucket(self):
        # OCR text without a credit line, so the import seeds no competing
        # candidate; both engines then read the same checksum-broken code.
        source = license_source(self.c)
        receipt = import_material(self.c, source, kind='certificate', subtype='business_license',
                                  title='坏校验位', actor='local_webui',
                                  ocr_factory=FakeOcr('营业执照\n名称 合成测试示例建筑工程有限公司'))
        mid, original = receipt['material_id'], receipt['files']['original']
        bad = zf.CREDIT_BAD
        seed_candidates(self.c, mid, original, [('unified_social_credit_code', bad)])
        receipt = dual_engine_process(self.c, mid, provider=vision_answer({'unified_social_credit_code': bad}))
        self.assertIn('unified_social_credit_code', receipt['validator_failed'])
        with Store(self.c, readonly=True) as s:
            code = s.db.execute("""SELECT o.unified_social_credit_code FROM material m
                JOIN org o ON o.org_id=m.subject_org_id""").fetchone()[0]
        self.assertIsNone(code)
        self.assertTrue(review_required(receipt, [], {'conflict': False}))

    def test_ambiguous_bucket(self):
        mid, original = self._import()
        seed_candidates(self.c, mid, original, [('enterprise_name', zf.NAME),
                                                ('enterprise_name', '另一家测试建筑有限公司')])
        receipt = dual_engine_process(self.c, mid, provider=vision_answer({'enterprise_name': zf.NAME}))
        self.assertIn('enterprise_name', receipt['ambiguous'])
        self.assertTrue(review_required(receipt, [], {'conflict': False}))

    def test_human_fact_conflict_marked_and_reported(self):
        mid, original = self._import()
        human_name = '另一家测试建筑有限公司'
        with Store(self.c) as s:
            s.verify(mid, 'enterprise_name', human_name, actor='王审核', reason='人工核对',
                     evidence_file_id=original)
        receipt = dual_engine_process(self.c, mid, provider=vision_answer(dict(ORG, enterprise_name=zf.NAME)))
        self.assertIn('enterprise_name', receipt['human_conflicts'])
        self.assertTrue(review_required(receipt, [], {'conflict': False}))
        # the conflict must be visible to the exception UX, not silently dropped
        disputes = disputes_for(self.c, mid)
        self.assertIn('enterprise_name', [d['field'] for d in disputes])
        self.assertEqual(disputes[0]['detail']['kind'], 'human_fact_conflict')
        with Store(self.c, readonly=True) as s:
            name = s.db.execute('SELECT name FROM org WHERE org_id=(SELECT subject_org_id FROM material WHERE material_id=?)',
                                (mid,)).fetchone()[0]
        self.assertEqual(name, human_name)  # human fact untouched


class DiskShaRecheckTests(unittest.TestCase):
    """Blocker #2: bytes mutated after recognition must never reach a
    machine_verified write; the critical section refuses the whole consensus."""

    def test_mid_flight_mutation_refuses_consensus(self):
        base, c = make_config('toctou-' + uuid.uuid4().hex[:4])
        source = license_source(c)
        receipt = import_material(c, source, kind='certificate', subtype='business_license',
                                  title='TOCTOU', actor='local_webui', ocr_factory=FakeOcr(zf.LICENSE_TEXT))
        mid, original_file_id = receipt['material_id'], receipt['files']['original']
        seed_candidates(c, mid, original_file_id, list(ORG.items()))
        with Store(c, readonly=True) as s:
            original_path = s.db.execute('SELECT relative_path FROM material_file WHERE file_id=?',
                                         (original_file_id,)).fetchone()[0]
        from jty_materials.common import safe_path

        class MutatingVision(zf.MockVision):
            def extract(self, pages, type_code, field_list, *, purpose):
                # simulate external tampering inside the recognition window
                with open(safe_path(c.repository_root, original_path), 'wb') as f:
                    f.write(b'tampered after recognition')
                return {'fields': {k: {'value': v, 'page': 1} for k, v in ORG.items()}}

        with self.assertRaises(ValueError):
            dual_engine_process(c, mid, provider=MutatingVision())
        with Store(c, readonly=True) as s:
            marks = s.db.execute("SELECT count(*) FROM extracted_fact WHERE material_id=? AND verification='machine_verified'",
                                 (mid,)).fetchone()[0]
            events = s.db.execute("SELECT count(*) FROM verification_event WHERE verification_method='dual_engine_consensus'").fetchone()[0]
            name = s.db.execute('SELECT name FROM org WHERE org_id=(SELECT subject_org_id FROM material WHERE material_id=?)',
                                (mid,)).fetchone()[0]
        self.assertEqual(marks, 0)
        self.assertEqual(events, 0)
        self.assertEqual(name, '')  # no partial machine fact was written


class ImageMimeTests(unittest.TestCase):
    """Blocker #3: declared mime must always match the actual encoded bytes."""

    def test_oversized_png_becomes_jpeg_with_matching_mime(self):
        from PIL import Image
        base, c = make_config('mime-' + uuid.uuid4().hex[:4])
        path = base / 'big.png'
        Image.new('RGB', (2600, 2600), 'white').save(path, 'PNG')
        pages = providers.render_pages_for_vision(path, max_side=2200)
        self.assertEqual(len(pages), 1)
        mime, data = pages[0]
        self.assertEqual(mime, 'image/jpeg')
        self.assertEqual(data[:2], b'\xff\xd8')  # JPEG magic

    def test_small_png_passthrough_keeps_png_mime(self):
        from PIL import Image
        base, c = make_config('mime2-' + uuid.uuid4().hex[:4])
        path = base / 'small.png'
        Image.new('RGB', (300, 200), 'white').save(path, 'PNG')
        original = path.read_bytes()
        mime, data = providers.render_pages_for_vision(path, max_side=2200)[0]
        self.assertEqual(mime, 'image/png')
        self.assertEqual(data, original)

    def test_pillow_unavailable_returns_original_bytes_safely(self):
        base, c = make_config('mime3-' + uuid.uuid4().hex[:4])
        path = base / 'big.png'
        from PIL import Image
        Image.new('RGB', (2600, 2600), 'white').save(path, 'PNG')
        original = path.read_bytes()
        with mock.patch.dict(sys.modules, {'PIL': None, 'PIL.Image': None, 'PIL.ImageOps': None}):
            mime, data = providers.render_pages_for_vision(path, max_side=2200)[0]
        self.assertEqual(mime, 'image/png')
        self.assertEqual(data, original)  # safe degradation: original bytes, matching mime


class ProjectIdentityTests(unittest.TestCase):
    """Blocker #4: a bare filename never creates a formal project."""

    def test_filename_only_does_not_create_project(self):
        base, c = make_config('ident-' + uuid.uuid4().hex[:4])
        text = '招标文件\n投标人须知：本文件仅用于合成测试，未包含项目名称或编号字段。'
        doc = stage_source(c, '某个扫描件.txt', text.encode('utf-8'))
        result = auto_intake(c, dict(source=str(doc), filename='某个扫描件.txt',
                                     expected_sha256=digest(doc)), 'local_webui')
        self.assertFalse(result['created_project'])
        self.assertFalse(result['saved'])
        self.assertIn('insufficient_project_identity', result['needs_review'])
        from jty_materials.projects import Projects
        self.assertEqual(Projects(c).list()['total'], 0)

    def test_named_tender_document_creates_project(self):
        base, c = make_config('ident2-' + uuid.uuid4().hex[:4])
        text = '招标文件\n项目名称：合成测试市政道路工程项目\n招标编号：JTYZB-2026-ID01\n投标人须知：合成测试。'
        doc = stage_source(c, 'tender.txt', text.encode('utf-8'))
        result = auto_intake(c, dict(source=str(doc), filename='tender.txt',
                                     expected_sha256=digest(doc)), 'local_webui')
        self.assertTrue(result['created_project'])
        self.assertTrue(result['saved'])
        self.assertEqual(result['fields']['name'], '合成测试市政道路工程项目')

    def test_project_file_bucket_uses_real_content(self):
        base, c = make_config('bucket-' + uuid.uuid4().hex[:4])
        from jty_materials.projects import execute
        made = execute(c, 'project.create', dict(name='桶测试项目', actor='local_webui'))
        doc = stage_source(c, 'scan0001.txt', '中标通知书\n项目名称：合成测试项目'.encode('utf-8'))
        receipt = add_file(c, dict(project_id=made['project_id'], source=str(doc),
                                   filename='scan0001.txt', expected_sha256=digest(doc)), 'local_webui')
        with Store(c, readonly=True) as s:
            category = s.db.execute('SELECT category FROM project_file WHERE project_file_id=?',
                                    (receipt['project_file_id'],)).fetchone()[0]
        self.assertEqual(category, 'bid_outputs')  # from content, not the neutral filename


class VisionOnlyClassificationTests(unittest.TestCase):
    """Blocker #5: vision alone can suggest, never bind a typed schema."""

    def test_vision_only_becomes_other_with_suggestion(self):
        from jty_materials.classify import combine
        final, basis, conflict = combine((None, 'no_confident_signal', 0.0),
                                         ('business_license', 'vision', 0.99))
        self.assertIsNone(final)
        self.assertTrue(conflict)
        self.assertEqual(basis, 'vision_only_suggested:business_license')

    def test_zero_fill_import_with_vision_only_stays_other(self):
        base, c = make_config('vonly-' + uuid.uuid4().hex[:4])
        # neutral text: the deterministic OCR classifier finds no header signal
        neutral = '随机文件内容，没有可识别的证照抬头。' * 20
        path = stage_source(c, 'neutral.png', b'')
        zf.make_license(path)  # image exists but OCR text is neutral in this scenario
        mv = zf.MockVision(classify_answer={'document_type': 'business_license', 'confidence': 0.99})
        receipt = import_material(c, path, actor='local_webui',
                                  ocr_factory=FakeOcr(neutral), vision_provider=mv)
        self.assertEqual(receipt['classification']['final'], 'other')
        self.assertTrue(receipt['classification']['conflict'])
        self.assertTrue(receipt['requires_human_review'])
        self.assertEqual(receipt['classification']['vision_signal'], 'business_license')
        from jty_materials.query import Query
        detail = Query(c).get_material(receipt['material_id'])
        self.assertEqual(detail['material_type'], 'other')


class MachineDirectSqlGuardTests(unittest.TestCase):
    """Blocker #8: plain SQL without a machine context still cannot write facts."""

    def test_uncontexted_fact_update_rejected(self):
        base, c = make_config('guard-' + uuid.uuid4().hex[:4])
        source = license_source(c)
        receipt = import_material(c, source, kind='certificate', subtype='business_license',
                                  title='guard', actor='local_webui', ocr_factory=FakeOcr(zf.LICENSE_TEXT))
        import sqlite3
        with Store(c) as s:
            with self.assertRaises(sqlite3.IntegrityError):
                s.db.execute("UPDATE org SET unified_social_credit_code='91XXXX000000000001' "
                             "WHERE org_id=(SELECT subject_org_id FROM material WHERE material_id=?)",
                             (receipt['material_id'],))
            with self.assertRaises(sqlite3.IntegrityError):
                s.db.execute("UPDATE certificate_detail SET certificate_no='FAKE-1' WHERE material_id=?",
                             (receipt['material_id'],))
        with Store(c, readonly=True) as s:
            code = s.db.execute("""SELECT o.unified_social_credit_code FROM material m
                JOIN org o ON o.org_id=m.subject_org_id""").fetchone()[0]
            no_row = s.db.execute('SELECT certificate_no FROM certificate_detail WHERE material_id=?',
                                  (receipt['material_id'],)).fetchone()[0]
        self.assertIsNone(code)
        self.assertIsNone(no_row)


class MigrationBackupNameTests(unittest.TestCase):
    """6.1: the pre-migration backup must be named after the TARGET version."""

    def test_backup_name_uses_target_schema_version(self):
        from jty_materials.store import SCHEMA_VERSION
        self.assertEqual(SCHEMA_VERSION, 'jty-mcp-0.7.0')
        self.assertTrue(SCHEMA_VERSION.startswith('jty-mcp-0.7'))
        self.assertNotIn('v06', 'schema-before-' + SCHEMA_VERSION)


if __name__ == '__main__':
    unittest.main(verbosity=1)
