"""Serialized project writes and consistent read views, independent of Material.

File publication uses a durable intention journal. A crash before DB commit
removes only this operation's unpublished evidence; a crash after commit retains
and verifies it. Recovery never replays a write or deletes registered evidence.
"""
import json
from contextlib import closing
import os
from pathlib import Path
import re
import shutil
import sqlite3
import uuid

from .common import atomic_write, consistent_read, digest, now, safe_path
from .schema_v05 import CATEGORIES
from .store import Store
from .workflow import audit

MAX_BYTES = 200 * 1024 * 1024
EXTENSIONS = ('.pdf', '.png', '.jpg', '.jpeg', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.md', '.zip')
INLINE = {'.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg'}
ACTIONS = ('project.create', 'project.update', 'project.archive', 'project.restore',
           'project.file.add', 'project.material.link', 'project.material.unlink')


def text(value, label, required=False, limit=500):
    if value is None and not required:
        return None
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        raise ValueError(label + '无效')
    value = value.strip()
    if len(value) > limit or (required and not value):
        raise ValueError(label + '不能为空或过长')
    return value or None


def project(db, project_id, writable=False):
    row = db.execute('SELECT * FROM project WHERE project_id=?', (project_id,)).fetchone()
    if row is None:
        raise ValueError('项目不存在')
    if writable and row['status'] != 'active':
        raise ValueError('项目已归档，请先恢复项目')
    return dict(row)


def metadata(payload):
    return {k: text(payload.get(k), label, k == 'name') for k, label in
            [('name', '项目名称'), ('tender_no', '招标编号'), ('tenderer', '招标人'), ('lot_name', '标段名称')]}


def execute(config, action, payload):
    """Called by Supervisor.job while operation.lock and maintenance are held."""
    if action not in ACTIONS:
        raise ValueError('unknown project action')
    actor = text(payload.get('actor'), '经办人', True, 100)
    if action == 'project.file.add':
        return add_file(config, payload, actor)
    with Store(config) as s:
        s.project_write = dict(actor=actor, action=action)
        with s.db:
            s.db.execute('BEGIN IMMEDIATE')
            at = now()
            if action == 'project.create':
                data = metadata(payload)
                pid = s.db.execute('SELECT coalesce(max(project_id),0)+1 FROM project').fetchone()[0]
                code = f'PRJ-{pid:06d}'
                s.db.execute('''INSERT INTO project(project_id,project_code,name,tender_no,tenderer,lot_name,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?)''', (pid, code, *data.values(), at, at))
                detail = dict(data, project_code=code)
            else:
                pid = int(payload['project_id'])
                old = project(s.db, pid, action not in ('project.archive', 'project.restore'))
                if action == 'project.update':
                    data = metadata(payload)
                    s.db.execute('UPDATE project SET name=?,tender_no=?,tenderer=?,lot_name=?,updated_at=? WHERE project_id=?',
                                 (*data.values(), at, pid))
                    detail = dict(before={k: old[k] for k in data}, after=data)
                elif action in ('project.archive', 'project.restore'):
                    status = 'archived' if action == 'project.archive' else 'active'
                    if old['status'] == status:
                        return dict(project_id=pid, unchanged=True)
                    s.db.execute('UPDATE project SET status=?,updated_at=? WHERE project_id=?', (status, at, pid))
                    detail = dict(before=old['status'], after=status)
                else:
                    mid = int(payload['material_id'])
                    if not s.db.execute('SELECT 1 FROM material WHERE material_id=?', (mid,)).fetchone():
                        raise ValueError('素材不存在')
                    link = s.db.execute('SELECT * FROM project_material WHERE project_id=? AND material_id=?', (pid, mid)).fetchone()
                    status = 'active' if action == 'project.material.link' else 'removed'
                    if (link and link['status'] == status) or (not link and status == 'removed'):
                        return dict(project_id=pid, material_id=mid, unchanged=True)
                    note = text(payload.get('note'), '引用备注', limit=2000)
                    if link:
                        s.db.execute('UPDATE project_material SET status=?,added_at=?,added_by=?,note=? WHERE reference_id=?',
                                     (status, at if status == 'active' else link['added_at'],
                                      actor if status == 'active' else link['added_by'],
                                      note if status == 'active' else link['note'], link['reference_id']))
                        rid = link['reference_id']
                    else:
                        rid = s.db.execute('''INSERT INTO project_material(project_id,material_id,status,added_at,added_by,note)
                            VALUES(?,?,?,?,?,?) RETURNING reference_id''', (pid, mid, status, at, actor, note)).fetchone()[0]
                    detail = dict(project_id=pid, material_id=mid, status=status,
                                  before=dict(link) if link else None, note=note)
                    audit(s.db, actor, action, 'project_material', rid, detail)
                    s.db.execute('UPDATE project SET updated_at=? WHERE project_id=?', (at, pid))
                    return dict(project_id=pid, material_id=mid, reference_id=rid)
            audit(s.db, actor, action, 'project', pid, detail)
    return dict(project_id=pid)


def validate_filename(filename):
    filename = text(filename, '文件名', True, 255)
    if '/' in filename or '\\' in filename or ':' in filename or filename.endswith(('.', ' ')):
        raise ValueError('文件名不允许包含路径')
    ext = Path(filename).suffix.lower()
    if ext not in EXTENSIONS:
        raise ValueError('不支持此项目文件格式')
    return filename, ext


def validate_signature(path, ext):
    """Check container signatures without unpacking or interpreting Office/ZIP."""
    with path.open('rb') as f:
        head = f.read(1024)
    expected = {'.pdf': (b'%PDF-',), '.png': (b'\x89PNG\r\n\x1a\n',),
                '.jpg': (b'\xff\xd8\xff',), '.jpeg': (b'\xff\xd8\xff',),
                '.doc': (b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1',),
                '.xls': (b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1',),
                '.ppt': (b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1',)}
    if ext in ('.docx', '.xlsx', '.pptx', '.zip'):
        expected[ext] = (b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08')
    if ext in expected and not head.startswith(expected[ext]):
        raise ValueError('文件内容与扩展名不符')
    if ext in ('.txt', '.md') and (b'\0' in head or head.startswith((b'MZ', b'PK\x03\x04', b'%PDF-'))):
        raise ValueError('文本文件内容与扩展名不符')


def journal_paths(config, record):
    fid, code, ext = record['file_id'], record['project_code'], record['extension']
    if not re.fullmatch('[a-f0-9]{32}', fid) or not re.fullmatch(r'PRJ-\d{6,}', code) or '.' + ext not in EXTENSIONS:
        raise ValueError('invalid project recovery journal')
    relative = f'projects/{code}/files/{fid}.{ext}'
    return (safe_path(config.repository_root, f'projects/.staging/{fid}.pending'),
            safe_path(config.repository_root, relative), relative)


def reconcile_files(config):
    """Under the manager lock, reconcile durable intentions against committed DB."""
    root = config.state_dir / 'project-file-journal'
    if not root.exists():
        return
    with closing(sqlite3.connect(config.db_path)) as db:
        for journal in root.glob('*.json'):
            record = json.loads(journal.read_text(encoding='utf-8'))
            staged, final, relative = journal_paths(config, record)
            row = db.execute('SELECT relative_path,sha256 FROM project_file WHERE project_file_id=?', (record['file_id'],)).fetchone()
            if row:
                if row != (relative, record['sha256']) or not final.is_file() or digest(final) != row[1]:
                    raise RuntimeError('registered project evidence checksum mismatch')
            elif final.exists() and not staged.exists():
                # A successful rename consumed staging. Never remove a path
                # that collided before publication, or bytes changed externally.
                if digest(final) != record['sha256']:
                    raise RuntimeError('unregistered project publication changed; inspect recovery journal')
                final.unlink()
            staged.unlink(missing_ok=True)
            journal.unlink()


def add_file(config, payload, actor):
    pid = int(payload['project_id'])
    category = payload.get('category')
    if category not in CATEGORIES:
        raise ValueError('项目文件分类无效')
    filename, ext = validate_filename(payload.get('filename'))
    source = Path(payload['source']).resolve(strict=True)
    if not source.is_file() or source.is_relative_to(config.repository_root) or source.is_relative_to(config.state_dir):
        raise ValueError('source must be outside managed repository/state')
    size = source.stat().st_size
    if not 0 < size <= MAX_BYTES:
        raise ValueError('文件为空或超过 200MB 上限')
    sha = digest(source)
    if sha != payload.get('expected_sha256'):
        raise ValueError('上传文件 SHA256 已变化')
    validate_signature(source, ext)
    journal = None
    try:
        with Store(config) as s:
            s.project_write = dict(actor=actor, action='project.file.add')
            with s.db:
                s.db.execute('BEGIN IMMEDIATE')
                p = project(s.db, pid, True)
                duplicate = s.db.execute('SELECT project_file_id FROM project_file WHERE project_id=? AND sha256=?', (pid, sha)).fetchone()
                if duplicate:
                    return dict(project_id=pid, project_file_id=duplicate[0], duplicate=True)
                fid = uuid.uuid4().hex
                record = dict(file_id=fid, project_code=p['project_code'], extension=ext[1:], sha256=sha)
                staged, final, relative = journal_paths(config, record)
                if staged.exists() or final.exists():
                    raise FileExistsError('project evidence path already exists; no file was changed')
                journal = config.state_dir / 'project-file-journal' / (fid + '.json')
                atomic_write(journal, json.dumps(record), immutable=True)
                staged.parent.mkdir(parents=True, exist_ok=True)
                with source.open('rb') as src, staged.open('xb') as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                    dst.flush()
                    os.fsync(dst.fileno())
                if staged.stat().st_size != size or digest(staged) != sha:
                    raise ValueError('project file changed during staging')
                final.parent.mkdir(parents=True, exist_ok=True)
                # On Windows rename refuses an existing target (no overwrite).
                os.rename(staged, final)
                s.db.execute('''INSERT INTO project_file(project_file_id,project_id,category,relative_path,filename,
                    extension,sha256,size_bytes,imported_at,imported_by) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (fid, pid, category, relative, filename, ext[1:], sha, size, now(), actor))
                s.db.execute('UPDATE project SET updated_at=? WHERE project_id=?', (now(), pid))
                audit(s.db, actor, 'project.file.add', 'project_file', fid,
                      dict(project_id=pid, filename=filename, category=category, relative_path=relative, sha256=sha, size_bytes=size))
        return dict(project_id=pid, project_file_id=fid, sha256=sha, duplicate=False)
    finally:
        if journal is not None:
            reconcile_files(config)


class Projects:
    def __init__(self, config):
        self.c = config

    @consistent_read
    def list(self, q='', status='active', page=1):
        if status not in ('active', 'archived'):
            raise ValueError('项目状态无效')
        q = text(q or '', '搜索词') or ''
        with Store(self.c, readonly=True) as s:
            where = "status=? AND (?='' OR instr(lower(name||' '||coalesce(tender_no,'')||' '||coalesce(tenderer,'')||' '||coalesce(lot_name,'')),lower(?))>0)"
            total = s.db.execute('SELECT count(*) FROM project WHERE ' + where, (status, q, q)).fetchone()[0]
            pages = max(1, (total + 19) // 20)
            page = max(1, min(int(page), pages))
            rows = s.db.execute('''SELECT p.*,
                (SELECT count(*) FROM project_file f WHERE f.project_id=p.project_id) AS file_count,
                (SELECT count(*) FROM project_material r WHERE r.project_id=p.project_id AND r.status='active') AS material_count
                FROM project p WHERE ''' + where + ' ORDER BY updated_at DESC,project_id DESC LIMIT 20 OFFSET ?',
                (status, q, q, (page - 1) * 20)).fetchall()
        return dict(items=[dict(r) for r in rows], total=total, page=page, pages=pages)

    @consistent_read
    def detail(self, project_id):
        with Store(self.c, readonly=True) as s:
            p = project(s.db, project_id)
            p['files'] = [dict(r) for r in s.db.execute('SELECT * FROM project_file WHERE project_id=? ORDER BY imported_at DESC', (project_id,))]
            p['references'] = [dict(r) for r in s.db.execute("SELECT * FROM project_material WHERE project_id=? AND status='active' ORDER BY added_at DESC", (project_id,))]
            p['audit'] = [dict(r) for r in s.db.execute('''SELECT * FROM audit_log WHERE
                (entity_type='project' AND entity_id=?) OR
                (entity_type IN ('project_file','project_material') AND json_extract(detail_json,'$.project_id')=?)
                ORDER BY log_id DESC LIMIT 50''', (project_id, project_id))]
        return p

    @consistent_read
    def reverse(self, material_id):
        with Store(self.c, readonly=True) as s:
            return [dict(r) for r in s.db.execute("""SELECT p.project_id,p.project_code,p.name,p.status FROM project p
                JOIN project_material r USING(project_id) WHERE r.material_id=? AND r.status='active'
                ORDER BY p.updated_at DESC,p.project_id DESC""", (material_id,))]

    @consistent_read
    def resolve_file(self, project_id, file_id):
        with Store(self.c, readonly=True) as s:
            row = s.db.execute('SELECT * FROM project_file WHERE project_id=? AND project_file_id=?', (project_id, file_id)).fetchone()
        if row is None:
            return None
        row = dict(row)
        path = safe_path(self.c.repository_root, row['relative_path'])
        if not path.is_file() or path.stat().st_size != row['size_bytes'] or digest(path) != row['sha256']:
            raise ValueError('项目文件校验失败，请运行系统体检')
        row['path'] = path
        return row
