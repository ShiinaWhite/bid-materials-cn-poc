"""One persistent local supervisor; admin traffic uses authenticated named pipes."""
import hashlib
import json
from multiprocessing.connection import Client, Listener
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

from .common import Config, FileLock, atomic_write, now, safe_path
from .dowse import Dowse
from .workflow import import_material, review_fact, rebuild_projections
from .backup import create_backup


def endpoint(config):
    identity = hashlib.sha256(str(config.path).lower().encode()).hexdigest()[:24]
    return r"\\.\pipe\jty-materials-" + identity


def key(config, create=False):
    path = config.state_dir / "control.key"
    if create and not path.exists():
        try:
            atomic_write(path, os.urandom(32), immutable=True)
        except FileExistsError:
            pass
    return path.read_bytes()


def request(config, action, payload=None, *, start=True, timeout=240):
    def call():
        with Client(endpoint(config), family="AF_PIPE", authkey=key(config)) as conn:
            conn.send_bytes(json.dumps({"action": action, "payload": payload or {}}).encode("utf-8"))
            if not conn.poll(timeout):
                raise TimeoutError("manager operation timed out; inspect doctor before retrying a write")
            response = json.loads(conn.recv_bytes(1024*1024))
        if not response["ok"]:
            raise RuntimeError(response["error"])
        return response["result"]
    try:
        return call()
    except (FileNotFoundError, ConnectionRefusedError, BrokenPipeError):
        if not start:
            return {"running": False}
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    key(config, create=True)
    with (config.logs_dir / "manager-console.log").open("ab") as log:
        entry = Path(__file__).resolve().parents[1] / "manage.py"
        child = subprocess.Popen([sys.executable, "-B", str(entry), "--config", str(config.path)],
                                 stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
    until = time.monotonic() + 12
    while time.monotonic() < until:
        try:
            return call()
        except (FileNotFoundError, ConnectionRefusedError, BrokenPipeError):
            if child.poll() is not None:
                raise RuntimeError("manager startup failed; inspect manager-console.log")
            time.sleep(.2)
    raise TimeoutError("manager startup timeout")


class Supervisor:
    def __init__(self, config):
        self.c = config
        self.d = Dowse(config)
        self.phase_ocr=None

    def status(self):
        return {"running": True, "pid": os.getpid(), "watch_pid": self.d.watch.pid if self.d.watch else None,
                "index": str(self.c.index_dir), "maintenance": self.c.maintenance_file.exists()}

    def job(self, action, payload):
        with FileLock(self.c.repository_root / "db/operation.lock"):
            atomic_write(self.c.maintenance_file, json.dumps({"action": action, "at": now(), "pid": os.getpid()}))
            try:
                self.d.stop()
                if action in ("import", "replace",'batch_import'):
                    result = import_material(self.c, **payload)
                    rebuild_projections(self.c)
                    self.d.maintain("index", self.c.materials)
                elif action == "review":
                    result = review_fact(self.c, payload)
                elif action == "backup":
                    result = create_backup(self.c,**payload)
                elif action in ('migrate-v03','migrate-v04'):
                    from .store import Store
                    result=Store.migrate(self.c)
                elif action=='phase_import':
                    from .phase_ocr import PhaseOcr
                    if self.phase_ocr is None:self.phase_ocr=PhaseOcr(self.c)
                    rows=payload.get('items',[])
                    if not 1<=len(rows)<=5:raise ValueError('phase commit group limited to 1..5')
                    result=[]
                    for row in rows:
                        try:
                            expected=row.pop('expected_source_sha256')
                            from .common import digest
                            if digest(row['source'])!=expected:raise ValueError('source SHA changed after inventory')
                            self.phase_ocr.source_label=row.get('source_metadata',{}).get('source_title',Path(row['source']).name)
                            receipt=import_material(self.c,**row,ocr_factory=lambda _:self.phase_ocr,defer_projection=True)
                            result.append(dict(ok=True,item_id=row['batch_context']['item_id'],receipt=receipt))
                        except Exception as error:
                            result.append(dict(ok=False,item_id=row['batch_context']['item_id'],error=type(error).__name__+': '+str(error)))
                    rebuild_projections(self.c);self.d.maintain('index',self.c.materials)
                elif action=='phase_end':
                    if self.phase_ocr is not None:self.phase_ocr.close();self.phase_ocr=None
                    result={'ocr_closed':True}
                elif action=='phase_relations':
                    from .store import Store
                    from .workflow import audit
                    with Store(self.c) as s:
                        with s.db:
                            for row in payload['rows']:
                                added=s.db.execute("INSERT OR IGNORE INTO material_relation(material_id,related_material_id,relation_type,relation_status,detail_json,created_at) VALUES(?,?,'source_group','relation_pending',?,?)",(row['material_id'],row['related_material_id'],json.dumps(row['detail'],ensure_ascii=False),now()))
                                if added.rowcount:audit(s.db,'phase1-offline-controller','material.relation_candidate','material',row['material_id'],row)
                    result={'relations_processed':len(payload['rows'])}
                elif action=='phase_enrich':
                    from .enrichment import enrich
                    result=enrich(self.c)
                elif action in ('reextract','phase_reextract'):
                    from .reextract import reextract
                    if action=='reextract':result=reextract(self.c,payload,defer_projection=True)
                    else:
                        from .phase_ocr import PhaseOcr
                        if self.phase_ocr is None:self.phase_ocr=PhaseOcr(self.c)
                        rows=payload.get('items',[])
                        if not 1<=len(rows)<=5:raise ValueError('OCR repair group limited to 1..5')
                        result=[]
                        for row in rows:
                            try:result.append(dict(ok=True,material_id=row['material_id'],receipt=reextract(self.c,row,ocr_factory=lambda _:self.phase_ocr,defer_projection=True)))
                            except Exception as e:result.append(dict(ok=False,material_id=row['material_id'],error=type(e).__name__+': '+str(e)))
                    rebuild_projections(self.c);self.d.maintain('index',self.c.materials)
                elif action=='candidates':
                    from .candidates import append_candidates
                    result=append_candidates(self.c,payload)
                elif action in ("index", "add"):
                    rebuild_projections(self.c)
                    target = self.c.materials if action == "index" else safe_path(self.c.repository_root, payload["relative_root"])
                    if not target.is_relative_to(self.c.materials):
                        raise ValueError("add root must be inside configured materials")
                    # This release has one registered root. Vendor `add` rejects
                    # nested/already registered roots; reconcile it with a rebuild.
                    self.d.maintain("index", self.c.materials)
                    result = {"action": action, "executed_action": "index", "healthy": True,
                              "index": str(self.c.index_dir), "root": str(self.c.materials)}
                else:
                    raise ValueError("unknown offline action")
                if self.d.watch is None and (self.c.index_dir / "meta.json").exists():
                    self.d.start()
                generation = self.c.state_dir / "generation.json"
                old = json.loads(generation.read_text()) if generation.exists() else 0
                atomic_write(generation, json.dumps(old+1))
                self.c.maintenance_file.unlink()
                self.c.log("manager.operation_complete", action=action)
                return result
            except Exception as error:
                self.c.log("manager.operation_failed", action=action, error=str(error), traceback=traceback.format_exc())
                try:
                    if self.d.watch is None and (self.c.index_dir / "meta.json").exists():
                        self.d.start()
                except Exception as restart_error:
                    self.c.log("manager.restart_failed", error=str(restart_error))
                if action in ("review", "backup"):
                    self.c.maintenance_file.unlink(missing_ok=True)
                raise

    def dispatch(self, action, payload):
        if action == "status":
            return self.status()
        if action == "watch_start":
            if self.c.maintenance_file.exists() or not (self.c.index_dir / "meta.json").exists():
                return self.job("index", {})
            if self.d.watch is None:
                self.d.start()
            return self.status()
        if action == "watch_stop":
            self.d.stop()
            return self.status()
        if action == "health":
            self.d.health()
            return {"healthy": True, **self.status()}
        if action == "shutdown":
            self.d.stop()
            return {"stopped": True}
        return self.job(action, payload)


def serve(config):
    config.state_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(config.state_dir / "manager.lock"):
        supervisor = Supervisor(config)
        config.log("manager.start", pid=os.getpid(), endpoint=endpoint(config))
        try:
            with Listener(endpoint(config), family="AF_PIPE", authkey=key(config, create=True)) as listener:
                while True:
                    with listener.accept() as conn:
                        action = None
                        try:
                            if not conn.poll(5):
                                continue
                            message = json.loads(conn.recv_bytes(1024*1024))
                            action, payload = message["action"], message.get("payload", {})
                            result = supervisor.dispatch(action, payload)
                            response = {"ok": True, "result": result}
                        except Exception as error:
                            response = {"ok": False, "error": str(error)}
                        try:conn.send_bytes(json.dumps(response, ensure_ascii=False).encode("utf-8"))
                        except (BrokenPipeError,EOFError,OSError) as error:
                            # The transaction/backup may already be committed. Keep the
                            # supervisor alive; retries must use recorded receipts.
                            config.log('manager.reply_lost',action=action,error=str(error),operation_ok=response.get('ok'))
                    if action == "shutdown" and response["ok"]:
                        break
        finally:
            if supervisor.phase_ocr is not None:supervisor.phase_ocr.close()
            supervisor.d.close()
            config.log("manager.stopped", pid=os.getpid())
