"""User feedback intake (v0.4.2). Deliberately separate from the materials
library: feedback never enters the repository, the database, OCR, the dowse
index, verification or audit_log. Storage is a plain directory of
human/agent-readable folders plus an index.jsonl for fast scanning:

    feedback/
      index.jsonl
      FB-YYYYMMDD-NNNN/
        feedback.json
        feedback.md
        attachments/screenshot-01.png

Security: client filenames are never used as paths (system-generated safe
names instead), attachment types are pinned by magic bytes, sizes are capped
while streaming, and serving goes through a strict-ID route only.
"""
import datetime as dt
import hashlib
import json
import re
import shutil
import tempfile
import threading
import uuid
from pathlib import Path

from jty_materials.common import atomic_write, now

from . import WEBUI_VERSION

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS = 5
MAX_BODY_BYTES = MAX_ATTACHMENTS * MAX_ATTACHMENT_BYTES + 2 * 1024 * 1024
MAX_CONTENT_CHARS = 5000
MAX_ACTOR_CHARS = 100
MAX_META_CHARS = 500

STATUS_LABELS = {"open": "待处理", "resolved": "已处理", "wont_fix": "暂不处理"}
CONTENT_TYPES = {".png": "image/png", ".jpg": "image/jpeg",
                 ".jpeg": "image/jpeg", ".webp": "image/webp"}

_FB_ID_RE = re.compile(r"^FB-(\d{8})-(\d{4})$")
_SAFE_NAME_RE = re.compile(r"^screenshot-\d{2,}\.(png|jpg|jpeg|webp)$")


class FeedbackError(ValueError):
    pass


