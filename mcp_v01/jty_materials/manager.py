"""One persistent local supervisor; admin traffic uses authenticated named pipes."""
import hashlib
import json
from multiprocessing.connection import Client, Listener
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import traceback

from .common import Config, FileLock, atomic_write, digest, now, safe_path
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


def review_required(dual_receipt, disputes, classification):
    """True only when something genuinely needs a human: an open field dispute
    (engine disagreement or human-fact conflict), ambiguous candidates, a
    single-chain field, a failed validator, or any classification conflict /
    vision failure. Fields the verification policy deliberately excludes
    (scope, audit_opinion, titles, quals, long prose) are NOT exceptions."""
    dual = dual_receipt or {}
    return bool(disputes) or bool(dual.get('ambiguous')) or bool(dual.get('single_chain')) \
        or bool(dual.get('validator_failed')) or bool(dual.get('human_conflicts')) \
        or bool((classification or {}).get('conflict')) \
        or dual.get('vision_status') != 'succeeded'


class Supervisor:
    def __init__(self, config):
        self.c = config
        self.d = Dowse(config)
        self.phase_ocr=None
        from .project_documents import DocumentJobs
        self.documents = DocumentJobs(self)

    def status(self):
        return {"running": True, "pid": os.getpid(), "watch_pid": self.d.watch.pid if self.d.watch else None,
                "index": str(self.c.index_dir), "maintenance": self.c.maintenance_file.exists(),
                "project_document_index": str(self.c.document_index),
                "document_concurrency": 1, "document_queue": self.documents.queue.qsize()}

    def publish_generation(self):
        generation = self.c.state_dir / "generation.json"
        old = json.loads(generation.read_text()) if generation.exists() else 0
        atomic_write(generation, json.dumps(old+1))

    def validate_repository(self):
        # Do not require the current schema here: an explicitly retried failed
        # migration may still have the previous compatible schema after rollback.
        from contextlib import closing
        from .projects import reconcile_files
        with closing(sqlite3.connect(self.c.db_path.as_uri() + '?mode=ro', uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            if [r[0] for r in db.execute('PRAGMA integrity_check')] != ['ok']:
                raise RuntimeError('database integrity check failed')
            if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise RuntimeError('database foreign key check failed')
            # Only a healthy committed DB may decide whether a pending UUID
            # publication is registered evidence or an abandoned file.
            reconcile_files(self.c)
            for path, sha256 in db.execute('SELECT relative_path,sha256 FROM material_file'):
                if digest(safe_path(self.c.repository_root, path)) != sha256:
                    raise RuntimeError('registered evidence checksum mismatch')
            if db.execute("SELECT 1 FROM sqlite_schema WHERE name='project_file'").fetchone():
                for path, sha256 in db.execute('SELECT relative_path,sha256 FROM project_file'):
                    if digest(safe_path(self.c.repository_root, path)) != sha256:
                        raise RuntimeError('registered project evidence checksum mismatch')

    def recover(self):
        """Reconcile derivatives from committed DB state, never replay a write.

        Called only while operation.lock and maintenance are held. Evidence or
        DB damage, disk/index failures and unhealthy watch keep reads closed.
        """
        self.d.stop()
        self.validate_repository()
        rebuild_projections(self.c)
        self.d.maintain('index', self.c.materials)
        self.d.health()
        self.publish_generation()

    def recover_or_retain(self, action, error):
        try:
            self.recover()
            self.c.maintenance_file.unlink()
        except Exception as recovery_error:
            atomic_write(self.c.maintenance_file, json.dumps({
                'action': action, 'at': now(), 'pid': os.getpid(),
                'state': 'recovery_required', 'error': str(error),
                'recovery_error': str(recovery_error)}))
            self.c.log('manager.recovery_failed', action=action,
                       error=str(recovery_error), traceback=traceback.format_exc())
            return False
        self.c.log('manager.operation_recovered', action=action)
        return True

    def _post_import_dual_engine(self, receipt):
        """Zero-fill: a freshly imported/replaced original immediately goes
        through the independent vision chain + consensus (spec #22). Without a
        configured provider the deployment keeps the pure-OCR v0.6.1 behavior;
        any failure leaves the original ingested and candidates pending (#40)."""
        if not isinstance(receipt, dict) or not receipt.get('material_id'):
            return
        if receipt.get('duplicate') or receipt.get('unchanged'):
            return
        from . import providers
        if providers.provider_config(self.c) is None:
            receipt['dual_engine'] = dict(vision_status='not_configured')
            return
        try:
            from .consensus import dual_engine_process, disputes_for
            dual = dual_engine_process(self.c, receipt['material_id'])
            disputes = disputes_for(self.c, receipt['material_id'])
            receipt['dual_engine'] = dict(vision_status=dual.get('vision_status'),
                                          verified=len(dual.get('verified', {})),
                                          disputed=len(disputes),
                                          ambiguous=len(dual.get('ambiguous', {})),
                                          single_chain=len(dual.get('single_chain', [])),
                                          validator_failed=len(dual.get('validator_failed', [])),
                                          human_conflicts=len(dual.get('human_conflicts', [])),
                                          policy_skipped=len(dual.get('policy_skipped', [])),
                                          verified_fields=sorted(dual.get('verified', {})))
            classification = receipt.get('classification') or {}
            receipt['requires_human_review'] = review_required(dual, disputes, classification)
        except Exception as error:
            self.c.log('manager.dual_engine_failed', material_id=receipt.get('material_id'),
                       error=type(error).__name__ + ': ' + str(error)[:300])
            receipt['dual_engine'] = dict(vision_status='failed', error=str(error)[:200])

    def job(self, action, payload):
        if action.startswith('project.document.'):
            try:
                result = self.documents.execute(action, payload)
            except Exception as error:
                if action in ('project.document._finish','project.document._progress'):
                    self.documents.fail(payload, error)
                raise
            if action == 'project.document._finish':
                self.documents.publish_finished(payload['run_id'])
            return result
        with FileLock(self.c.repository_root / "db/operation.lock"):
            resuming_migration = False
            # A previous failed recovery must be reconciled before any new write.
            if self.c.maintenance_file.exists():
                if action in ('migrate-v03', 'migrate-v04', 'migrate-v05', 'migrate-v06', 'migrate-v07'):
                    # The migration itself is the explicit repair for an older
                    # schema. Check evidence/DB first; it keeps maintenance until
                    # successful completion, just like every other job.
                    self.validate_repository()
                    resuming_migration = True
                elif not self.recover_or_retain(action, 'previous maintenance requires recovery'):
                    raise RuntimeError('MAINTENANCE: recovery required; inspect manager events')
            atomic_write(self.c.maintenance_file, json.dumps({"action": action, "at": now(), "pid": os.getpid()}))
            try:
                self.d.stop()
                if action in ("import", "replace",'batch_import'):
                    from . import providers as _providers
                    vision_provider = _providers.vision_provider(self.c) if action in ('import', 'replace') else None
                    payload = dict(payload)
                    payload['vision_provider'] = vision_provider
                    result = import_material(self.c, **payload)
                    self._post_import_dual_engine(result)
                    rebuild_projections(self.c)
                    self.d.maintain("index", self.c.materials)
                elif action == "review":
                    result = review_fact(self.c, payload)
                elif action == "backup":
                    result = create_backup(self.c,**payload)
                elif action in ('migrate-v03','migrate-v04','migrate-v05','migrate-v06','migrate-v07'):
                    from .store import Store
                    result=Store.migrate(self.c)
                    if resuming_migration:
                        self.recover()
                elif action.startswith('project.'):
                    from .projects import execute
                    result = execute(self.c, action, payload)
                elif action=='phase_import':
                    from .phase_ocr import PhaseOcr
                    if self.phase_ocr is None:self.phase_ocr=PhaseOcr(self.c)
                    rows=payload.get('items',[])
                    if not 1<=len(rows)<=5:raise ValueError('phase commit group limited to 1..5')
                    result=[]
                    for row in rows:
                        row = dict(row)  # keep approved retry payload intact
                        try:
                            expected=row.pop('expected_source_sha256')
                            from .common import digest
                            if digest(row['source'])!=expected:raise ValueError('source SHA changed after inventory')
                            self.phase_ocr.source_label=row.get('source_metadata',{}).get('source_title',Path(row['source']).name)
                            receipt=import_material(self.c,**row,ocr_factory=lambda _:self.phase_ocr,defer_projection=True)
                            result.append(dict(ok=True,item_id=row['batch_context']['item_id'],receipt=receipt))
                        except Exception as error:
                            self.c.log('manager.item_failed', action=action, error=str(error))
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
                elif action in ('reextract','reextract_current','phase_reextract'):
                    from .reextract import reextract,current_record,existing_ocr
                    if action in ('reextract','reextract_current'):
                        record=current_record(self.c,payload) if action=='reextract_current' else payload
                        cached=existing_ocr(self.c,record) if action=='reextract_current' else None
                        options=dict(defer_projection=True)
                        if cached is not None:options['ocr_factory']=lambda _:cached
                        result=reextract(self.c,record,**options)
                    else:
                        from .phase_ocr import PhaseOcr
                        if self.phase_ocr is None:self.phase_ocr=PhaseOcr(self.c)
                        rows=payload.get('items',[])
                        if not 1<=len(rows)<=5:raise ValueError('OCR repair group limited to 1..5')
                        result=[]
                        for row in rows:
                            try:result.append(dict(ok=True,material_id=row['material_id'],receipt=reextract(self.c,row,ocr_factory=lambda _:self.phase_ocr,defer_projection=True)))
                            except Exception as e:
                                self.c.log('manager.item_failed', action=action, error=str(e))
                                result.append(dict(ok=False,material_id=row['material_id'],error=type(e).__name__+': '+str(e)))
                    rebuild_projections(self.c);self.d.maintain('index',self.c.materials)
                    if action in ('reextract','reextract_current') and isinstance(result,dict):
                        self._post_import_dual_engine(result)
                elif action=='candidates':
                    from .candidates import append_candidates
                    result=append_candidates(self.c,payload)
                elif action=='dual_engine_process':
                    from .consensus import dual_engine_process
                    result=dual_engine_process(self.c,payload['material_id'])
                elif action=='exception.resolve':
                    from .consensus import resolve_exception
                    result=resolve_exception(self.c,payload)
                elif action=='material.reclassify':
                    from .consensus import reclassify
                    result=reclassify(self.c,payload)
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
                self.publish_generation()
                self.c.maintenance_file.unlink()
                self.c.log("manager.operation_complete", action=action)
                return result
            except Exception as error:
                self.c.log("manager.operation_failed", action=action, error=str(error), traceback=traceback.format_exc())
                self.recover_or_retain(action, error)
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
            if self.documents.capabilities:
                raise RuntimeError('document extraction active; wait for completion before shutdown')
            self.d.stop()
            return {"stopped": True}
        return self.job(action, payload)


def serve(config):
    config.state_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(config.state_dir / "manager.lock"):
        supervisor = Supervisor(config)
        supervisor.documents.recover()
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
