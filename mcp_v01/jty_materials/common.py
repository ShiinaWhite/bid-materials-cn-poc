import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import tempfile
import unicodedata
import urllib.parse
import msvcrt
import functools


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def digest(path):
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def atomic_write(path, data, *, immutable=False):
    """Same-volume temporary write, flush, then publish. Never rewrite evidence."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd, name = tempfile.mkstemp(prefix=".pending-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if immutable:
            # Windows rename refuses an existing target; unlike replace, no clobber.
            if os.name == "nt":
                os.rename(name, path)
            else:
                os.link(name, path)
                os.unlink(name)
        else:
            os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def normalize(text):
    return unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")


def safe_path(root, relative):
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise ValueError("invalid relative path")
    parts = relative.split("/")
    if ("\\" in relative or ":" in relative or PureWindowsPath(relative).drive
            or any(p in ("", ".", "..") or p.endswith((" ", ".")) for p in parts)):
        raise ValueError("only canonical relative paths are allowed")
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("path escapes repository (including symlinks/junctions)")
    return path


class Config:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.base = self.path.parent
        for key in ("repository_root", "index_dir", "dowse_exe", "umi_exe", "logs_dir", "db_path", "state_dir", "backup_dir"):
            if not isinstance(self.raw.get(key), str) or not self.raw[key]:
                raise ValueError("missing configuration path: " + key)
            setattr(self, key, (self.base / self.raw[key]).resolve())
        self.materials = self.repository_root / "materials"
        self.umi_url = self.raw["umi_url"]
        url = urllib.parse.urlparse(self.umi_url)
        if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port or url.path not in ("", "/"):
            raise ValueError("umi_url must be an HTTP loopback address with an explicit port")
        if not self.db_path.is_relative_to(self.repository_root):
            raise ValueError("database must be inside repository_root")
        for p in (self.index_dir, self.state_dir, self.backup_dir, self.logs_dir):
            if p.is_relative_to(self.materials):
                raise ValueError("runtime paths must not be inside searchable materials")
        if self.backup_dir.is_relative_to(self.repository_root):
            raise ValueError("backups must be outside the repository")
        self.maintenance_file = self.state_dir / "maintenance.json"

    def log(self, event, **data):
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        with (self.logs_dir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": now(), "event": event, **data}, ensure_ascii=False) + "\n")

    def assert_readable(self):
        if self.maintenance_file.exists():
            raise RuntimeError("MAINTENANCE: offline operation in progress or interrupted; run doctor/reindex")

    def generation(self):
        path = self.state_dir / "generation.json"
        return json.loads(path.read_text()) if path.exists() else 0


def consistent_read(function):
    @functools.wraps(function)
    def read(self, *args, **kwargs):
        self.c.assert_readable()
        before = self.c.generation()
        self.c.assert_readable()
        result = function(self, *args, **kwargs)
        self.c.assert_readable()
        if before != self.c.generation():
            raise RuntimeError("RETRY: repository changed during query")
        return result
    return read


class FileLock:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = path.open("a+b")
        self.f.seek(0)
        if not self.f.read(1):
            self.f.write(b"0"); self.f.flush()
        self.f.seek(0)
        try:
            msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
        except BaseException:
            self.f.close()
            raise RuntimeError("another operation holds the repository lock")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.f.seek(0)
        msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
        self.f.close()


def snapshot(project):
    result = {}
    for folder in ("poc", "data", "design", "runtime", "docs", "evidence"):
        for p in (Path(project) / folder).rglob("*"):
            if p.is_file():
                st = p.stat()
                result[p.relative_to(project).as_posix()] = [st.st_size, st.st_mtime_ns, digest(p)]
    return result