def _sniff_image(head: bytes):
    """Canonical extension by magic bytes, or None when not a supported image."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def _clean_meta(value, limit):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value or "")).strip()[:limit]


def _clean_original_name(name, limit=120):
    """Kept for reference only — never used to build a path."""
    name = re.split(r"[\\/]", str(name or ""))[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return name[:limit] or "未命名"


# ----------------------------------------------------- multipart parsing ----

class _FieldBuf:
    def __init__(self, cap=1024 * 1024):
        self.parts = []
        self.cap = cap

    def append(self, data):
        if sum(len(p) for p in self.parts) + len(data) > self.cap:
            raise FeedbackError("表单字段过大")
        self.parts.append(data)

    def text(self):
        return b"".join(self.parts).decode("utf-8", "replace")


class _AttachmentWriter:
    """Streams one attachment to a temp file, enforcing size and image type."""

    def __init__(self, target: Path, max_bytes, meta):
        self.path = target
        self.max_bytes = max_bytes
        self.meta = meta
        self.written = 0
        self.head = b""
        self.sha = hashlib.sha256()
        self._fh = None

    def write(self, chunk: bytes):
        self.written += len(chunk)
        if self.written > self.max_bytes:
            raise FeedbackError("单个截图超过 20MB 上限")
        if len(self.head) < 16:
            self.head = (self.head + chunk)[:16]
        self.sha.update(chunk)
        if self._fh is None:
            self._fh = self.path.open("wb")
        self._fh.write(chunk)

    def finish(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        if _sniff_image(self.head) is None:
            raise FeedbackError("只支持 PNG / JPG / WEBP 图片截图")
        return {"size": self.written, "sha256": self.sha.hexdigest()}

    def abort(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        self.path.unlink(missing_ok=True)


class _SkippedPart:
    """A file part we intentionally ignore (e.g. filename="")."""

    def __init__(self, meta):
        self.meta = meta
        self.path = None

    def write(self, chunk):
        pass

    def finish(self):
        return {}

    def abort(self):
        pass


def parse_multipart(rfile, content_type, content_length, *,
                    max_body=MAX_BODY_BYTES, max_file=MAX_ATTACHMENT_BYTES):
    """Minimal streaming multipart/form-data parser (stdlib only).

    Returns (fields: dict[str, str], files: list[dict]); each file dict has
    path / field / original_name and, for kept attachments, size and sha256.
    Callers own the temp files and must delete them.
    """
    m = re.search(r'boundary="?([^";,]+)"?', content_type or "", re.I)
    if not m:
        raise FeedbackError("请求缺少 multipart boundary")
    if not isinstance(content_length, int) or content_length <= 0:
        raise FeedbackError("请求缺少 Content-Length")
    if content_length > max_body:
        raise FeedbackError("请求体超过上限（单张截图 20MB，最多 5 张）")
    boundary = m.group(1).encode("utf-8")
    delim = b"\r\n--" + boundary
    lead = b"--" + boundary
    holdback = len(delim) - 1
    fields, files = {}, []
    buf = b""
    remaining = content_length

    def read_more():
        nonlocal remaining, buf
        if remaining <= 0:
            return False
        chunk = rfile.read(min(64 * 1024, remaining))
        if not chunk:
            return False
        remaining -= len(chunk)
        buf += chunk
        return True

    sink = None       # _FieldBuf | _AttachmentWriter | _SkippedPart
    field_name = ""
    in_headers = True

    def keep_sink(data):
        if sink is not None and data:
            if isinstance(sink, _FieldBuf):
                sink.append(data)
            else:
                sink.write(data)

    try:
        while not buf.startswith(lead):
            if len(buf) >= len(lead) or not read_more():
                raise FeedbackError("请求体格式无效")
        buf = buf[len(lead):]
        if buf.startswith(b"\r\n"):  # opening delimiter line terminator
            buf = buf[2:]
        while True:
            if in_headers:
                idx = buf.find(b"\r\n\r\n")
                while idx == -1:
                    if len(buf) > 65536 or not read_more():
                        raise FeedbackError("请求体格式无效")
                    idx = buf.find(b"\r\n\r\n")
                headers = buf[:idx].decode("utf-8", "replace")
                buf = buf[idx + 4:]
                disp = next((h for h in headers.split("\r\n")
                             if h.lower().startswith("content-disposition:")), "")
                name_m = re.search(r'name="([^"]*)"', disp)
                file_m = re.search(r'filename="([^"]*)"', disp)
                field_name = name_m.group(1) if name_m else ""
                in_headers = False
                if file_m:
                    meta = {"field": field_name,
                            "original_name": _clean_original_name(file_m.group(1))}
                    if meta["original_name"] == "未命名":
                        sink = _SkippedPart(meta)
                    else:
                        tmp = Path(tempfile.gettempdir()) / ("fb-" + uuid.uuid4().hex)
                        sink = _AttachmentWriter(tmp, max_file, meta)
                else:
                    sink = _FieldBuf()
                    fields[field_name] = sink
            # data phase: scan for the next delimiter, holding back a
            # possible partial match at the buffer tail
            while True:
                idx = buf.find(delim)
                if idx != -1:
                    data, buf = buf[:idx], buf[idx + len(delim):]
                    keep_sink(data)
                    while len(buf) < 2:
                        if not read_more():
                            raise FeedbackError("请求体被截断")
                    tail, buf = buf[:2], buf[2:]
                    if tail not in (b"--", b"\r\n"):
                        raise FeedbackError("请求体格式无效")
                    if isinstance(sink, _AttachmentWriter):
                        info = sink.finish()
                        files.append({**sink.meta, "path": str(sink.path), **info})
                    elif isinstance(sink, _FieldBuf):
                        fields[field_name] = sink.text()
                    sink = None
                    if tail == b"--":
                        return fields, files
                    in_headers = True
                    break
                keep = max(0, len(buf) - holdback)
                data, buf = buf[:keep], buf[keep:]
                keep_sink(data)
                if not read_more():
                    raise FeedbackError("请求体缺少结束边界")
    except BaseException:
        if isinstance(sink, _AttachmentWriter):
            sink.abort()
        for done in files:  # caller never sees these on failure
            Path(done["path"]).unlink(missing_ok=True)
        raise


# ------------------------------------------------------------- storage ----

def store_for(config):
    """The feedback directory lives at the project root, outside both the
    repository and the state directories."""
    return FeedbackStore(config.base.parent / "feedback")


class FeedbackStore:
    """Append-only feedback storage rooted at its own directory."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.lock = threading.Lock()

    def submit(self, *, content, actor="", page_path="", page_title="",
               page_url="", user_agent="", attachments=()):
        """attachments: dicts {path, original_name}; original bytes are kept."""
        content = (content or "").strip()
        if not content:
            raise FeedbackError("反馈内容不能为空")
        if len(content) > MAX_CONTENT_CHARS:
            raise FeedbackError("反馈内容过长（最多 5000 字）")
        actor = _clean_meta(actor, MAX_ACTOR_CHARS)
        page_path = _clean_meta(page_path, MAX_META_CHARS)
        page_title = _clean_meta(page_title, 300)
        page_url = _clean_meta(page_url, MAX_META_CHARS)
        user_agent = _clean_meta(user_agent, 300)
        atts = list(attachments or [])
        if len(atts) > MAX_ATTACHMENTS:
            raise FeedbackError("每条反馈最多 5 个附件")
        material_id = None
        m = re.search(r"^/material/(\d+)", page_path)
        if m:
            material_id = int(m.group(1))
        with self.lock:
            feedback_id = self._allocate_id()
            folder = self.root / feedback_id
            att_dir = folder / "attachments"
            created_at = now()
            try:
                att_dir.mkdir(parents=True)
                saved = []
                for i, att in enumerate(atts, 1):
                    src = Path(att["path"])
                    with src.open("rb") as f:
                        head = f.read(16)
                        f.seek(0)
                        ext = _sniff_image(head)
                        if ext is None:
                            raise FeedbackError("只支持 PNG / JPG / WEBP 图片截图")
                        target = att_dir / f"screenshot-{i:02d}.{ext}"
                        sha = hashlib.sha256()
                        size = 0
                        with target.open("wb") as out:
                            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                                out.write(chunk)  # original bytes, never recompressed
                                sha.update(chunk)
                                size += len(chunk)
                    saved.append({"name": target.name,
                                  "original_name": _clean_original_name(att.get("original_name")),
                                  "size": size, "sha256": sha.hexdigest()})
                record = {
                    "feedback_id": feedback_id,
                    "status": "open",
                    "created_at": created_at,
                    "webui_version": WEBUI_VERSION,
                    "page_path": page_path,
                    "page_url": page_url,
                    "page_title": page_title,
                    "material_id": material_id,
                    "actor": actor,
                    "content": content,
                    "attachments": saved,
                    "user_agent": user_agent,
                }
                atomic_write(folder / "feedback.json",
                             json.dumps(record, ensure_ascii=False, indent=2) + "\n")
                atomic_write(folder / "feedback.md", render_md(record) + "\n")
                with (self.root / "index.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "feedback_id": feedback_id,
                        "created_at": created_at,
                        "status": "open",
                        "actor": actor,
                        "summary": content.splitlines()[0][:80],
                        "page_path": page_path,
                        "page_title": page_title,
                        "material_id": material_id,
                        "attachment_count": len(saved),
                        "attachments": [a["name"] for a in saved],
                        "webui_version": WEBUI_VERSION,
                    }, ensure_ascii=False) + "\n")
            except BaseException:
                shutil.rmtree(folder, ignore_errors=True)
                raise
        return record

    def _allocate_id(self):
        self.root.mkdir(parents=True, exist_ok=True)
        today = dt.datetime.now().strftime("%Y%m%d")
        top = 0
        for entry in self.root.iterdir():
            m = _FB_ID_RE.match(entry.name)
            if m and m.group(1) == today:
                top = max(top, int(m.group(2)))
        return f"FB-{today}-{top + 1:04d}"

    def index_entries(self):
        path = self.root / "index.jsonl"
        if not path.is_file():
            return []
        entries = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return entries

    def load_record(self, feedback_id):
        if not _FB_ID_RE.match(feedback_id):
            raise FeedbackError("反馈编号无效")
        path = self.root / feedback_id / "feedback.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None

    def attachment_path(self, feedback_id, name):
        """Resolve an attachment for serving; refuses anything outside this
        feedback's own attachments directory."""
        if not _FB_ID_RE.match(feedback_id):
            raise FeedbackError("反馈编号无效")
        if not _SAFE_NAME_RE.match(name):
            raise FeedbackError("附件名称无效")
        root = self.root.resolve()
        att_root = (root / feedback_id / "attachments").resolve()
        path = (att_root / name).resolve()
        if path.parent != att_root or not path.is_relative_to(root):
            raise FeedbackError("附件路径无效")
        return path if path.is_file() else None


def render_md(record):
    """Agent/human-readable companion; internal JSON fields stay out."""
    title = re.sub(r"\s*·\s*投标素材库\s*$", "", record.get("page_title") or "")
    status = record.get("status") or "open"
    lines = [
        f"# {record['feedback_id']}",
        "",
        f"状态：{STATUS_LABELS.get(status, status)}（{status}）",
        f"时间：{(record.get('created_at') or '')[:16].replace('T', ' ')}",
        f"经办人：{record.get('actor') or '（未填写）'}",
        "",
        f"页面：{record.get('page_path') or '（未记录）'}",
        f"页面标题：{title or '（未记录）'}",
        "",
        "## 反馈",
        "",
        record.get("content", ""),
        "",
    ]
    atts = record.get("attachments") or []
    if atts:
        lines += ["## 附件", ""]
        lines += [f"- attachments/{a['name']}" for a in atts]
        lines.append("")
    return "\n".join(lines)
