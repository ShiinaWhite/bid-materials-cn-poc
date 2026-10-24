"""Mandatory real Umi 2.1.5 + named pipe manager + real dowse upload flow."""
import json, time, unittest
from pathlib import Path
from jty_materials.common import atomic_write, digest, safe_path
from jty_materials.query import Query
from jty_materials.manager import request
from jty_materials.store import Store
from webui.uploads import UploadManager
from upload_repair_support import EXPECTED, NAME, CREDIT, make_license, real_fixture


class UploadRepairIntegrationTests(unittest.TestCase):
    def test_real_umi_web_upload_pending_and_reextract_without_new_original(self):
        base,c=real_fixture()
        path=make_license(base/'营业执照-新.png');sha=digest(path)
        uploader=UploadManager(c)
        try:
            job=uploader.submit('import',path,path.name,path.stat().st_size,dict(actor='isolated real Umi fixture',subtype='business_license',title=''))
            self.wait_job(job)
            mid=job.result['material_id'];m=Query(c).get_material(mid)
            raw_file=next(f for f in m['files'] if f['role']=='ocr_raw_text' and f['is_current'])
            raw=safe_path(c.repository_root,raw_file['relative_path']).read_text(encoding='utf-8')
            atomic_write(base/'checked-raw-ocr.txt',raw)
            for value in [NAME,CREDIT,'2001年02月03日']:self.assertIn(value,raw)
            self.assertEqual({f:cs[0]['value'] for f,cs in m['candidate_fields'].items()},EXPECTED)
            self.assertEqual(m['verified_facts'],{})
            with Store(c,readonly=True) as s:
                before=self.snapshot(s.db)
                self.assertEqual(before['events'],[])
            original=next(f for f in m['files'] if f['role']=='original')
            self.assertEqual(original['sha256'],sha)
            job=uploader.submit('reextract',None,'',0,dict(actor='isolated real retry',material_id=mid))
            self.wait_job(job)
            m=Query(c).get_material(mid)
            self.assertEqual({f:cs[0]['value'] for f,cs in m['candidate_fields'].items()},EXPECTED)
            self.assertEqual(m['verified_facts'],{})
            with Store(c,readonly=True) as s:
                self.assertEqual(self.snapshot(s.db),before)
                self.assertEqual(s.db.execute("select count(*) from extracted_fact where review_status='pending'").fetchone()[0],3)
                self.assertEqual(s.db.execute("select count(*) from extracted_fact where review_status='superseded'").fetchone()[0],3)
                options=json.loads(s.db.execute('select params_json from extraction_run order by run_id desc limit 1').fetchone()[0])
                self.assertFalse(options['ocr_engine_rerun']);self.assertEqual(len(options['reused_files']),3)
            self.assertEqual(digest(safe_path(c.repository_root,original['relative_path'])),sha)
            atomic_write(base/'real-umi-evidence.json',json.dumps(dict(OCR_TEXT='PASS',STRUCTURED_EXTRACTION='PASS',umi_version='2.1.5',
                candidates=EXPECTED,material_id=mid,original_sha256=sha,original_and_version_unchanged=True,verified_events=0,config=str(c.path)),ensure_ascii=False,indent=2))
        finally:
            request(c,'shutdown',start=False)

    def wait_job(self,job):
        until=time.monotonic()+240
        while job.state not in ('done','error') and time.monotonic()<until:time.sleep(.1)
        self.assertEqual(job.state,'done',job.error)

    def snapshot(self,db):
        return dict(material_count=db.execute('select count(*) from material').fetchone()[0],
            versions=[tuple(r) for r in db.execute("select file_id,version_id,sha256 from material_file where role='original'")],
            events=[tuple(r) for r in db.execute('select * from verification_event')])
