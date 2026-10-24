"""Web upload pipeline. The browser file is staged OUTSIDE the repository and
state dirs; the actual import is the one serialized manager RPC used by the
offline CLI (staging -> SHA -> duplicate handling -> OCR -> version registration
-> projection/index rebuild -> audit). This module adds no write semantics.
"""
import datetime as dt
import hashlib
import re
import threading
import time
import uuid
from pathlib import Path

from jty_materials.manager import request
from jty_materials.store import Store

from . import catalog

MAX_UPLOAD_BYTES = 200 * 1024 * 1024


def source_context(filename, title):
    """Unverified display hints only; never accept browser paths/metadata."""
    filename = re.split(r'[\\/]', filename)[-1].strip()
    filename = ''.join(c for c in filename if ord(c) >= 32)[:240] or 'upload'
    title = (title or '').strip()
    fallback = Path(filename).stem.strip() or '上传资料'
    return dict(source_origin='webui', source_filename=filename,
                source_relative_path=filename, source_title=title or fallback,
                title_origin='user' if title else 'filename_fallback')


def staging_root(config):
    # Project root/local/uploads: outside repository_root and state_dir, as
    # import_material requires sources to be outside both.
    path = config.base.parent / "local" / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def purge_stale_staging(config, max_age_hours=24):
    cutoff = time.time() - max_age_hours * 3600
    for p in staging_root(config).glob("tmp-*"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def stage_body(config, reader, expected_size):
    """Stream the request body to a private staging file; returns (path, sha256, size)."""
    if expected_size is None:
        raise ValueError("Content-Length is required")
    if expected_size > MAX_UPLOAD_BYTES:
        raise ValueError("文件超过 200MB 上限")
    root = staging_root(config)
    tmp = root / ("tmp-" + uuid.uuid4().hex)
    sha = hashlib.sha256()
    size = 0
    try:
        with tmp.open("wb") as f:
            remaining = expected_size
            while remaining > 0:
                chunk = reader.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("请求体比 Content-Length 短")
                size += len(chunk)
                sha.update(chunk)
                f.write(chunk)
                remaining -= len(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp, sha.hexdigest(), size


def find_duplicate_original(config, sha256):
    with Store(config, readonly=True) as s:
        return s.db.execute("""SELECT f.material_id, f.file_id, m.material_code, m.title
            FROM material_file f JOIN material m USING(material_id)
            WHERE f.role='original' AND f.sha256=? ORDER BY f.file_id LIMIT 1""", (sha256,)).fetchone()


class UploadJob:
    def __init__(self, job_id, mode, filename, size, params):
        self.id = job_id
        self.mode = mode  # import | replace | reextract
        self.filename = filename
        self.size = size
        self.params = params
        self.state = "queued"
        self.step = "排队等待"
        self.result = None
        self.error = None
        self.created_at = time.time()
        self.finished_at = None

    def to_dict(self):
        return {"job_id": self.id, "mode": self.mode, "state": self.state, "step": self.step,
                "filename": self.filename, "size": self.size, "params": self.params,
                "result": self.result, "error": self.error,
                "elapsed": round((self.finished_at or time.time()) - self.created_at, 1)}


class UploadManager:
    def __init__(self, config):
        self.c = config
        self.lock = threading.Lock()
        self.sem = threading.Semaphore(2)
        self.jobs = {}

    def submit(self, mode, staged_path, filename, size, params):
        job_id = uuid.uuid4().hex[:12]
        job = UploadJob(job_id, mode, filename, size, params)
        with self.lock:
            self.jobs[job_id] = job
            if len(self.jobs) > 100:
                for old in sorted(self.jobs.values(), key=lambda j: j.created_at)[:-50]:
                    if old.finished_at and time.time() - old.finished_at > 3600:
                        self.jobs.pop(old.id, None)
        threading.Thread(target=self._run, args=(job, staged_path), daemon=True).start()
        return job

    def get(self, job_id):
        with self.lock:
            return self.jobs.get(job_id)

    def _run(self, job, staged_path):
        with self.sem:
            try:
                job.state = "running"
                if job.mode == 'reextract':
                    job.step = '正在重新识别当前文件'
                    receipt = request(self.c, 'reextract_current',
                                      dict(material_id=int(job.params['material_id']), actor=job.params['actor']), timeout=1800)
                    job.result = dict(material_id=receipt['material_id'], receipt=receipt)
                    job.state = 'done'; job.step = '完成'
                    return
                source = Path(staged_path)
                suffix = source.suffix.lower() if source.suffix else Path(job.filename).suffix.lower()
                if suffix not in catalog.UPLOAD_EXTS:
                    raise ValueError(catalog.UPLOAD_FORMAT_ERROR)
                # import_material copies to staging under a fixed name; give it the
                # real extension by staging a hardlink-like renamed copy.
                renamed = source.with_name(source.stem + suffix)
                if renamed != source:
                    source.rename(renamed)
                    source = renamed
                job.step = "正在核对资料"
                if job.mode == "import":
                    dup = find_duplicate_original(self.c, _sha(source))
                    if dup is not None:
                        job.state = "done"
                        job.step = "完成"
                        job.result = {"duplicate": True, "material_id": dup["material_id"],
                                      "material_code": dup["material_code"], "title": dup["title"],
                                      "note": "这份文件已经保存过，为避免重复未再次保存。"}
                        return
                job.step = "正在识别资料并保存"
                payload = {"source": str(source), "actor": job.params["actor"]}
                context = source_context(job.filename, job.params.get('title'))
                payload['source_metadata'] = context
                if job.mode == "import":
                    payload.update(kind="material", subtype=job.params["subtype"] or None,
                                   title=(str(job.params.get("title") or "").strip() or context['source_title']))
                else:
                    payload["material_id"] = int(job.params["material_id"])
                    context['title_origin'] = 'preserved'
                receipt = request(self.c, job.mode, payload, timeout=1800)
                job.state = "done"
                job.step = "完成"
                job.result = {"duplicate": False, "receipt": receipt,
                              "material_id": receipt.get("material_id"),
                              "material_code": receipt.get("material_code"),
                              "unchanged": bool(receipt.get("unchanged")),
                              "requires_human_review": bool(receipt.get("requires_human_review"))}
            except Exception as error:
                job.state = "error"
                job.step = "处理失败"
                job.error = str(error)
            finally:
                job.finished_at = time.time()
                try:
                    if staged_path is not None:
                        Path(staged_path).unlink(missing_ok=True)
                    if 'source' in locals():
                        source.unlink(missing_ok=True)
                except OSError:
                    pass
                self.c.log("webui.upload_job", job=job.id, mode=job.mode, state=job.state,
                           material_id=(job.result or {}).get("material_id"), error=job.error)


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
