"""Schema guards and genuine 0.4 -> 0.5 transactional migration fixtures."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from unittest import mock

from project_test_support import ProjectFixture
from jty_materials.common import Config, atomic_write, digest, now
from jty_materials.store import Store, SCHEMA_VERSION, harden, release_schema
from jty_materials import schema_v05
from jty_materials.schema_v04 import upgrade as v04
from jty_materials.projects import Projects
from jty_materials.doctor import check_files


class ProjectSchemaTests(ProjectFixture):
    def test_initialization_only_three_project_tables_and_version(self):
        with Store(self.c, readonly=True) as s:
            self.assertEqual(SCHEMA_VERSION, 'jty-mcp-0.8.0')
            self.assertEqual({r[0] for r in s.db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name LIKE 'project%' ")},
                             {'project','project_file','project_material','project_document','project_document_run','project_document_unit',
                             'project_document_image','project_material_candidate'})  # v0.8
        self.assertEqual(Projects(self.c).list()['total'], 0)

    def test_identity_file_and_ledger_guards_even_external_sql(self):
        pid = self.create(); mid = self.material()
        self.call('project.material.link', project_id=pid, material_id=mid)
        self.call('project.file.add', **self.file_payload(pid))
        sqls = ["UPDATE project SET project_id=2", "UPDATE project SET project_code='PRJ-999999'",
                "DELETE FROM project", "INSERT OR REPLACE INTO project SELECT * FROM project",
                "UPDATE project SET name='direct'", "UPDATE project_file SET filename='overwrite.pdf'",
                "UPDATE project_file SET sha256='"+'a'*64+"'", "DELETE FROM project_file",
                "INSERT OR REPLACE INTO project_file SELECT project_file_id,project_id,category,relative_path,filename,extension,sha256,size_bytes,imported_at,imported_by,status FROM project_file",
                "UPDATE project_material SET status='removed'", "DELETE FROM project_material",
                "INSERT OR REPLACE INTO project_material SELECT * FROM project_material",
                "UPDATE audit_log SET actor='overwritten'", "DELETE FROM audit_log",
                "INSERT OR REPLACE INTO audit_log SELECT * FROM audit_log"]
        with closing(sqlite3.connect(self.c.db_path)) as db:
            for sql in sqls:
                with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                    db.execute(sql)
                db.rollback()
        with Store(self.c) as s:
            s.project_write = dict(actor='fixture', action='project.update')
            for sql in sqls[:4]:
                with self.subTest(controlled=sql), self.assertRaises(sqlite3.DatabaseError): s.db.execute(sql)
                s.db.rollback()

    def legacy(self):
        raw = dict(self.c.raw, repository_root='legacy', db_path='legacy/db/materials.db', backup_dir='legacy-backups')
        cp = self.base/'legacy.json'; atomic_write(cp, json.dumps(raw)); c = Config(cp)
        c.db_path.parent.mkdir(parents=True)
        original = c.repository_root/'materials/legacy.pdf'; original.parent.mkdir(); original.write_bytes(b'%PDF-legacy-v04')
        with closing(sqlite3.connect(c.db_path)) as db:
            db.executescript(Path(__file__).with_name('jty_materials').joinpath('schema_v1.sql').read_text(encoding='utf-8'))
            harden(db); release_schema(db); v04(db)
            values = dict(actor='reviewer',field='valid_until',evidence=1,candidate=None,reason='legacy review',time=now(),material=1,sha256=digest(original),action='review')
            for k,v in values.items(): db.create_function('review_'+k,0,lambda v=v:v)
            db.execute("INSERT INTO material(title,material_type_id,created_at,updated_at) VALUES('0.4 material',(SELECT type_id FROM material_type WHERE code='safety_prod_license'),?,?)", (now(), now()))
            db.execute("INSERT INTO certificate_detail(material_id,certificate_kind) VALUES(1,'safety_production')")
            db.execute("INSERT INTO material_file(material_id,role,relative_path,filename,extension,sha256,size_bytes,imported_at) VALUES(1,'original','materials/legacy.pdf','legacy.pdf','pdf',?,?,?)", (digest(original),original.stat().st_size,now()))
            db.execute("UPDATE certificate_detail SET valid_until='2030-01-01' WHERE material_id=1")
            db.commit()
        return c

    def snapshot(self, c):
        with closing(sqlite3.connect(c.db_path)) as db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name<>'schema_meta' AND name NOT LIKE 'sqlite_%'")]
            return {t: db.execute('SELECT * FROM '+t).fetchall() for t in tables}

    def test_real_04_migration_preserves_all_rows_guards_backup_and_doctor(self):
        c = self.legacy(); before = self.snapshot(c)
        with self.assertRaisesRegex(ValueError, 'explicit migration'): Store(c)
        result = Store.migrate(c)
        self.assertTrue(result['changed']); self.assertFalse(Store.migrate(c)['changed'])
        after = self.snapshot(c)
        for t in before:
            self.assertEqual(before[t], after[t][:-1] if t=='audit_log' else after[t])
        backup = Path(result['pre_migration_database'])
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("SELECT meta_value FROM schema_meta WHERE meta_key='schema_version'").fetchone()[0], 'jty-mcp-0.4.0')
        with Store(c, readonly=True) as s:
            self.assertEqual(check_files(c,s.db,'material_file')['errors'], [])
            self.assertEqual(check_files(c,s.db,'project_file'), dict(checked=0,errors=[]))
            self.assertEqual(s.db.execute('PRAGMA foreign_key_check').fetchall(), [])
        with Store(c) as s:
            with self.assertRaises(sqlite3.DatabaseError): s.db.execute("UPDATE certificate_detail SET valid_until='2099-01-01'")
        from jty_materials.projects import execute
        execute(c,'project.create',dict(name='迁移后项目',actor='fixture'))

    def test_migration_failure_rolls_back_schema_and_audit_then_retry(self):
        c = self.legacy(); before = self.snapshot(c)
        with mock.patch.object(schema_v05, 'now', side_effect=RuntimeError('injected migration failure')):
            with self.assertRaisesRegex(RuntimeError, 'injected'): Store.migrate(c)
        self.assertEqual(before,self.snapshot(c))
        with closing(sqlite3.connect(c.db_path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_schema WHERE name='project'").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT meta_value FROM schema_meta WHERE meta_key='schema_version'").fetchone()[0],'jty-mcp-0.4.0')
        self.assertEqual(len(list(c.backup_dir.glob('*.db'))),1)
        self.assertTrue(Store.migrate(c)['changed'])

    def test_project_path_guard_blocks_sql_traversal_and_foreign_binding(self):
        pid = self.create()
        added = self.call('project.file.add', **self.file_payload(pid))
        row = Projects(self.c).resolve_file(pid, added['project_file_id'])
        with Store(self.c) as s:
            s.project_write = dict(actor='fixture', action='project.file.add')
            for path in ['../outside.pdf','materials/wrong.pdf','projects/PRJ-000001/files/../../outside.pdf',
                         'projects/PRJ-000001/files/wrong.pdf', 'C:/outside.pdf']:
                with self.subTest(path=path), self.assertRaises(sqlite3.DatabaseError):
                    s.db.execute('''INSERT INTO project_file(project_file_id,project_id,category,relative_path,filename,
                        extension,sha256,size_bytes,imported_at,imported_by) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                        ('f'*32, pid, 'other', path, 'display.pdf', 'pdf', 'e'*64, 10, now(), 'fixture'))
                s.db.rollback()
            with self.assertRaises(sqlite3.DatabaseError):
                s.db.execute("INSERT INTO project_material(project_id,material_id,status,added_at,added_by) VALUES(?,999,'active',?,'fixture')",(pid,now()))
            s.db.rollback()
