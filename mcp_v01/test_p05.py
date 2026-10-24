"""P0.5: isolated HTTP, transaction failures, and a real named-pipe manager.

Optional real runtime test uses P05_DOWSE_EXE or local/runtime/dowse/dowse.exe;
it COPIES that executable before launch, never shares a deployed runtime/index.
"""
from email.message import Message
from contextlib import closing
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

import test_v01 as old
from jty_materials.common import Config, atomic_write, digest, safe_path
from jty_materials.store import Store
from jty_materials import manager, workflow
from jty_materials.query import Query
from webui import app, api, catalog, pages, uploads
from webui.same_origin import post_allowed


class OriginTests(unittest.TestCase):
    def allowed(self, host, origin=None, local='192.0.2.10', port=8800, **extra):
        headers = Message()
        if host is not None:
            headers['Host'] = host
        if origin is not None:
            headers['Origin'] = origin
        for k, v in extra.items():
            headers[k.replace('_', '-')] = v
        handler = SimpleNamespace(headers=headers, server=SimpleNamespace(server_port=port),
                                  connection=SimpleNamespace(getsockname=lambda: (local, port)))
        with mock.patch('webui.same_origin.socket.gethostname', return_value='office-pc'), \
                mock.patch('webui.same_origin.socket.getfqdn', return_value='office-pc.example'):
            return post_allowed(handler)

    def test_direct_lan_localhost_computer_name_and_ipv6(self):
        for host, local in [('192.0.2.10', '192.0.2.10'), ('127.0.0.1', '127.0.0.1'),
                            ('localhost', '127.0.0.1'), ('OFFICE-PC', '192.0.2.10'),
                            ('office-pc.example', '192.0.2.10'), ('[::1]', '::1')]:
            with self.subTest(host=host):
                self.assertTrue(self.allowed(host+':8800', 'http://'+host+':8800', local=local))

    def test_absent_origin_controlled_calls_and_fetch_metadata(self):
        self.assertTrue(self.allowed('192.0.2.10:8800'))
        self.assertFalse(self.allowed('192.0.2.10:8800', Sec_Fetch_Site='cross-site'))

    def test_malicious_malformed_and_different_origins(self):
        for origin in ['https://evil.example', 'http://evil.example:8800', 'null',
                       'http://192.0.2.10:8801', 'https://192.0.2.10:8800',
                       'http://192.0.2.10:8800/path', 'http://192.0.2.10:8800?x',
                       'http://192.0.2.10:8800?', 'http://192.0.2.10:8800#',
                       'http://192.0.2.10:8800#x', 'http://user@192.0.2.10:8800',
                       'http://192.0.2.10:8800 http://evil.example', '']:
            with self.subTest(origin=origin):
                self.assertFalse(self.allowed('192.0.2.10:8800', origin))

    def test_host_validation_and_default_port(self):
        for host in [None, '', 'evil.example:8800', '192.0.2.11:8800',
                     '192.0.2.10:8801', '192.0.2.10:8800/path', 'user@192.0.2.10:8800']:
            with self.subTest(host=host):
                self.assertFalse(self.allowed(host))
        self.assertFalse(self.allowed('evil.example:8800', 'http://evil.example:8800'))
        self.assertTrue(self.allowed('192.0.2.10', 'http://192.0.2.10:80', port=80))
        self.assertFalse(self.allowed('192.0.2.10:0', 'http://192.0.2.10:0', port=80))
        self.assertFalse(self.allowed('192.0.2.10:', 'http://192.0.2.10:', port=80))

    def test_duplicate_headers(self):
        self.assertFalse(self.allowed('192.0.2.10:8800', Host='192.0.2.10:8800'))
        self.assertFalse(self.allowed('192.0.2.10:8800', 'http://192.0.2.10:8800', Origin='null'))

    def test_all_post_routes_guarded_before_dispatch_and_body_read(self):
        routes = [p.pattern for method, p, _ in app.ROUTES if method == 'POST']
        self.assertEqual(set(routes), {'^/api/reextract$', '^/api/upload$', '^/api/review$', '^/api/feedback$', '^/api/doctor$', '^/api/project/(?P<action>create|update|archive|restore|material/link|material/unlink)$', '^/api/project/file$', '^/api/project/document/(?P<action>extract|retry|rebuild)$'})
        context = SimpleNamespace(config=SimpleNamespace(log=lambda *a, **k: None), api=mock.Mock())
        handler = type('IsolatedHandler', (app.Handler,), {'app': context})
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for route in routes + ['^/future-write$']:
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                conn.request('POST', route[1:-1], body=b'unread', headers={'Origin': 'https://evil.example'})
                response = conn.getresponse()
                self.assertEqual(response.status, 403)
                self.assertIn('禁止跨站', response.read().decode())
                self.assertTrue(response.will_close)
                conn.close()
            for origin in [None, f'http://127.0.0.1:{server.server_port}']:
                context.api.submit_review.return_value = (200, {'ok': True})
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                conn.request('POST', '/api/review', headers={'Origin': origin} if origin else {})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                response.read(); conn.close()
            self.assertEqual(context.api.submit_review.call_count, 2)
            context.api.upload.assert_not_called()
            context.api.submit_feedback.assert_not_called()
            context.api.doctor_start.assert_not_called()
            context.api.upload.return_value = (400, {'error': catalog.UPLOAD_FORMAT_ERROR})
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            conn.request('POST', '/api/upload', body=b'office body rejected before read')
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            self.assertTrue(response.will_close)
            response.read(); conn.close()
        finally:
            server.shutdown(); server.server_close(); thread.join()


