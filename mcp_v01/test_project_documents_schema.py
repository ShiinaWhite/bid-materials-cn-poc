from contextlib import closing
import json
from pathlib import Path
import sqlite3
from unittest import mock

from document_test_support import DocumentFixture, make_pdf
from jty_materials.common import Config, atomic_write, digest, now
from jty_materials.store import Store, harden, release_schema, SCHEMA_VERSION
from jty_materials import schema_v05, schema_v06


class DocumentSchemaTests(DocumentFixture):
    def legacy(self):
        config=self.base/'legacy.json'
        atomic_write(config,json.dumps(dict(self.c.raw,repository_root='legacy',db_path='legacy/db/materials.db')))
        c=Config(config);c.db_path.parent.mkdir(parents=True)
        source=c.repository_root/'projects/PRJ-000001/files'/('f'*32+'.pdf')
        source.parent.mkdir(parents=True);source.write_bytes(b'%PDF-original-v05')
        with closing(sqlite3.connect(c.db_path)) as db:
            db.executescript(Path(__file__).with_name('jty_materials').joinpath('schema_v1.sql').read_text(encoding='utf-8'))
            harden(db);release_schema(db);schema_v05.upgrade(db)
            db.create_function('project_actor',0,lambda:'fixture')
            db.create_function('project_action',0,lambda:'project.create')
            db.execute("INSERT INTO project(project_id,project_code,name,created_at,updated_at) VALUES(1,'PRJ-000001','legacy',?,?)",(now(),now()))
            db.create_function('project_action',0,lambda:'project.file.add')
            db.execute('''INSERT INTO project_file(project_file_id,project_id,category,relative_path,filename,extension,sha256,size_bytes,imported_at,imported_by)
                VALUES(?,1,'other',?,'original.pdf','pdf',?,?,?,'fixture')''',('f'*32,source.relative_to(c.repository_root).as_posix(),digest(source),source.stat().st_size,now()))
            db.commit()
        return c

    def test_initialize_and_pending_document_on_upload(self):
        self.add(make_pdf(self.base/'pending.pdf'))
        with Store(self.c,readonly=True) as s:
            self.assertEqual(SCHEMA_VERSION,'jty-mcp-0.8.0')
            # v0.8 zero-fill: the upload itself queues extraction, so the
            # document leaves 'pending' immediately and one run exists.
            self.assertEqual(s.db.execute('SELECT status FROM project_document').fetchone()[0],'queued')
            self.assertEqual(s.db.execute('SELECT count(*) FROM project_document_run').fetchone()[0],1)
            self.assertEqual(s.db.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_initialization_upgrade_failure_rolls_back_all_schema(self):
        path=self.base/'init-failure.json'
        atomic_write(path,json.dumps(dict(self.c.raw,repository_root='init-failure',db_path='init-failure/db/materials.db')))
        c=Config(path)
        with mock.patch('jty_materials.store.upgrade',side_effect=RuntimeError('injected initialize failure')):
            with self.assertRaises(RuntimeError):Store.initialize(c)
        with closing(sqlite3.connect(c.db_path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sqlite_schema').fetchone()[0],0)

    def test_real_05_migration_backups_pending_metadata_preserves_source_and_idempotent(self):
        c=self.legacy()
        with self.assertRaisesRegex(ValueError,'explicit migration'):Store(c)
        with closing(sqlite3.connect(c.db_path)) as db:before=db.execute('SELECT * FROM project_file').fetchall()
        result=Store.migrate(c)
        self.assertTrue(result['changed']);self.assertFalse(Store.migrate(c)['changed'])
        with Store(c,readonly=True) as s:
            self.assertEqual(before,[tuple(r) for r in s.db.execute('SELECT * FROM project_file')])
            self.assertEqual(s.db.execute('SELECT status FROM project_document').fetchone()[0],'pending')
            self.assertEqual(s.db.execute('PRAGMA foreign_key_check').fetchall(),[])
        with closing(sqlite3.connect(result['pre_migration_database'])) as db:
            self.assertEqual(db.execute("SELECT meta_value FROM schema_meta WHERE meta_key='schema_version'").fetchone()[0],'jty-mcp-0.5.0')

    def test_05_migration_failure_rolls_back_and_retry(self):
        c=self.legacy()
        with mock.patch.object(schema_v06,'now',side_effect=OSError('injected migration disk failure')):
            with self.assertRaises(OSError):Store.migrate(c)
        with closing(sqlite3.connect(c.db_path)) as db:
            self.assertEqual(db.execute("SELECT meta_value FROM schema_meta WHERE meta_key='schema_version'").fetchone()[0],'jty-mcp-0.5.0')
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_schema WHERE name='project_document'").fetchone()[0],0)
        self.assertTrue(Store.migrate(c)['changed'])

    def test_direct_document_writes_blocked_and_source_binding_guard(self):
        fid=self.add(make_pdf(self.base/'guards.pdf'))
        with closing(sqlite3.connect(self.c.db_path)) as db:
            for sql in ("DELETE FROM project_document","UPDATE project_document SET status='complete'","INSERT OR REPLACE INTO project_document SELECT * FROM project_document"):
                with self.subTest(sql=sql),self.assertRaises(sqlite3.Error):db.execute(sql)
                db.rollback()
        with Store(self.c) as s:
            s.document_write=True
            with self.assertRaises(sqlite3.IntegrityError):
                s.db.execute("UPDATE project_document SET source_sha256=? WHERE project_file_id=?",('a'*64,fid))
