import msvcrt
import os
from pathlib import Path
import subprocess
import time
import uuid

from .common import atomic_write, normalize
from .rpc import Rpc
from .windows import owned_processes, stop_console


class Dowse:
    def __init__(self, config):
        self.c = config
        self.watch = None
        self.watch_log = None
        self.env = dict(os.environ, DOWSE_INDEX_DIR=str(config.index_dir))
        config.index_dir.parent.mkdir(parents=True, exist_ok=True)
        self.lock = (config.index_dir.parent / "lifecycle.lock").open("a+b")
        self.lock.seek(0)
        if not self.lock.read(1):
            self.lock.write(b"0"); self.lock.flush()
        self.lock.seek(0)
        msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)

    def command(self, action, *args):
        if action not in ("status", "index", "add"):
            raise ValueError("unsupported administrative command")
        if action in ("index", "add"):
            others = [p for p in owned_processes(self.c.dowse_exe.parent)
                      if "watch" in (p.get("CommandLine") or "").lower()]
            if self.watch is not None or others:
                self.c.log("dowse.mutation_blocked", action=action, watchers=others)
                raise RuntimeError("stop watch before index/add")
        command = [str(self.c.dowse_exe), action, *map(str, args)]
        self.c.log("dowse.command_start", command=command, index=str(self.c.index_dir))
        p = subprocess.run(command, capture_output=True, env=self.env, timeout=60,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        self.c.log("dowse.command_end", action=action, code=p.returncode,
                   stdout=p.stdout.decode("utf-8", "replace"), stderr=p.stderr.decode("utf-8", "replace"))
        if p.returncode:
            raise RuntimeError("dowse command failed; inspect events")

    def search(self, query):
        with Rpc([str(self.c.dowse_exe), "mcp"], env=self.env) as rpc:
            hits = []; offset = 0
            while True:
                result = rpc.tool("search", {"query": normalize(query), "ext": "txt",
                                            "limit": 100, "offset": offset})
                hits.extend(result["hits"])
                if len(hits) >= result["total_hits"]:
                    return hits
                if not result["hits"] or len(hits) > 10000:
                    raise RuntimeError("incomplete dowse pagination")
                offset += len(result["hits"])

    def wait_for(self, query, present=True, timeout=12):
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if bool(self.search(query)) == present:
                return round(time.monotonic() - start, 3)
            time.sleep(.25)
        raise TimeoutError(f"index health check failed: {query!r} present={present}")

    def health(self, timeout=12):
        if self.watch is None or self.watch.poll() is not None:
            raise RuntimeError("watch is not alive")
        token = "health" + uuid.uuid4().hex
        atomic_write(self.c.materials / "_health/canary.search.txt", token)
        delay = self.wait_for(token, timeout=timeout)
        self.c.log("dowse.health_pass", pid=self.watch.pid, token=token, delay=delay)

    def start(self):
        if self.watch is not None:
            raise RuntimeError("watch already managed")
        self.watch_log = (self.c.logs_dir / "watch.log").open("ab")
        si = subprocess.STARTUPINFO(); si.dwFlags |= subprocess.STARTF_USESHOWWINDOW; si.wShowWindow = 0
        self.watch = subprocess.Popen([str(self.c.dowse_exe), "watch"], env=self.env,
                                      stdout=self.watch_log, stderr=self.watch_log,
                                      creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=si)
        self.c.log("dowse.watch_start", pid=self.watch.pid, index=str(self.c.index_dir))
        # Wait for the initial subscription before the first canary write.
        time.sleep(1)
        # NTFS MFT/USN subscription may need longer than a normal incremental
        # update on this low-memory machine, especially during a large intake.
        self.health(timeout=45)

    def stop(self):
        if self.watch is None:
            return
        p = self.watch
        sent = stop_console(p) if p.poll() is None else True
        # The USN reader has a separate stop flag set only after the last
        # index flush. A single wake can arrive too early; keep waking until
        # both the main loop and the reader have observed their stop flags.
        until = time.monotonic() + 30
        wakes = 0
        while p.poll() is None and time.monotonic() < until:
            atomic_write(self.c.materials / "_health/stop-wakeup.tmp", uuid.uuid4().hex)
            wakes += 1
            try:
                p.wait(timeout=.3)
            except subprocess.TimeoutExpired:
                pass
        try:
            p.wait(timeout=.1)
        except subprocess.TimeoutExpired:
            p.terminate(); p.wait(timeout=5)
            self.c.log("dowse.watch_forced_stop", pid=p.pid)
            raise RuntimeError("watch failed to stop gracefully")
        finally:
            self.watch_log.close(); self.watch_log = None; self.watch = None
        self.c.log("dowse.watch_stopped", pid=p.pid, signal_sent=sent, wakeups=wakes, exit_code=p.returncode)
        if p.returncode != 0:
            raise RuntimeError("watch abnormal exit")

    def maintain(self, action, target):
        if action not in ("index", "add"):
            raise ValueError("maintenance permits only index/add")
        self.stop()
        try:
            self.command(action, target)
        finally:
            # Even failed maintenance attempts recover watch when an index exists.
            if (self.c.index_dir / "meta.json").exists():
                self.start()

    def close(self):
        try:
            self.stop()
        finally:
            self.lock.seek(0)
            msvcrt.locking(self.lock.fileno(), msvcrt.LK_UNLCK, 1)
            self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