class OfficeTests(unittest.TestCase):
    def test_whitelist_and_rejection_before_staging_for_both_modes(self):
        self.assertEqual(catalog.UPLOAD_EXTS, ('.pdf', '.png', '.jpg', '.jpeg'))
        instance = api.Api.__new__(api.Api)
        for mode in ['import', 'replace']:
            for ext in ['doc', 'docx', 'xlsx', 'md', 'aip', 'exe', 'pdf.docx', '']:
                with self.subTest(mode=mode, ext=ext), mock.patch.object(uploads, 'stage_body') as stage:
                    req = SimpleNamespace(query={'mode': mode, 'filename': 'file.'+ext})
                    code, result = instance.upload(req)
                    self.assertEqual(code, 400)
                    self.assertEqual(result['error'], catalog.UPLOAD_FORMAT_ERROR)
                    stage.assert_not_called()

    def test_uppercase_supported_extensions_reach_staging(self):
        instance = api.Api.__new__(api.Api); instance.c = mock.Mock()
        for ext in ['PDF', 'PNG', 'JPG', 'JPEG']:
            req = SimpleNamespace(query=dict(filename='file.'+ext, actor='fixture', subtype='other', title='fixture'),
                                  headers={'Content-Length': '5'}, rfile=io.BytesIO(b'bytes'))
            with mock.patch.object(uploads, 'stage_body', side_effect=ValueError('stage reached')):
                self.assertEqual(instance.upload(req), (400, {'error': 'stage reached'}))

    def test_worker_rejects_office_without_rpc(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'tmp-file'; path.write_bytes(b'office')
            worker = uploads.UploadManager(mock.Mock())
            job = uploads.UploadJob('fixture', 'import', 'file.docx', 6, {})
            with mock.patch.object(uploads, 'request') as rpc:
                worker._run(job, path)
                rpc.assert_not_called()
            self.assertEqual(job.error, catalog.UPLOAD_FORMAT_ERROR)
            self.assertFalse(path.exists())

    def test_import_and_replace_render_the_same_boundary(self):
        detail = {'material_id': 1, 'title': 'fixture'}
        with mock.patch.object(pages.Query, 'get_material', return_value=detail), \
                mock.patch.object(pages.display, 'display_from_detail', return_value='fixture'), \
                mock.patch.object(pages.service, 'system_ok', return_value=True):
            for params in [{}, {'material': '1'}]:
                html = pages.upload_page(mock.Mock(), params)
                self.assertIn('accept=".pdf,.png,.jpg,.jpeg"', html)
                self.assertIn('DOCX / XLSX 暂不支持，.doc 不支持', html)
                self.assertNotIn('支持 PDF、图片、Word、Excel', html)


class FixtureOcr(old.FakeOcr):
    def __init__(self, config):
        super().__init__(config, 'fixture document searchable text 35kV')
    def close(self):
        pass


class BrokenOcr(FixtureOcr):
    def document(self, source):
        raise RuntimeError('injected OCR failure')


class MemoryDowse:
    def __init__(self, config):
        self.c = config; self.watch = None
    def stop(self):
        self.watch = None
    def start(self):
        self.watch = SimpleNamespace(pid=123)
    def maintain(self, *args):
        self.start()
    def health(self):
        if self.watch is None:
            raise RuntimeError('watch not alive')


class RecoveryTests(unittest.TestCase):
    setUp = old.V01Tests.setUp

    def supervisor(self):
        with mock.patch.object(manager, 'Dowse', MemoryDowse):
            return manager.Supervisor(self.c)

    def assert_db(self):
        with Store(self.c, readonly=True) as store:
            self.assertEqual(store.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(store.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def next_write(self, supervisor):
        self.assertTrue(supervisor.dispatch('status', {})['running'])
        self.assertFalse(self.c.maintenance_file.exists())
        self.assert_db()
        result = supervisor.dispatch('replace', dict(source=str(self.source), material_id=1, actor='fixture'))
        self.assertTrue(result['unchanged'])
        self.assertFalse(self.c.maintenance_file.exists())
        supervisor.d.health()

    def test_all_action_exceptions_reconcile_and_next_write_works(self):
        supervisor = self.supervisor()
        for action in ['import', 'replace', 'batch_import', 'review', 'backup', 'migrate-v03',
                       'migrate-v04', 'migrate-v05', 'project.create', 'project.update', 'project.archive', 'project.restore', 'project.file.add', 'project.material.link', 'project.material.unlink', 'phase_import', 'phase_end', 'phase_relations',
                       'phase_enrich', 'reextract', 'phase_reextract', 'candidates', 'index', 'add', 'unknown']:
            with self.subTest(action=action), mock.patch.object(supervisor.d, 'stop', side_effect=[RuntimeError('injected stop'), None]):
                with self.assertRaisesRegex(RuntimeError, 'injected stop'):
                    supervisor.dispatch(action, {})
            self.next_write(supervisor)

    def test_unrecoverable_evidence_retains_lock_and_blocks_new_write_until_repaired(self):
        supervisor = self.supervisor()
        with Store(self.c, readonly=True) as s:
            row = s.db.execute("SELECT relative_path FROM material_file WHERE role='original'").fetchone()
        original = safe_path(self.c.repository_root, row[0]); saved = original.read_bytes()
        original.write_bytes(b'corrupt fixture')
        with self.assertRaises(ValueError):
            supervisor.job('unknown', {})
        self.assertEqual(json.loads(self.c.maintenance_file.read_text())['state'], 'recovery_required')
        with mock.patch.object(manager, 'import_material') as mutation:
            with self.assertRaisesRegex(RuntimeError, 'MAINTENANCE'):
                supervisor.job('replace', {})
            mutation.assert_not_called()
        self.assertTrue(supervisor.status()['running']); self.assert_db()
        original.write_bytes(saved)
        self.assertTrue(supervisor.job('replace', dict(source=str(self.source), material_id=1, actor='fixture'))['unchanged'])
        self.next_write(supervisor)

    def test_persistent_index_or_watch_failure_retains_then_recovers(self):
        for method in ['maintain', 'health']:
            supervisor = self.supervisor()
            with self.subTest(method=method), mock.patch.object(supervisor.d, method, side_effect=RuntimeError('injected runtime failure')):
                with self.assertRaises(ValueError):
                    supervisor.job('unknown', {})
                self.assertTrue(self.c.maintenance_file.exists())
                self.assertTrue(supervisor.status()['running']); self.assert_db()
            supervisor.job('replace', dict(source=str(self.source), material_id=1, actor='fixture'))
            self.next_write(supervisor)

    def test_explicit_migration_retry_can_repair_old_schema_maintenance(self):
        supervisor = self.supervisor()
        # A rolled-back migration can leave a compatible older schema. Recovery
        # must not deadlock the very migration requested to repair that state.
        with Store(self.c) as s:
            s.db.execute("UPDATE schema_meta SET meta_value='jty-mcp-0.3.0' WHERE meta_key='schema_version'")
            s.db.commit()
        projection = self.c.materials/self.item['material_code']/'current.search.txt'
        expected_projection = digest(projection)
        projection.write_text('stale migration projection', encoding='utf-8')
        atomic_write(self.c.maintenance_file, '{"state":"recovery_required"}')
        with self.assertRaisesRegex(RuntimeError, 'MAINTENANCE'):
            supervisor.job('replace', {})
        from jty_materials.store import SCHEMA_VERSION
        def migrate(c):
            import sqlite3
            with closing(sqlite3.connect(c.db_path)) as db:
                with db:
                    db.execute("UPDATE schema_meta SET meta_value=? WHERE meta_key='schema_version'", (SCHEMA_VERSION,))
            return {'changed': True}
        with mock.patch.object(Store, 'migrate', migrate):
            self.assertTrue(supervisor.job('migrate-v04', {})['changed'])
        self.assertEqual(digest(projection), expected_projection)
        self.next_write(supervisor)


def fixture_manager(config_path):
    """Test-only process entry: real pipe, locks, SQL, files, dowse and watch.

    Only OCR and precisely selected failure points are replaced. There is no
    injection API in production code.
    """
    from jty_materials.dowse import Dowse
    from jty_materials import reextract as repair
    config = Config(config_path)
    real_import = workflow.import_material
    real_reextract = repair.reextract
    real_register = Store.register_file
    real_command = Dowse.command
    real_start = Dowse.start

    def injected_import(c, **payload):
        source = Path(payload['source'])
        label = source.stem
        if label in ('index-fail', 'watch-fail'):
            atomic_write(c.state_dir/(label+'.once'), '1')
        if label == 'ocr-fail':
            payload['ocr_factory'] = BrokenOcr
        payload.setdefault('ocr_factory', FixtureOcr)
        prior = None
        if payload.get('batch_context'):
            with Store(c, readonly=True) as store:
                prior = workflow.batch_receipt(store.db, payload['batch_context'])
        calls = 0
        def register(store, *args, **kwargs):
            nonlocal calls
            calls += 1
            if label in ('db-fail', 'replace-fail') and calls == 3:
                raise RuntimeError('injected mid-transaction failure')
            return real_register(store, *args, **kwargs)
        with mock.patch.object(Store, 'register_file', register):
            result = real_import(c, **payload)
        if label == 'postcommit-fail' and prior is None:
            raise RuntimeError('injected after commit before reply')
        return result

    def injected_reextract(c, payload, **kwargs):
        if payload['reason'] == 'fail':
            kwargs['ocr_factory'] = BrokenOcr
        kwargs.setdefault('ocr_factory', FixtureOcr)
        return real_reextract(c, payload, **kwargs)

    def command(d, action, *args):
        flag = config.state_dir/'index-fail.once'
        if flag.exists():
            flag.unlink()
            raise RuntimeError('injected index failure')
        return real_command(d, action, *args)

    def start(d):
        real_start(d)
        flag = config.state_dir/'watch-fail.once'
        if flag.exists():
            flag.unlink()
            raise RuntimeError('injected watch startup failure after spawn')

    with mock.patch.object(manager, 'import_material', injected_import), \
            mock.patch.object(repair, 'reextract', injected_reextract), \
            mock.patch('jty_materials.phase_ocr.PhaseOcr', FixtureOcr), \
            mock.patch.object(Dowse, 'command', command), mock.patch.object(Dowse, 'start', start):
        manager.serve(config)


class RealManagerTests(unittest.TestCase):
    def test_real_manager_failure_injections_and_receipt_replays(self):
        executable = Path(os.environ.get('P05_DOWSE_EXE', Path(__file__).resolve().parents[1]/'local/runtime/dowse/dowse.exe'))
        if not executable.is_file():
            self.skipTest('install dowse or set P05_DOWSE_EXE to run real process regression')
        # Short path: old Windows MAX_PATH also applies to retained versions.
        artifacts = Path(__file__).resolve().parents[1]/'runs/large-document/project-center'
        artifacts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='p05-', dir=artifacts, delete=False) as root:
            root = Path(root)
            runtime = root/'bin'; runtime.mkdir()
            shutil.copy2(executable, runtime/'dowse.exe')
            atomic_write(root/'config.json', json.dumps(dict(repository_root='repository', db_path='repository/db/materials.db',
                state_dir='state', logs_dir='logs', backup_dir='backups', index_dir='index/dowse-index',
                dowse_exe='bin/dowse.exe', umi_exe='unused/Umi-OCR.exe', umi_url='http://127.0.0.1:1224', fixture_only=True)))
            c = Config(root/'config.json'); c.materials.mkdir(parents=True); Store.initialize(c)
            c.logs_dir.mkdir()
            with (root/'console.log').open('wb') as log:
                child = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), '--fixture-manager', str(c.path)],
                    stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW,
                    env=dict(os.environ, DOWSE_FORCE_SLOW_LANE='1'))
                def call(action, payload=None):
                    return manager.request(c, action, payload, start=False, timeout=180)
                try:
                    until = time.monotonic()+15
                    while not call('status').get('running'):
                        if child.poll() is not None or time.monotonic() > until:
                            self.fail('fixture manager startup failed')
                        time.sleep(.1)
                    pid = call('status')['pid']
                    def check():
                        self.assertIsNone(child.poll())
                        status = call('status')
                        self.assertEqual(status['pid'], pid)
                        self.assertFalse(status['maintenance'])
                        self.assertTrue(call('health')['healthy'])
                        with Store(c, readonly=True) as store:
                            self.assertEqual(store.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                            self.assertEqual(store.db.execute('PRAGMA foreign_key_check').fetchall(), [])
                        # A genuine new write after every failure, not a mocked RPC.
                        source = root/('next-'+uuid.uuid4().hex[:6]+'.pdf'); source.write_bytes(os.urandom(32))
                        result = call('import', dict(source=str(source), kind='material', subtype='other', title='next', actor='fixture'))
                        self.assertGreater(result['material_id'], 0)
                        self.assertFalse(call('status')['maintenance'])
                    seed = root/'seed.pdf'; seed.write_bytes(b'fixture seed')
                    item = call('import', dict(source=str(seed), kind='material', subtype='other', title='seed', actor='fixture'))
                    check()
                    for label, action in [('ocr-fail', 'import'), ('db-fail', 'import'), ('replace-fail', 'replace'),
                                          ('index-fail', 'import'), ('watch-fail', 'import')]:
                        with self.subTest(failure=label):
                            source = root/(label+'.pdf'); source.write_bytes(label.encode())
                            payload = dict(source=str(source), actor='fixture')
                            payload.update(material_id=item['material_id']) if action == 'replace' else payload.update(kind='material', subtype='other', title=label)
                            before = Query(c).get_material(item['material_id'])
                            with Store(c, readonly=True) as store:
                                counts = [store.db.execute('SELECT count(*) FROM '+table).fetchone()[0]
                                          for table in ['material', 'material_file', 'audit_log']]
                            with self.assertRaisesRegex(RuntimeError, 'injected'):
                                call(action, payload)
                            self.assertEqual(Query(c).get_material(item['material_id']), before)
                            if label in ('ocr-fail', 'db-fail', 'replace-fail'):
                                with Store(c, readonly=True) as store:
                                    self.assertEqual(counts, [store.db.execute('SELECT count(*) FROM '+table).fetchone()[0]
                                                             for table in ['material', 'material_file', 'audit_log']])
                            check()
                    def batch_row(label, number):
                        source = root/(label+'.pdf'); source.write_bytes(label.encode())
                        return dict(source=str(source), actor='fixture', kind='material', subtype='other', title=label,
                            expected_source_sha256=digest(source), batch_context=dict(batch_id='fixture-batch', item_id=number,
                            plan_sha256='a'*64, source_sha256=digest(source), expected_current_sha256=None))
                    rows = [batch_row('ocr-fail', '1'), batch_row('phase-good', '2')]
                    result = call('phase_import', {'items': rows})
                    self.assertEqual([r['ok'] for r in result], [False, True]); check()
                    replay = call('phase_import', {'items': [rows[1]]})
                    self.assertEqual(replay[0]['receipt'], result[1]['receipt']); check()
                    record = dict(material_id=item['material_id'], evidence_file_id=item['files']['original'],
                        evidence_sha256=item['original_sha256'], expected_search_file_id=item['files']['search_text'],
                        operation_id='fixture-repair', actor='fixture', reason='fail')
                    with self.assertRaisesRegex(RuntimeError, 'OCR failure'):
                        call('reextract', record)
                    check()
                    result = call('phase_reextract', {'items': [record]})
                    self.assertFalse(result[0]['ok']); check()
                    record['reason'] = 'success'
                    receipt = call('reextract', record)
                    self.assertEqual(call('reextract', record), receipt); check()
                    row = batch_row('postcommit-fail', '3')
                    result = call('phase_import', {'items': [row]})
                    self.assertFalse(result[0]['ok']); check()
                    replay = call('phase_import', {'items': [row]})
                    self.assertTrue(replay[0]['ok'])
                    with Store(c, readonly=True) as s:
                        self.assertEqual(s.db.execute("SELECT count(*) FROM audit_log WHERE action='batch.item.commit' AND json_extract(detail_json,'$.item_id')='3'").fetchone()[0], 1)
                    check()
                    # A client disconnect after sending a genuine write must
                    # leave the process alive and the committed receipt replayable.
                    row = batch_row('lost-reply', '4')
                    row.pop('expected_source_sha256')
                    from multiprocessing.connection import Client
                    with Client(manager.endpoint(c), family='AF_PIPE', authkey=manager.key(c)) as conn:
                        conn.send_bytes(json.dumps(dict(action='batch_import', payload=row)).encode())
                    check()
                    receipt = call('batch_import', row)
                    self.assertEqual(call('batch_import', row), receipt)
                    with Store(c, readonly=True) as s:
                        self.assertEqual(s.db.execute("SELECT count(*) FROM audit_log WHERE action='batch.item.commit' AND json_extract(detail_json,'$.item_id')='4'").fetchone()[0], 1)
                    check()
                    events = (c.logs_dir/'events.jsonl').read_text(encoding='utf-8')
                    self.assertIn('manager.operation_failed', events)
                    self.assertIn('manager.operation_recovered', events)
                    self.assertIn('manager.item_failed', events)
                    self.assertIn('manager.reply_lost', events)
                finally:
                    if child.poll() is None:
                        call('shutdown')
                        child.wait(timeout=45)
                    self.assertEqual(child.returncode, 0)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--fixture-manager':
        fixture_manager(sys.argv[2])
    else:
        unittest.main(verbosity=2)
