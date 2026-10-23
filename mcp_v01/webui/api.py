"""JSON API endpoints backing the interactive parts of the UI."""
import re
import time
from pathlib import Path

from jty_materials.query import Query

from . import feedback as feedback_module
from . import pages, review, service, uploads


def _job_dict(job):
    d = job.to_dict()
    if d["result"] and d["result"].get("material_id"):
        d["material_url"] = f"/material/{d['result']['material_id']}"
    return d


def _header_text(value):
    """http headers are parsed as latin-1; browsers send UTF-8 for XHR headers."""
    if value is None:
        return ""
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


class Api:
    def __init__(self, config):
        self.c = config
        self.uploader = uploads.UploadManager(config)
        self.doctor = DoctorRunner(config)
        uploads.purge_stale_staging(config)

    # GET /api/jobs/<id>
    def job(self, job_id):
        job = self.uploader.get(job_id)
        if job is None:
            return 404, {"error": "任务不存在"}
        return 200, _job_dict(job)

    # POST /api/upload  (raw body; metadata in query params)
    def upload(self, request):
        q = request.query
        mode = q.get("mode", "import")
        if mode not in ("import", "replace"):
            return 400, {"error": "上传模式无效"}
        filename = re.split(r"[\\/]", q.get("filename", ""))[-1].strip() or "upload"
        if Path(filename).suffix.lower() not in pages.catalog.UPLOAD_EXTS:
            return 400, {"error": pages.catalog.UPLOAD_FORMAT_ERROR}
        actor = (q.get("actor") or "").strip()
        subtype = (q.get("subtype") or "").strip()
        title = (q.get("title") or "").strip()
        material_id = (q.get("material") or "").strip()
        try:
            expected = int(request.headers.get("Content-Length", ""))
        except ValueError:
            return 411, {"error": "缺少 Content-Length"}
        if not actor:
            return 400, {"error": "缺少操作人（将写入审计记录）"}
        if mode == "import":
            if subtype not in pages.catalog.UPLOAD_TYPES:
                return 400, {"error": "请选择有效的材料类别"}
            if not title:
                return 400, {"error": "请填写材料标题"}
        else:
            if not re.fullmatch(r"\d+", material_id):
                return 400, {"error": "材料编号无效"}
        try:
            staged, sha, size = uploads.stage_body(self.c, request.rfile, expected)
        except ValueError as e:
            return 413 if "200MB" in str(e) else 400, {"error": str(e)}
        params = {"actor": actor, "subtype": subtype, "title": title, "material_id": material_id}
        job = self.uploader.submit(mode, staged, filename, size, params)
        self.c.log("webui.upload_received", job=job.id, mode=mode, filename=filename,
                   size=size, sha256=sha, actor=actor, subtype=subtype or None,
                   material_id=material_id or None)
        return 202, _job_dict(job)

    # GET /api/material/<id>/text
    def material_text(self, material_id):
        try:
            result = service.ocr_text(self.c, int(material_id))
        except ValueError:
            return 404, {"error": "材料不存在"}
        return 200, result

    # POST /api/review
    def submit_review(self, request):
        payload = request.json()
        if not isinstance(payload, dict):
            return 400, {"error": "请求格式无效"}
        try:
            result = review.submit_review(self.c, payload)
        except ValueError as e:
            return 400, {"error": str(e)}
        except RuntimeError as e:
            return 503, {"error": str(e), "retry": True}
        return 200, {"ok": True, "result": result}

    # POST /api/feedback (multipart: content, actor, page_*, attachment=…)
    def submit_feedback(self, request):
        try:
            length = int(request.headers.get("Content-Length", ""))
        except ValueError:
            return 411, {"error": "缺少 Content-Length"}
        store = feedback_module.store_for(self.c)
        files = []
        try:
            fields, files = feedback_module.parse_multipart(
                request.rfile, request.headers.get("Content-Type"), length)
            record = store.submit(
                content=fields.get("content", ""),
                actor=fields.get("actor", ""),
                page_path=fields.get("page_path", ""),
                page_title=fields.get("page_title", ""),
                page_url=fields.get("page_url", ""),
                user_agent=_header_text(request.headers.get("User-Agent")),
                attachments=[{"path": f["path"], "original_name": f["original_name"]}
                             for f in files])
            self.c.log("webui.feedback_submitted", feedback_id=record["feedback_id"],
                       page_path=record["page_path"], material_id=record["material_id"],
                       actor=record["actor"] or None, attachments=len(files))
            return 200, {"ok": True, "feedback_id": record["feedback_id"]}
        except feedback_module.FeedbackError as e:
            return 400, {"error": str(e)}
        finally:
            for f in files:
                Path(f["path"]).unlink(missing_ok=True)

    # GET /api/doctor  POST /api/doctor
    def doctor_status(self):
        state = self.doctor.snapshot()
        return 200, state

    def doctor_start(self):
        return 202, self.doctor.start()


class DoctorRunner:
    """Deep integrity check (same checks as offline doctor) in a worker thread."""

    def __init__(self, config):
        self.c = config
        self.locked = False
        self.result = None
        self.finished_at = None

    def snapshot(self):
        if self.locked:
            return {"state": "running"}
        if self.result is not None:
            return {"state": "done", "finished_at": self.finished_at, "result": self.result}
        return {"state": "idle"}

    def start(self):
        if self.locked:
            return {"state": "running"}
        self.locked = True
        self.result = None
        import threading
        threading.Thread(target=self._run, daemon=True).start()
        return {"state": "running"}

    def _run(self):
        started = time.time()
        try:
            self.result = self._check()
        except Exception as error:
            self.result = {"error": str(error)}
        self.finished_at = time.time()
        self.locked = False
        self.c.log("webui.doctor", elapsed=round(self.finished_at - started, 1),
                   ok="error" not in (self.result or {}))

    def _check(self):
        import os
        from jty_materials.common import digest, safe_path
        from jty_materials.rpc import Rpc
        from jty_materials.store import Store
        from jty_materials.manager import request
        with Store(self.c, readonly=True) as store:
            integrity = store.db.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = len(store.db.execute("PRAGMA foreign_key_check").fetchall())
            from jty_materials.doctor import check_files
            material_files = check_files(self.c, store.db, 'material_file')
            project_files = check_files(self.c, store.db, 'project_file')
            projection_files = check_files(self.c, store.db, 'search_projection')
        with Rpc([str(self.c.dowse_exe), "mcp"], env=dict(os.environ, DOWSE_INDEX_DIR=str(self.c.index_dir))) as rpc:
            index = rpc.tool("index_status", {})
        return {"integrity": integrity, "foreign_key_errors": foreign_keys,
                "files_checked": material_files['checked'], "file_errors": len(material_files['errors']),
                "project_files_checked": project_files['checked'], "project_file_errors": len(project_files['errors']),
                "projections_checked": projection_files["checked"], "projection_errors": len(projection_files["errors"]),
                "index_docs": index.get("num_docs", "—"),
                "manager": request(self.c, "status", start=False)}
