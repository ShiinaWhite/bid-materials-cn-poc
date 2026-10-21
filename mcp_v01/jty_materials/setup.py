import json
from pathlib import Path
import shutil
import socket

from .common import Config, atomic_write
from .store import Store


def copy_runtime(umi_exe, dowse_exe, target):
    target = Path(target).resolve()
    if target.exists():
        raise FileExistsError("runtime target already exists")
    target.mkdir(parents=True, exist_ok=False)
    shutil.copytree(Path(umi_exe).resolve().parent, target / "umi")
    (target / "dowse").mkdir()
    shutil.copy2(dowse_exe, target / "dowse/dowse.exe")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    data = target / "umi/UmiOCR-data"
    settings = (data / ".settings").read_text(encoding="utf-8")
    import re
    settings = re.sub(r"(?m)^server.port=.*$", f"server.port={port}", settings)
    settings = settings.replace("window.startupInvisible=false", "window.startupInvisible=true")
    settings = settings.replace("window.closeWin2Hide=true", "window.closeWin2Hide=false")
    atomic_write(data / ".settings", settings)
    pre = json.loads((data / ".pre_settings").read_text(encoding="utf-8"))
    pre.update(server_port=port, last_pid=0, last_ptime="")
    atomic_write(data / ".pre_settings", json.dumps(pre))
    return f"http://127.0.0.1:{port}"


def initialize(config_path, *, umi_exe, dowse_exe, runtime_target, repository, fixture_only=False):
    config_path = Path(config_path).resolve(); repository = Path(repository).resolve()
    if config_path.exists() or repository.exists():
        raise FileExistsError("configuration and repository must both be new")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    url = copy_runtime(umi_exe, dowse_exe, runtime_target)
    import os
    def rel(p):
        return Path(os.path.relpath(Path(p).resolve(), config_path.parent)).as_posix()
    local = Path(runtime_target).resolve().parent
    raw = {"config_version": 1, "repository_root": rel(repository),
           "db_path": rel(repository / "db/materials.db"), "index_dir": rel(local / "index/dowse-index"),
           "umi_exe": rel(Path(runtime_target) / "umi/Umi-OCR.exe"), "dowse_exe": rel(Path(runtime_target) / "dowse/dowse.exe"),
           "state_dir": rel(local / "state"), "logs_dir": rel(local / "logs"), "backup_dir": rel(local / "backups"),
           "umi_url": url, "fixture_only": fixture_only}
    atomic_write(config_path, json.dumps(raw, ensure_ascii=False, indent=2), immutable=True)
    config = Config(config_path)
    config.materials.mkdir(parents=True)
    Store.initialize(config)
    return {"config": str(config_path), "repository": str(repository)}
