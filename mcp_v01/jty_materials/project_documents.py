"""Manager-owned document state/publication and read-only project search.

Compute threads only read originals and write disposable JSONL. All SQLite,
projection, index and progress writes execute on the manager dispatcher under
operation.lock. Completion is an internal RPC capability, never a client path.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import sqlite3
import threading
import time
import uuid

from .common import FileLock, atomic_write, consistent_read, digest, now, normalize, safe_path
from .document_extract import extract, SUPPORTED, Unavailable
from . import document_index
from .projects import Projects, project, text
from .store import Store
from .workflow import audit

ACTIONS = ('project.document.extract', 'project.document.retry', 'project.document.rebuild', 'project.document.index.rebuild')
INTERNAL = ('project.document._progress', 'project.document._finish')


def remove_tree(root, path):
    root, path = root.resolve(), path.resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError('refusing cleanup outside derivative root')
    if path.exists():
        shutil.rmtree(path)


class Documents:
    def __init__(self, config):
        self.c = config

    @consistent_read
    def status(self, pid):
        with Store(self.c, readonly=True) as s:
            project(s.db, pid)
            return [dict(r) for r in s.db.execute('''SELECT d.*,f.filename,f.extension,
                r.run_id,r.status AS run_status,r.processed_units,r.total_units,r.progress_kind
                FROM project_document d JOIN project_file f USING(project_file_id)
                LEFT JOIN project_document_run r ON r.run_id=(SELECT rr.run_id FROM project_document_run rr
                    WHERE rr.document_id=d.document_id ORDER BY rr.rowid DESC LIMIT 1)
                WHERE f.project_id=? ORDER BY f.imported_at DESC''', (pid,))]

    @consistent_read
    def candidates(self, pid):
        """Discovered material candidates for one project, newest first."""
        with Store(self.c, readonly=True) as s:
            project(s.db, pid)
            return [dict(r) for r in s.db.execute('''
                SELECT c.*, f.filename, f.extension AS file_extension,
                       i.document_order AS image_order, i.nearest_heading, i.media_path,
                       u.unit_index AS source_unit_index, u.section_path AS source_section
                FROM project_material_candidate c
                JOIN project_document d ON d.document_id=c.document_id
                JOIN project_file f ON f.project_file_id=d.project_file_id
                LEFT JOIN project_document_image i ON i.image_id=c.source_image_id
                LEFT JOIN project_document_unit u ON u.unit_id=c.source_unit_id
                WHERE c.project_id=? AND c.status='active'
                ORDER BY c.subject_name, c.candidate_type, c.created_at''', (pid,))]

    @consistent_read
    def search(self, pid, query, page=1):
        query = text(query, '搜索词', True, 200)
        page = max(1, int(page))
        start, total, items = (page - 1) * 20, 0, []
        with Store(self.c, readonly=True) as s:
            project(s.db, pid)
            count = s.db.execute('''SELECT count(*) FROM project_document_unit u JOIN project_document d USING(document_id)
                JOIN project_file f USING(project_file_id) WHERE f.project_id=?''', (pid,)).fetchone()[0]
            if not count:
                return dict(items=[], total=0, page=page, pages=1)
            if s.db.execute('''SELECT 1 FROM project_document d JOIN project_file f USING(project_file_id)
                WHERE f.project_id=? AND d.index_status!='ready' AND d.unit_count>0''', (pid,)).fetchone():
                raise RuntimeError('项目内容索引待修复，请重新整理或重建项目搜索')
            for hit in document_index.matches(self.c, pid, query):
                row = s.db.execute('''SELECT f.project_id,f.project_file_id,f.filename,f.extension,u.*,
                    i.document_order AS image_order
                    FROM project_document_unit u JOIN project_document d USING(document_id)
                    JOIN project_file f USING(project_file_id)
                    LEFT JOIN project_document_image i ON i.image_id=u.image_id
                    WHERE f.project_id=? AND u.unit_id=? AND u.document_id=? AND u.run_id=? AND d.current_run_id=u.run_id''',
                    (pid, hit['unit_id'], hit['document_id'], hit['run_id'])).fetchone()
                if row is None:
                    continue  # stale derivative never yields stale or foreign content
                if start <= total < start + 20:
                    item = dict(row)
                    raw = item.pop('text_raw')
                    # NFKC may change length. Locate on raw only when it matches;
                    # otherwise show a faithfully retained raw prefix.
                    offset = raw.casefold().find(query.casefold())
                    if offset < 0:
                        folded, mapping = [], []
                        for i, char in enumerate(raw):
                            part = normalize(char).casefold()
                            folded.append(part)
                            mapping.extend([i] * len(part))
                        matched = ''.join(folded).find(normalize(query).casefold())
                        if matched >= 0:
                            offset = mapping[matched]
                    offset = max(0, offset - 70)
                    item['snippet'] = ('…' if offset else '') + raw[offset:offset+320] + ('…' if len(raw) > offset+320 else '')
                    item.pop('text_norm')
                    items.append(item)
                total += 1
        return dict(items=items, total=total, page=page, pages=max(1, (total+19)//20))


def resolve_source(config, pid, fid):
    """Manager-side project-file resolution with the same integrity checks as
    Projects.resolve_file, but without the maintenance-time read guard: the
    document dispatcher serializes everything under operation.lock already,
    and auto-extraction runs while the upload job still holds maintenance."""
    with Store(config, readonly=True) as s:
        row = s.db.execute('SELECT * FROM project_file WHERE project_id=? AND project_file_id=?', (pid, fid)).fetchone()
        if row is None:
            return None
        row = dict(row)
    path = safe_path(config.repository_root, row['relative_path'])
    if not path.is_file() or path.stat().st_size != row['size_bytes'] or digest(path) != row['sha256']:
        raise ValueError('项目文件校验失败，请运行系统体检')
    row['path'] = path
    return row


class DocumentJobs:
    def __init__(self, supervisor):
        self.owner, self.c = supervisor, supervisor.c
        self.queue = queue.Queue()
        self.capabilities = {}
        self.worker = None

    def start_worker(self):
        if self.worker is None or not self.worker.is_alive():
            self.worker = threading.Thread(target=self.work, name='project-document-compute', daemon=True)
            self.worker.start()

    def enqueue(self, task):
        self.queue.put(task)
        self.start_worker()

    def work(self):
        from .manager import request
        while True:
            task = self.queue.get()
            if task is None:
                return
            run_id, source, capability = task
            folder = self.c.document_tmp / run_id
            result = None
            try:
                folder.mkdir(parents=True, exist_ok=True)
                last = [0.0]

                def progress(processed, total, kind):
                    if processed == 0 or processed == total or time.monotonic() - last[0] >= 1:
                        request(self.c, 'project.document._progress', dict(run_id=run_id, capability=capability,
                                processed=processed, total=total, kind=kind), start=False)
                        last[0] = time.monotonic()
                progress(0, None, 'document')
                entry = self.capabilities.get(run_id) or {}
                result = extract(source, folder/'units.jsonl', self.c, progress, resume=entry.get('resume'))
            except Unavailable as error:
                result = dict(status='unsupported', error=str(error), unit_count=0, text_length=0)
            except Exception as error:
                result = dict(status='failed', error=str(error)[:2000], unit_count=0, text_length=0)
            # Staging is only selected by a manager-held capability. Clients
            # cannot supply a stage path, extracted text, or publication result.
            entry = self.capabilities.get(run_id)
            if entry is None:
                remove_tree(self.c.document_tmp, folder)
                self.queue.task_done()
                continue
            entry['result'] = result
            try:
                reply = request(self.c, 'project.document._finish', dict(run_id=run_id, capability=capability), start=False)
                if reply.get('running') is False:
                    raise RuntimeError('manager unavailable at publication')
            except Exception as error:
                self.c.log('project.document.reply_failed', run_id=run_id, error=str(error))
                # If the manager cannot persist even the failure record, a
                # subsequent user retry recognizes an interrupted active run.
                self.capabilities.pop(run_id, None)
            finally:
                remove_tree(self.c.document_tmp, folder)
                self.queue.task_done()

    def recover(self):
        """On manager startup, interrupt persisted runs; user may retry safely."""
        if not self.c.db_path.is_file():
            return
        with closing(sqlite3.connect(self.c.db_path.as_uri()+'?mode=ro', uri=True)) as db:
            if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='project_document'").fetchone():
                return  # explicit migration remains available
        with FileLock(self.c.repository_root/'db/operation.lock'), Store(self.c) as s:
            s.document_write = True
            with s.db:
                s.db.execute("UPDATE project_document_run SET status='failed',error='manager restarted; retry extraction',updated_at=? WHERE status IN ('queued','processing')", (now(),))
                s.db.execute("UPDATE project_document SET status='failed',error='manager restarted; retry extraction',updated_at=? WHERE status IN ('queued','processing')", (now(),))
            if self.c.document_tmp.exists():
                for folder in self.c.document_tmp.iterdir():
                    if folder.is_dir():
                        remove_tree(self.c.document_tmp, folder)
            self.cleanup(s.db)

    def cleanup(self, db):
        expected = {safe_path(self.c.document_root, r[0]) for r in db.execute('SELECT projection_path FROM project_document_unit')}
        if self.c.document_root.exists():
            for path in self.c.document_root.rglob('*.txt'):
                resolved = path.resolve()
                if not resolved.is_relative_to(self.c.document_root.resolve()):
                    raise ValueError('projection escapes derivative root')
                if resolved not in expected:
                    path.unlink()
            for folder in sorted(self.c.document_root.rglob('*'), key=lambda p: len(p.parts), reverse=True):
                if folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()

    def rebuild(self, db):
        for row in db.execute('SELECT projection_path,text_norm,projection_sha256 FROM project_document_unit'):
            data = row['text_norm'].encode()
            if hashlib.sha256(data).hexdigest() != row['projection_sha256']:
                raise ValueError('unit projection mapping corrupt; re-extract source')
            atomic_write(safe_path(self.c.document_root, row['projection_path']), data)
        # A corrupt/missing disposable index can always be recreated from DB.
        if not document_index.health(self.c, db)['healthy']:
            for suffix in ('', '-wal', '-shm'):
                Path(str(self.c.document_index)+suffix).unlink(missing_ok=True)
        document_index.publish(self.c, db)
        db.execute("UPDATE project_document SET index_status='ready'")
        self.cleanup(db)

    def _queue_extract(self, s, pid, fid, actor):
        """Create a queued run for one document. Caller holds operation.lock
        and an open writable Store with document_write set; the caller's
        transaction commits the durable registration before we enqueue."""
        project(s.db, pid, True)
        source = resolve_source(self.c, pid, fid)
        if source is None:
            raise ValueError('项目文件不存在')
        actor = text(actor, '经办人', True, 100)
        doc = s.db.execute('SELECT * FROM project_document WHERE project_file_id=?', (fid,)).fetchone()
        if doc is None or doc['source_sha256'] != source['sha256']:
            raise ValueError('document source SHA mismatch; run doctor')
        active = s.db.execute("SELECT run_id FROM project_document_run WHERE document_id=? AND status IN ('queued','processing')", (doc['document_id'],)).fetchone()
        if active:
            if active[0] in self.capabilities:
                return None, dict(project_id=pid, document_id=doc['document_id'], run_id=active[0], duplicate=True)
            s.db.execute("UPDATE project_document_run SET status='failed',error='interrupted write; retry',updated_at=? WHERE run_id=?", (now(),active[0]))
        rid, cap, at = uuid.uuid4().hex, uuid.uuid4().hex, now()
        s.db.execute("INSERT INTO project_document_run(run_id,document_id,status,requested_by,created_at,updated_at) VALUES(?,?,'queued',?,?,?)", (rid,doc['document_id'],actor,at,at))
        s.db.execute("UPDATE project_document SET status='queued',error=NULL,updated_at=? WHERE document_id=?", (at,doc['document_id']))
        # Resume set: previously OCR'd images with a still-valid cache key are
        # reused verbatim; only failures and new pictures run the engine again.
        resume = {row['ocr_cache_key']: dict(text_raw=row['ocr_text_raw'] or '', text_norm=row['ocr_text_norm'] or '',
                                             elapsed_ms=row['ocr_elapsed_ms'])
                  for row in s.db.execute(
                      "SELECT ocr_cache_key,ocr_text_raw,ocr_text_norm,ocr_elapsed_ms FROM project_document_image"
                      " WHERE document_id=? AND ocr_status='done' AND ocr_cache_key IS NOT NULL", (doc['document_id'],))}
        return (rid, source, cap), dict(project_id=pid, document_id=doc['document_id'], run_id=rid,
                                        status='queued', _resume=resume)

    def auto_extract(self, upload_result, actor):
        """Zero-fill: queue extraction right after a project original is
        stored, with no second click. Caller is the manager dispatcher, which
        already holds operation.lock and this job's maintenance marker."""
        if not isinstance(upload_result, dict) or upload_result.get('duplicate') or not upload_result.get('saved', True):
            return None
        pid, fid = upload_result.get('project_id'), upload_result.get('project_file_id')
        if not pid or not fid:
            return None
        source = resolve_source(self.c, pid, fid)
        if source is None or source['extension'] not in SUPPORTED:
            return None
        with Store(self.c) as s:
            s.document_write = True
            with s.db:
                s.db.execute('BEGIN IMMEDIATE')
                task, result = self._queue_extract(s, pid, fid, actor or 'local_webui')
            if task:
                rid, source, cap = task
                resume = result.pop('_resume', {})
                self.capabilities[rid] = dict(capability=cap, source=source,
                                              document_id=result['document_id'], resume=resume)
                self.enqueue(task)  # only after the durable registration committed
        return result

    def execute(self, action, payload):
        if action not in ACTIONS + INTERNAL:
            raise ValueError('unknown document action')
        allowed = {'project_id','project_file_id','actor'} if action in ACTIONS else {'run_id','capability','processed','total','kind'}
        if set(payload) - allowed:
            raise ValueError('document actions accept identities only, never filesystem paths or text')
        task = None
        with FileLock(self.c.repository_root/'db/operation.lock'):
            self.c.assert_readable()
            with Store(self.c) as s:
                s.document_write = True
                with s.db:
                    s.db.execute('BEGIN IMMEDIATE')
                    if action == 'project.document.index.rebuild':
                        # Administrative action, all derived text, no OCR or source edits.
                        self.rebuild(s.db)
                        result = dict(rebuilt=True, project_id=payload.get('project_id'))
                    elif action in ACTIONS:
                        pid, fid = int(payload['project_id']), payload['project_file_id']
                        task, result = self._queue_extract(s, pid, fid, payload.get('actor', '本机用户'))
                        if task is not None:
                            audit(s.db, text(payload.get('actor', '本机用户'), '经办人', True, 100), action,
                                  'project_file', fid, dict(project_id=pid, document_id=result['document_id'],
                                  run_id=result['run_id']))
                    else:
                        rid = payload.get('run_id')
                        entry = self.capabilities.get(rid)
                        if not entry or payload.get('capability') != entry['capability']:
                            raise ValueError('invalid internal document capability')
                        row = s.db.execute('SELECT * FROM project_document_run WHERE run_id=?', (rid,)).fetchone()
                        if row['status'] not in ('queued','processing'):
                            return dict(unchanged=True)
                        if action.endswith('_progress'):
                            s.db.execute("UPDATE project_document_run SET status='processing',processed_units=?,total_units=?,progress_kind=?,updated_at=? WHERE run_id=?", (payload['processed'],payload['total'],payload['kind'],now(),rid))
                            s.db.execute("UPDATE project_document SET status='processing',updated_at=? WHERE document_id=?", (now(),entry['document_id']))
                            result = dict(processing=True)
                        else:
                            result = self.finish(s, rid, entry)
            if task:
                rid, source, cap = task
                resume = result.pop('_resume', {})
                self.capabilities[rid] = dict(capability=cap, source=source, document_id=result['document_id'], resume=resume)
                self.enqueue(task)  # only after the durable registration committed
            self.owner.publish_generation()
        return result

    def finish(self, s, rid, entry):
        result = dict(entry['result'])
        did, source = entry['document_id'], entry['source']
        original = Projects(self.c).resolve_file(source['project_id'], source['project_file_id'])
        if original is None or original['sha256'] != source['sha256']:
            raise ValueError('source SHA mismatch at publication')
        if result['status'] in ('complete','partial'):
            s.db.execute('DELETE FROM project_material_candidate WHERE document_id=?', (did,))
            s.db.execute('DELETE FROM project_document_unit WHERE document_id=?', (did,))
            s.db.execute('DELETE FROM project_document_image WHERE document_id=?', (did,))
            stage = self.c.document_tmp/rid/'units.jsonl'
            final = safe_path(self.c.document_root, f'{did}/{rid}')
            final.parent.mkdir(parents=True, exist_ok=True)
            staged = stage.parent/'projections'
            staged.mkdir(exist_ok=True)  # empty documents still have a publishable directory
            os.rename(staged, final)  # UUID run directory, no replacement of previous text
            image_ids = {}
            images_stage = stage.parent/'images.jsonl'
            if images_stage.is_file():
                at = now()
                with images_stage.open(encoding='utf-8') as stream:
                    for line in stream:
                        record = json.loads(line)
                        # units reference images first (foreign key), keyed by the
                        # stable (document_order, media_path) anchor from staging.
                        image_ids[(record['document_order'], record['media_path'])] = uuid.uuid4().hex
                with images_stage.open(encoding='utf-8') as stream:
                    for line in stream:
                        record = json.loads(line)
                        s.db.execute("""INSERT INTO project_document_image(image_id,document_id,run_id,document_order,
                            media_path,relationship_id,section_path,nearest_heading,preceding_text,following_text,
                            width_px,height_px,mime,byte_size,sha256,ocr_status,ocr_text_raw,ocr_text_norm,
                            ocr_error,ocr_elapsed_ms,ocr_cache_key,created_at,updated_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (image_ids[(record['document_order'], record['media_path'])], did, rid,
                             record['document_order'], record['media_path'], record.get('relationship_id'),
                             record.get('section_path'), record.get('nearest_heading'),
                             record.get('preceding_text'), record.get('following_text'),
                             record.get('width_px'), record.get('height_px'), record.get('mime'),
                             record.get('byte_size') or 0, record.get('sha256'), record['ocr_status'],
                             record.get('ocr_text_raw'), record.get('ocr_text_norm'), record.get('ocr_error'),
                             record.get('ocr_elapsed_ms'), record.get('ocr_cache_key'), at, now()))
            count, length = 0, 0
            with stage.open(encoding='utf-8') as stream:
                for i, line in enumerate(stream, 1):
                    unit = json.loads(line)
                    image_ref = unit.pop('_image_ref', None)
                    image_id = image_ids.get(tuple(image_ref)) if image_ref else None
                    if image_ref and image_id is None:
                        raise ValueError('staging image reference has no published registry row')
                    count, length = i, length + len(unit['text_raw'])
                    if normalize(unit['text_raw']) != unit['text_norm']:
                        raise ValueError('staging raw/normalized text mismatch')
                    relative = f'{did}/{rid}/unit-{i:06d}.txt'
                    data = unit['text_norm'].encode()
                    sha = hashlib.sha256(data).hexdigest()
                    if digest(safe_path(self.c.document_root, relative)) != sha:
                        raise ValueError('staging projection SHA mismatch')
                    values = dict(unit_id=uuid.uuid4().hex, document_id=did, run_id=rid, unit_index=i,
                                  image_id=image_id, projection_path=relative, projection_sha256=sha, **unit)
                    cols = ','.join(values)
                    s.db.execute(f'INSERT INTO project_document_unit({cols}) VALUES({",".join("?" for _ in values)})', tuple(values.values()))
            if count != result['unit_count'] or length != result['text_length']:
                raise ValueError('staging count/length mismatch')
            s.db.execute("UPDATE project_document SET current_run_id=?,unit_count=?,text_length=?,index_status='pending',summary_json=? WHERE document_id=?",
                         (rid,result['unit_count'],result['text_length'],
                          json.dumps(result.get('summary') or {}, ensure_ascii=False), did))
        s.db.execute('UPDATE project_document_run SET status=?,error=?,updated_at=? WHERE run_id=?', (result['status'],result.get('error'),now(),rid))
        s.db.execute('UPDATE project_document SET status=?,error=?,updated_at=? WHERE document_id=?', (result['status'],result.get('error'),now(),did))
        # DB transaction commits before index mutation. If index fails, keep the
        # text committed and explicitly unavailable until rebuild, never close
        # Material browsing behind maintenance for a derivative failure.
        return result

    def publish_finished(self, rid):
        entry = self.capabilities.get(rid)
        if not entry:
            return
        with FileLock(self.c.repository_root/'db/operation.lock'), Store(self.c) as s:
            s.document_write = True
            try:
                document_index.publish(self.c, s.db, entry['document_id'])
                with s.db:
                    s.db.execute("UPDATE project_document SET index_status='ready' WHERE document_id=?", (entry['document_id'],))
                try:
                    from .discovery import discover
                    discover(self.c, s, entry['document_id'])
                except Exception as error:
                    # Discovery is a derivative of committed text; its failure
                    # never degrades the document or its search availability.
                    self.c.log('project.discovery_failed', run_id=rid, error=str(error)[:500])
                self.cleanup(s.db)
            except Exception as error:
                with s.db:
                    s.db.execute("UPDATE project_document SET index_status='failed',error=? WHERE document_id=?", ('rebuildable derivative error: '+str(error)[:1000],entry['document_id']))
                self.c.log('project.document.index_failed', run_id=rid, error=str(error))
            self.owner.publish_generation()
        self.capabilities.pop(rid, None)

    def fail(self, payload, error):
        rid = payload.get('run_id')
        entry = self.capabilities.get(rid)
        if not entry or entry['capability'] != payload.get('capability'):
            return
        try:
            with FileLock(self.c.repository_root/'db/operation.lock'), Store(self.c) as s:
                s.document_write = True
                with s.db:
                    s.db.execute("UPDATE project_document_run SET status='failed',error=?,updated_at=? WHERE run_id=?", (str(error)[:2000],now(),rid))
                    s.db.execute("UPDATE project_document SET status='failed',error=?,updated_at=? WHERE document_id=?", (str(error)[:2000],now(),entry['document_id']))
                self.cleanup(s.db)
                self.owner.publish_generation()
        except Exception as failure:
            self.c.log('project.document.failure_record_failed', run_id=rid, error=str(failure))
        self.capabilities.pop(rid, None)
