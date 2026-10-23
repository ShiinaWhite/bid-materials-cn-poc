"""Consistent repository backups. Index and runtime state are rebuildable."""
import datetime as dt
import json
from pathlib import Path
import shutil
import sqlite3
import uuid
from contextlib import closing

from .common import Config, atomic_write, digest, now, safe_path
from .store import Store, SCHEMA_VERSION


def create_backup(config,name=None):
    if name is not None and (not isinstance(name,str) or not __import__('re').fullmatch(r'[A-Za-z0-9_-]{1,100}',name)):raise ValueError('invalid backup name')
    target = config.backup_dir / (name or (dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]))
    target.mkdir(parents=True, exist_ok=False)
    with Store(config, readonly=True) as store:
        db_target = target / "database.db"
        with closing(sqlite3.connect(db_target)) as destination:
            store.db.backup(destination)
        files = dict(store.db.execute("SELECT relative_path,sha256 FROM material_file"))
        files.update(dict(store.db.execute("SELECT relative_path,sha256 FROM project_file")))
        files.update(dict(store.db.execute("SELECT relative_path,sha256 FROM search_projection")))
        batches=config.repository_root/'batches'
        if batches.exists():
            for p in batches.rglob('*'):
                if p.is_file() and p.suffix in ('.json','.jsonl'):
                    relative=p.relative_to(config.repository_root).as_posix();safe_path(config.repository_root,relative)
                    files[relative]=digest(p)
        manifest = []
        for relative in sorted(files):
            source = safe_path(config.repository_root, relative)
            if digest(source) != files[relative]:
                raise ValueError("registered evidence/projection checksum mismatch; backup not completed")
            dest = safe_path(target / "files", relative)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
            if digest(source) != digest(dest):
                raise ValueError("file changed during backup")
            manifest.append({"relative_path": relative, "sha256": digest(dest), "size": dest.stat().st_size})
    atomic_write(target / "config.json", config.path.read_bytes(), immutable=True)
    info = {"format": "jty-backup-0.1", "at": now(), "schema": SCHEMA_VERSION,
            "db_sha256": digest(db_target), "config_sha256": digest(target / "config.json"),
            "runtime": {"dowse_exe": str(config.dowse_exe), "umi_exe": str(config.umi_exe)}, "files": manifest}
    atomic_write(target / "manifest.json", json.dumps(info, ensure_ascii=False, indent=2), immutable=True)
    # The completion marker is published last; partial backups are not restorable.
    atomic_write(target / "COMPLETE", digest(target / "manifest.json"), immutable=True)
    config.log("backup.complete", target=str(target), files=len(manifest))
    return {"backup": str(target), "files": len(manifest), "database_sha256": info["db_sha256"]}


def restore_backup(backup, target, runtime_root=None):
    backup = Path(backup).resolve(strict=True); target = Path(target).resolve()
    if target.exists() or backup.is_relative_to(target):
        raise ValueError("restore target must not exist and must not contain the backup")
    if (backup / "COMPLETE").read_text() != digest(backup / "manifest.json"):
        raise ValueError("incomplete or altered backup manifest")
    info = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    if info.get("format") != "jty-backup-0.1" or info.get("schema") not in ('jty-mcp-0.1.0','jty-mcp-0.2.0','jty-mcp-0.3.0','jty-mcp-0.4.0',SCHEMA_VERSION):
        raise ValueError("unsupported backup/schema")
    if digest(backup / "database.db") != info["db_sha256"] or digest(backup / "config.json") != info["config_sha256"]:
        raise ValueError("backup database/config checksum mismatch")
    for item in info["files"]:
        source = safe_path(backup / "files", item["relative_path"])
        if digest(source) != item["sha256"] or source.stat().st_size != item["size"]:
            raise ValueError("backup payload checksum mismatch")
    with closing(sqlite3.connect((backup / "database.db").as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("backup database integrity failure")
    target.mkdir(parents=True, exist_ok=False)
    repository = target / "repository"
    (repository / "db").mkdir(parents=True)
    shutil.copy2(backup / "database.db", repository / "db/materials.db")
    for item in info["files"]:
        dest = safe_path(repository, item["relative_path"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(safe_path(backup / "files", item["relative_path"]), dest)
    runtime = info["runtime"]
    umi = Path(runtime_root) / "umi/Umi-OCR.exe" if runtime_root else Path(runtime["umi_exe"])
    dowse = Path(runtime_root) / "dowse/dowse.exe" if runtime_root else Path(runtime["dowse_exe"])
    from .setup import copy_runtime
    url = copy_runtime(umi, dowse, target / "runtime")
    raw = json.loads((backup / "config.json").read_text(encoding="utf-8"))
    raw.update(repository_root="repository", db_path="repository/db/materials.db", index_dir="index/dowse-index",
               state_dir="state", logs_dir="logs", backup_dir="backups", umi_exe="runtime/umi/Umi-OCR.exe",
               dowse_exe="runtime/dowse/dowse.exe", umi_url=url)
    atomic_write(target / "config.json", json.dumps(raw, ensure_ascii=False, indent=2), immutable=True)
    Store.migrate(Config(target/'config.json'))
    return {"config": str(target / "config.json"), "index_rebuild_required": True}
