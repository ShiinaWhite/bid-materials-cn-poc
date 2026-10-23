"""Business boundaries, immutable private files, references, doctor and backup."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from unittest import mock

from project_test_support import ProjectFixture
from jty_materials.projects import Projects, MAX_BYTES
from jty_materials.common import digest, safe_path
from jty_materials.store import Store
from jty_materials.doctor import check_files
from jty_materials.backup import create_backup, restore_backup
from jty_materials.query import Query
from webui import projects as ui, service, pages


class ProjectServiceTests(ProjectFixture):
    def test_create_update_search_archive_restore_retains_everything(self):
        counts = self.counts(); pid = self.create(); other = self.create('某园区10kV工程')
        self.assertEqual(self.counts()['material'], counts['material'])
        self.call('project.update', project_id=pid, name='新名称', tender_no='T-001', tenderer='测试招标人', lot_name='一标段')
        p = Projects(self.c).detail(pid)
        self.assertEqual(p['project_code'], 'PRJ-000001')
        self.assertEqual(p['lot_name'], '一标段')
        self.assertEqual(Projects(self.c).list('T-001')['total'],1)
        self.assertEqual(Projects(self.c).list('10kV')['items'][0]['project_id'],other)
        mid = self.material(); self.call('project.material.link', project_id=pid, material_id=mid)
        added = self.call('project.file.add', **self.file_payload(pid))
        self.call('project.archive', project_id=pid)
        self.assertEqual(Projects(self.c).list()['total'], 1)
        self.assertEqual(Projects(self.c).list(status='archived')['total'],1)
        self.assertIsNotNone(Projects(self.c).resolve_file(pid,added['project_file_id']))
        self.assertEqual(Projects(self.c).reverse(mid)[0]['status'],'archived')
        for action, data in [('project.update',dict(name='blocked')),('project.file.add',self.file_payload(pid)),
                             ('project.material.link',dict(material_id=mid)),('project.material.unlink',dict(material_id=mid))]:
            with self.subTest(action=action), self.assertRaisesRegex(ValueError,'归档'):
                self.call(action, **dict(data,project_id=pid))
        self.call('project.restore', project_id=pid)
        self.call('project.update', project_id=pid, name='恢复后项目')
        self.assertEqual(len(Projects(self.c).detail(pid)['files']),1)
        self.assert_clean()

    def test_required_fields_missing_ids_and_description_are_validated(self):
        for name in ['',None,'x'*501,'bad\nname']:
            with self.subTest(name=name), self.assertRaises(ValueError): self.create(name)
        with self.assertRaises(ValueError): self.call('project.update', project_id=999, name='不存在')
        pid=self.create()
        for mid in [999, -1]:
            with self.assertRaises(ValueError): self.call('project.material.link',project_id=pid,material_id=mid)
        self.assertEqual(self.counts()['project_material'],0)

    def test_reference_no_copy_unlink_relink_reverse_and_audit(self):
        pid=self.create(); mid=self.material(); before=self.counts()
        with Store(self.c,readonly=True) as s:
            rows={t:[tuple(r) for r in s.db.execute('SELECT * FROM '+t)] for t in ('material','material_file')}
            sha=s.db.execute("SELECT sha256 FROM material_file WHERE role='original'").fetchone()[0]
        files={p.relative_to(self.c.repository_root).as_posix():digest(p) for p in self.c.repository_root.rglob('*') if p.is_file() and 'db' not in p.parts}
        result=self.call('project.material.link', project_id=pid,material_id=mid,note='本次使用')
        self.assertTrue(self.call('project.material.link',project_id=pid,material_id=mid)['unchanged'])
        after=self.counts();self.assertEqual(after,dict(before,project_material=1))
        self.assertEqual(files,{p.relative_to(self.c.repository_root).as_posix():digest(p) for p in self.c.repository_root.rglob('*') if p.is_file() and 'db' not in p.parts})
        self.assertEqual(sum(digest(p)==sha for p in self.c.repository_root.rglob('*') if p.is_file() and 'db' not in p.parts),1)
        with Store(self.c,readonly=True) as s:
            for t in rows:self.assertEqual(rows[t],[tuple(r) for r in s.db.execute('SELECT * FROM '+t)])
        self.assertEqual(Projects(self.c).reverse(mid)[0]['project_id'],pid)
        self.assertIn('用于 1 个项目',ui.reverse_section(self.c,mid))
        self.call('project.material.unlink',project_id=pid,material_id=mid)
        self.assertEqual(Projects(self.c).reverse(mid),[]);self.assertEqual(ui.reverse_section(self.c,mid),'')
        self.assertEqual(self.counts(),after)
        relink=self.call('project.material.link',project_id=pid,material_id=mid)
        self.assertEqual(result['reference_id'],relink['reference_id'])
        with Store(self.c,readonly=True) as s:
            events=[r[0] for r in s.db.execute("SELECT action FROM audit_log WHERE entity_type='project_material' ORDER BY log_id")]
        self.assertEqual(events,['project.material.link','project.material.unlink','project.material.link'])

    def test_upload_immutable_sha_duplicates_display_name_and_doctor_damage(self):
        pid=self.create(); payload=self.file_payload(pid,filename='同名文件.PDF')
        result=self.call('project.file.add',**payload)
        row=Projects(self.c).resolve_file(pid,result['project_file_id'])
        self.assertEqual(row['filename'],'同名文件.PDF')
        self.assertEqual(row['sha256'],digest(payload['source']))
        self.assertEqual(row['relative_path'],f'projects/PRJ-000001/files/{result["project_file_id"]}.pdf')
        self.assertTrue(self.call('project.file.add',**payload)['duplicate'])
        self.call('project.file.add',**self.file_payload(pid,filename='同名文件.pdf',data=b'%PDF-another'))
        other=self.create('某园区10kV工程')
        self.assertIsNone(Projects(self.c).resolve_file(other,result['project_file_id']))
        self.assertEqual(self.counts()['project_file'],2)
        with Store(self.c,readonly=True) as s:self.assertEqual(check_files(self.c,s.db,'project_file'),dict(checked=2,errors=[]))
        row['path'].write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError,'校验失败'):Projects(self.c).resolve_file(pid,result['project_file_id'])
        with Store(self.c,readonly=True) as s:self.assertEqual(check_files(self.c,s.db,'project_file')['errors'],[row['relative_path']])

    def test_project_file_formats_fake_extensions_paths_size_and_sha(self):
        pid=self.create()
        signatures={'.pdf':b'%PDF-', '.png':b'\x89PNG\r\n\x1a\n', '.jpg':b'\xff\xd8\xff', '.jpeg':b'\xff\xd8\xff',
                    '.doc':b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1', '.xls':b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1', '.ppt':b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1',
                    '.docx':b'PK\x03\x04', '.xlsx':b'PK\x03\x04', '.pptx':b'PK\x03\x04', '.zip':b'PK\x03\x04', '.txt':b'text', '.md':b'# text'}
        for ext,data in signatures.items():
            with self.subTest(ext=ext):self.call('project.file.add',**self.file_payload(pid,'file'+ext,data+ext.encode()))
        for name in ['../evil.pdf','C:/evil.pdf','dir\\evil.pdf','evil.exe','fake.pdf.exe','evil.pdf:ads','evil.pdf.']:
            with self.subTest(name=name), self.assertRaises(ValueError):self.call('project.file.add',**self.file_payload(pid,name))
        for name,data in [('fake.pdf',b'MZ-executable'),('fake.docx',b'not-office'),('fake.png',b'not-image'),('fake.txt',b'MZ-executable')]:
            with self.subTest(fake=name),self.assertRaises(ValueError):self.call('project.file.add',**self.file_payload(pid,name,data))
        payload=self.file_payload(pid,data=b'')
        with self.assertRaises(ValueError):self.call('project.file.add',**payload)
        path=Path(payload['source'])
        with path.open('wb') as f:f.truncate(MAX_BYTES+1)
        payload['expected_sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'200MB'):self.call('project.file.add',**payload)
        path.unlink()
        payload=self.file_payload(pid);payload['expected_sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'SHA256'):self.call('project.file.add',**payload)
        payload=self.file_payload(999)
        with self.assertRaisesRegex(ValueError,'不存在'):self.call('project.file.add',**payload)
        self.assert_clean()

    def test_project_only_office_never_enters_global_search_or_extraction(self):
        from docx import Document
        pid=self.create();self.material();before=self.counts()
        path=self.base/'project-specific-file.docx';doc=Document();doc.add_paragraph('PROJECT_ONLY_SENTINEL_847291');doc.save(path)
        result=self.call('project.file.add',project_id=pid,filename=path.name,category='process',source=str(path),expected_sha256=digest(path))
        self.assertEqual(self.counts(),dict(before,project_file=1))
        self.assertEqual(Projects(self.c).resolve_file(pid,result['project_file_id'])['path'].read_bytes(),path.read_bytes())
        with mock.patch.object(Query,'text_hits',return_value=[]):
            self.assertEqual(Query(self.c).search_materials(query='PROJECT_ONLY_SENTINEL_847291')['total'],0)
            self.assertEqual(service.list_materials(self.c,q='PROJECT_ONLY_SENTINEL_847291')['total'],0)
            with mock.patch.object(service,'system_ok',return_value=True):
                self.assertNotIn('project-specific-file.docx',pages.browse(self.c,dict(q='PROJECT_ONLY_SENTINEL_847291')))
        self.assertFalse(list(self.c.materials.rglob('*.docx')))
        self.assert_clean()

    def test_backup_restore_includes_private_evidence_and_material_reference(self):
        pid=self.create();mid=self.material();self.call('project.material.link',project_id=pid,material_id=mid)
        added=self.call('project.file.add',**self.file_payload(pid))
        result=create_backup(self.c)
        manifest=json.loads((Path(result['backup'])/'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(sum(r['relative_path'].startswith('projects/') for r in manifest['files']),1)
        def runtime(umi,dowse,target):
            target.mkdir(parents=True);return 'http://127.0.0.1:1224'
        with mock.patch('jty_materials.setup.copy_runtime',runtime):
            restored=restore_backup(result['backup'],self.base/'restored')
        c=__import__('jty_materials.common',fromlist=['Config']).Config(restored['config'])
        p=Projects(c).detail(pid)
        self.assertEqual(p['references'][0]['material_id'],mid)
        self.assertEqual(p['files'][0]['project_file_id'],added['project_file_id'])
        self.assertEqual(Projects(c).resolve_file(pid,added['project_file_id'])['sha256'],added['sha256'])
        with Store(c,readonly=True) as s:
            self.assertEqual(check_files(c,s.db,'project_file')['errors'],[])
            self.assertEqual(check_files(c,s.db,'material_file')['errors'],[])
