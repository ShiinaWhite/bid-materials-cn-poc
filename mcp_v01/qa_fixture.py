"""Build the browser-QA fixture: one machine-verified license, one disputed
license, one classification-exception 'other', and a project. Run the WebUI
against it with: python -B webui/app.py --config <base>/config.json --port 8899
"""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import zero_fill_support as zf
from jty_materials.common import atomic_write, digest, now
from jty_materials.store import Store
from jty_materials.workflow import import_material
from jty_materials.consensus import dual_engine_process
from jty_materials.projects import execute as project_execute

base = Path(sys.argv[1]).resolve()
base.mkdir(parents=True, exist_ok=False)
raw = dict(repository_root='repo', db_path='repo/db/materials.db', state_dir='state', logs_dir='logs',
           backup_dir='backups', index_dir='index/dowse-index',
           umi_exe='rt/umi/Umi-OCR.exe', dowse_exe='rt/dowse/dowse.exe',
           umi_url='http://127.0.0.1:19999', fixture_only=True)
atomic_write(base / 'config.json', json.dumps(raw))
c = Config_ = None
from jty_materials.common import Config
c = Config(base / 'config.json')
c.materials.mkdir(parents=True)
c.logs_dir.mkdir(parents=True)
zf.init_store(c)

sources = base / 'sources'
sources.mkdir()


def stage(name):
    path = sources / (uuid.uuid4().hex[:8] + '-' + name)
    return path


def seed_candidates(mid, original_file_id, rows):
    with Store(c) as s:
        with s.db:
            run = s.db.execute(
                """INSERT INTO extraction_run(material_id,source_file_id,extractor,extractor_version,params_json,run_at,status)
                   VALUES(?,?,?,?,?,?,?) RETURNING run_id""",
                (mid, original_file_id, 'umi-ocr+typed-candidate-rules', '2.1.5/qa',
                 json.dumps({'text_status': 'complete'}), now(), 'succeeded')).fetchone()[0]
            for field, value in rows:
                span = json.dumps({'extractor': 'umi-ocr+typed-candidate-rules', 'human_verified': False,
                                   'page': 1, 'basis': 'qa fixture', 'original_file_id': original_file_id,
                                   'original_sha256': s.db.execute('SELECT sha256 FROM material_file WHERE file_id=?',
                                                                   (original_file_id,)).fetchone()[0]})
                vt = 'date' if field == 'established_on' else 'text'
                s.db.execute("""INSERT INTO extracted_fact(run_id,material_id,field_name,value_type,raw_value,normalized_value,page_no,evidence_span)
                               VALUES(?,?,?,?,?,?,?,?)""", (run, mid, field, vt, value, value, 1, span))


def vision(fields):
    return zf.MockVision(extract_answers=[{'fields': {k: {'value': v, 'page': 1} for k, v in fields.items()}}])


ORG = dict(enterprise_name=zf.NAME, unified_social_credit_code=zf.CREDIT, established_on=zf.ESTABLISHED)

# A: normal machine-verified license
p = stage('ok-license.png'); zf.make_license(p)
r = import_material(c, p, kind='certificate', subtype='business_license', title='QA 正常执照',
                    actor='local_webui', ocr_factory=lambda _: None or zf.FakeOcr(zf.LICENSE_TEXT))
seed_candidates(r['material_id'], r['files']['original'], list(ORG.items()))
dual_engine_process(c, r['material_id'], provider=vision(ORG))

# B: disputed credit code
bad = zf.CREDIT[:-1] + ('0' if zf.CREDIT[-1] != '0' else '1')
p = stage('conflict-license.png'); zf.make_license(p)
r = import_material(c, p, kind='certificate', subtype='business_license', title='QA 冲突执照',
                    actor='local_webui', ocr_factory=zf.FakeOcr(zf.LICENSE_TEXT))
seed_candidates(r['material_id'], r['files']['original'], list(ORG.items()))
dual_engine_process(c, r['material_id'], provider=vision({**ORG, 'unified_social_credit_code': bad}))

# C: classification exception → other
p = stage('unknown-doc.png'); zf.make_license(p)
mv = zf.MockVision(classify_answer={'document_type': 'patent', 'confidence': 0.93})
r = import_material(c, p, actor='local_webui',
                    ocr_factory=zf.FakeOcr(zf.LICENSE_TEXT), vision_provider=mv)

project_execute(c, 'project.create', dict(name='合成测试市政道路工程项目', tender_no='JTYZB-QA-001',
                                          tenderer='合成测试城市建设开发有限公司', actor='local_webui'))
print(json.dumps({'base': str(base), 'ok': r['classification']['conflict']}))
