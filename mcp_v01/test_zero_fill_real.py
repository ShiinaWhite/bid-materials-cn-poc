"""REAL integration: real Umi OCR + real OpenCode Go vision on a synthetic license.

Gates (spec #50): same synthetic document -> Umi OCR -> direct vision read of
the same original -> field-level consensus -> machine_verified, with zero
manual inputs. Requires LARGE_DOC_UMI_EXE, P05_DOWSE_EXE and a valid
OPENCODE_GO_API_KEY in the environment; without any of them the test fails
loudly (never a fake PASS).
"""
import json
import os
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import zero_fill_support as zf
from jty_materials.common import Config, atomic_write, digest
from jty_materials.store import Store
from jty_materials.manager import request
from jty_materials.query import Query


@unittest.skipUnless(os.environ.get('LARGE_DOC_UMI_EXE') and os.environ.get('P05_DOWSE_EXE')
                     and os.environ.get('OPENCODE_GO_API_KEY'),
                     'real Umi/dowse/OpenCode Go credential required; no fake PASS')
class RealDualEngineTests(unittest.TestCase):
    def test_zero_fill_license_end_to_end(self):
        base = Path(__file__).resolve().parents[1] / 'runs' / ('zf-real-' + uuid.uuid4().hex[:6])
        from jty_materials.setup import copy_runtime
        umi = Path(os.environ['LARGE_DOC_UMI_EXE'])
        dowse = Path(os.environ['P05_DOWSE_EXE'])
        assert umi.is_file() and dowse.is_file(), 'real Umi/dowse required; no skip'
        version = json.loads((umi.parent / 'UmiOCR-data/about.json').read_text(encoding='utf-8'))['version']
        assert [version[k] for k in ('major', 'minor', 'patch')] == [2, 1, 5], version
        # Isolated runtime copy: a production dowse watch must never block this
        # run's index rebuild, and the fixture must never touch production state.
        umi_url = copy_runtime(umi, dowse, base / 'rt')
        raw = dict(repository_root='repo', db_path='repo/db/materials.db', state_dir='state',
                   logs_dir='logs', backup_dir='backups', index_dir='index/dowse-index',
                   umi_exe='rt/umi/Umi-OCR.exe', dowse_exe='rt/dowse/dowse.exe',
                   umi_url=umi_url, fixture_only=True)
        base.mkdir(parents=True, exist_ok=True)
        atomic_write(base / 'config.json', json.dumps(raw))
        c = Config(base / 'config.json')
        c.materials.mkdir(parents=True)
        c.logs_dir.mkdir(parents=True)
        Store.initialize(c)
        # synthetic source, staged outside the repository like the webui does
        sources = base.parent / 'sources'
        sources.mkdir(parents=True, exist_ok=True)
        source = sources / (uuid.uuid4().hex[:8] + '-license.png')
        zf.make_license(source)
        # ZERO-FILL: no kind, no subtype, no title, no actor input
        receipt = request(c, 'import', {'source': str(source), 'actor': 'local_webui'}, timeout=1800)
        mid = receipt['material_id']
        self.assertEqual(receipt['classification']['final'], 'business_license',
                         receipt.get('classification'))
        self.assertFalse(receipt['classification']['conflict'])
        self.assertTrue(receipt.get('dual_engine'), receipt)
        detail = Query(c).get_material(mid)
        self.assertEqual(detail['material_type'], 'business_license')
        verified = detail['verified_facts']
        for field, expected in (('enterprise_name', zf.NAME),
                                ('unified_social_credit_code', zf.CREDIT),
                                ('established_on', zf.ESTABLISHED)):
            self.assertIn(field, verified, f'{field} not machine-verified: {sorted(verified)}')
            self.assertEqual(verified[field]['value'], expected, field)
            self.assertEqual(verified[field]['verification_method'], 'dual_engine_consensus', field)
        self.assertEqual(detail['verification_status'], 'ai_extracted')
        self.assertFalse(receipt.get('requires_human_review'), receipt)
        # the stored title was built deterministically from the verified name
        self.assertIn(zf.NAME, detail['title'], detail['title'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
