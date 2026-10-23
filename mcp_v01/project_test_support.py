"""Isolated project fixtures, always under runs/project-center."""
import json
import os
from pathlib import Path
import unittest
from unittest import mock
import uuid

from jty_materials.common import Config, atomic_write, digest
from jty_materials.store import Store
from jty_materials.projects import execute, Projects
from jty_materials.workflow import import_material
from test_v01 import FakeOcr
from test_p05 import MemoryDowse
from jty_materials import manager

BASE = Path(__file__).resolve().parents[1] / 'runs/project-center'


class ProjectFixture(unittest.TestCase):
    def setUp(self):
        self.base = BASE / 'tests' / uuid.uuid4().hex[:8]
        self.base.mkdir(parents=True)
        atomic_write(self.base/'config.json', json.dumps(dict(
            repository_root='repository', db_path='repository/db/materials.db',
            state_dir='state', logs_dir='logs', backup_dir='backups', index_dir='index/dowse-index',
            dowse_exe=os.environ.get('P05_DOWSE_EXE', 'unused/dowse.exe'), umi_exe='unused/Umi-OCR.exe',
            umi_url='http://127.0.0.1:1224', fixture_only=True)))
        self.c = Config(self.base/'config.json')
        self.c.materials.mkdir(parents=True)
        Store.initialize(self.c)
        with mock.patch.object(manager, 'Dowse', MemoryDowse):
            self.supervisor = manager.Supervisor(self.c)

    def call(self, action, **payload):
        return self.supervisor.job(action, dict(actor='fixture-reviewer', **payload))

    def create(self, name='某医院配电改造项目'):
        return self.call('project.create', name=name)['project_id']

    def file_payload(self, pid, filename='招标资料.pdf', data=b'%PDF-project-fixture', category='tender_docs'):
        path = self.base/('source-' + uuid.uuid4().hex)
        path.write_bytes(data)
        return dict(project_id=pid, filename=filename, category=category, source=str(path), expected_sha256=digest(path))

    def material(self):
        source = self.base/'material.pdf'; source.write_bytes(b'%PDF-global-material')
        return import_material(self.c, source, kind='certificate', subtype='safety_prod_license',
                               title='全局安全生产许可证', actor='fixture',
                               ocr_factory=lambda c: FakeOcr(c, '全局素材可检索 GLOBAL_MATERIAL'))['material_id']

    def counts(self):
        with Store(self.c, readonly=True) as s:
            return {t: s.db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in
                    ('material','material_file','project','project_file','project_material','search_projection','extraction_run')}

    def assert_clean(self):
        self.assertFalse(self.c.maintenance_file.exists())
        self.assertEqual(list((self.c.state_dir/'project-file-journal').glob('*.json')), [])
        self.assertEqual(list((self.c.repository_root/'projects/.staging').glob('*')), [])
        with Store(self.c, readonly=True) as s:
            self.assertEqual(s.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(s.db.execute('PRAGMA foreign_key_check').fetchall(), [])
