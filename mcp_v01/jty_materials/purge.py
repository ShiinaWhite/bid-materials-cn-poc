"""Controlled purge of point-in-time transient materials (maintenance only).

The evidence model forbids deletes (no_delete_* triggers) because evidence is
append-only. A purge is an owner-level maintenance decision that specific
materials never belonged in the durable library (e.g. point-in-time
creditchina.gov.cn query captures that must be re-queried before every bid).
This module implements that decision as a narrow, audited action:

- reachable only through the offline manager pipe; no WebUI/MCP route exists
- one fixed reason vocabulary, one purpose; explicit material_id list required
- refuses any material with an active project reference, any verification
  history (human or machine ledger events, accepted/machine-verified facts),
  or any file that fails checksum / directory-exclusivity checks
- the DB delete is one transaction; the BEFORE DELETE guard triggers are
  captured verbatim, dropped, and recreated inside that same transaction
- filesystem removal happens after the DB commit, driven by a journal written
  before the transaction; an interrupted removal leaves the journal on disk
  describing exactly what remains, and the manager recovery path keeps the
  repository readable (deleted rows no longer reference those files)
"""
import json
import os
import sqlite3
import uuid
from pathlib import Path

from .common import atomic_write, digest, now, safe_path
from .store import SCHEMA_VERSION

# Closed reason vocabulary; one entry per authorized cleanup campaign.
# 'transient_credit_china_query': point-in-time creditchina.gov.cn captures.
# 'transient_tender_query': public-registry query captures that must be
#   re-queried before each tender (court/mohurd/wenshu/gsxt etc.).
REASONS = frozenset({'transient_credit_china_query', 'transient_tender_query'})
ACTOR = 'system-maintenance'
# Every BEFORE DELETE guard the evidence model puts in front of a full
# material purge. Captured verbatim, dropped, and recreated inside the same
# transaction as the deletes.
PURGE_TRIGGERS = ('no_delete_material', 'no_delete_material_file',
                  'no_delete_certificate_detail', 'no_delete_performance_detail',
                  'v04_relation_DELETE', 'project_material_no_delete')


def _blocked(detail):
    return RuntimeError('BLOCKED_DELETE: ' + detail)


def purge_transient(config, payload):
    if not isinstance(payload, dict) or payload.get('reason') not in REASONS:
        raise ValueError('material.purge_transient requires reason in ' + repr(sorted(REASONS)))
    reason = payload['reason']
    ids = payload.get('material_ids')
    if (not isinstance(ids, list) or not ids
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError('material_ids must be a non-empty list of distinct integers')

    # ---- read-only preflight: nothing is modified before every check passes
    con = sqlite3.connect(config.db_path.as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    try:
        version = con.execute("SELECT meta_value FROM schema_meta WHERE meta_key='schema_version'").fetchone()
        if version is None or version[0] != SCHEMA_VERSION:
            raise ValueError('unexpected schema; purge refuses to run')
        marks = ','.join('?' * len(ids))
        materials = con.execute(
            f"SELECT material_id, material_code, title FROM material WHERE material_id IN ({marks})", ids).fetchall()
        if len(materials) != len(ids):
            missing = sorted(set(ids) - {m['material_id'] for m in materials})
            raise _blocked(f'material not found: {missing}')
        active_refs = con.execute(
            f"SELECT material_id, count(*) n FROM project_material WHERE material_id IN ({marks})"
            " AND status='active' GROUP BY material_id", ids).fetchall()
        if active_refs:
            raise _blocked('active project reference: ' + str({r['material_id']: r['n'] for r in active_refs}))
        events = con.execute(
            f"SELECT material_id, count(*) n FROM verification_event WHERE material_id IN ({marks})"
            " GROUP BY material_id", ids).fetchall()
        if events:
            raise _blocked('verification ledger not empty: ' + str({r['material_id']: r['n'] for r in events}))
        invested = con.execute(
            f"SELECT material_id, count(*) n FROM extracted_fact WHERE material_id IN ({marks})"
            " AND (review_status IN ('accepted','rejected') OR verification='machine_verified')"
            " GROUP BY material_id", ids).fetchall()
        if invested:
            raise _blocked('accepted/machine-verified facts exist: ' + str({r['material_id']: r['n'] for r in invested}))

        plans = {}
        for m in materials:
            prefix = f"materials/{m['material_code']}/"
            rows = con.execute("SELECT relative_path, sha256, role, filename FROM material_file WHERE material_id=?",
                               (m['material_id'],)).fetchall()
            if not rows:
                raise _blocked(f"no registered files for {m['material_code']}")
            foreign = con.execute(
                f"""SELECT relative_path FROM material_file WHERE relative_path LIKE ? AND material_id NOT IN ({marks})""",
                (prefix + '%', *ids)).fetchone() or con.execute(
                f"""SELECT relative_path FROM search_projection WHERE relative_path LIKE ? AND material_id NOT IN ({marks})""",
                (prefix + '%', *ids)).fetchone()
            if foreign:
                raise _blocked(f"{m['material_code']}: directory shares evidence with another material ({foreign[0]})")
            for row in rows:
                if not row['relative_path'].startswith(prefix):
                    raise _blocked(f"{m['material_code']}: registered file outside its directory ({row['relative_path']})")
                path = safe_path(config.repository_root, row['relative_path'])
                if not path.is_file() or digest(path) != row['sha256']:
                    raise _blocked(f"{m['material_code']}: evidence checksum mismatch ({row['relative_path']})")
            original = con.execute(
                "SELECT sha256, filename, relative_path FROM material_file WHERE material_id=? AND role='original'"
                " AND coalesce(is_current,0)=1", (m['material_id'],)).fetchone()
            source = con.execute(
                "SELECT detail_json FROM audit_log WHERE entity_type='material' AND entity_id=?"
                " AND action='material.source_context'", (m['material_id'],)).fetchone()
            source_name = json.loads(source['detail_json']).get('source_title') if source else None
            directory = config.materials / m['material_code']
            disk_files = []
            for root, dirs, files in os.walk(directory):
                root_path = Path(root).resolve()
                if not root_path.is_relative_to(config.materials.resolve()):
                    raise _blocked(f"{m['material_code']}: directory escapes materials root")
                for name in files:
                    p = root_path / name
                    disk_files.append({'path': str(p), 'size': p.stat().st_size})
            plans[m['material_id']] = dict(code=m['material_code'], title=m['title'],
                                           original=dict(sha256=original['sha256'], filename=original['filename'],
                                                         relative_path=original['relative_path']) if original else None,
                                           source_filename=source_name, disk_files=disk_files,
                                           bytes=sum(f['size'] for f in disk_files))
    finally:
        con.close()

    # ---- journal first: the removal intent survives any later interruption
    stamp = now().replace(':', '').replace('-', '')[:15]
    journal = config.state_dir / 'purge' / f'journal-{stamp}-{uuid.uuid4().hex[:8]}.json'
    record = {'action': 'material.purge_transient', 'reason': reason, 'actor': ACTOR,
              'started_at': now(), 'status': 'pending',
              'materials': [{'material_id': mid, 'material_code': p['code'], 'title': p['title'],
                             'original_sha256': (p['original'] or {}).get('sha256'),
                             'source_filename': p['source_filename'],
                             'files': p['disk_files'], 'bytes': p['bytes']} for mid, p in plans.items()]}
    atomic_write(journal, json.dumps(record, ensure_ascii=False, indent=2))

    # ---- single-transaction DB removal; triggers captured and recreated verbatim
    db = sqlite3.connect(config.db_path, timeout=5)
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=5000')
        db.execute('PRAGMA recursive_triggers=ON')
        db.execute('BEGIN IMMEDIATE')
        try:
            triggers = db.execute(
                "SELECT name, sql FROM sqlite_schema WHERE type='trigger' AND name IN (?,?,?,?,?,?)",
                PURGE_TRIGGERS).fetchall()
            if len(triggers) != len(PURGE_TRIGGERS):
                raise _blocked('guard trigger set does not match expectation; schema state unclear')
            for name, _ in triggers:
                db.execute(f'DROP TRIGGER "{name}"')
            db.execute(f"DELETE FROM material_relation WHERE material_id IN ({marks}) OR related_material_id IN ({marks})", (*ids, *ids))
            db.execute(f"DELETE FROM search_projection WHERE material_id IN ({marks})", ids)
            db.execute(f"DELETE FROM extraction_run WHERE material_id IN ({marks})", ids)
            db.execute(f"DELETE FROM project_material WHERE material_id IN ({marks})", ids)
            db.execute(f"DELETE FROM material WHERE material_id IN ({marks})", ids)
            for name, sql in triggers:
                db.execute(sql)
            details = []
            for mid, p in sorted(plans.items()):
                detail = {'material_id': mid, 'material_code': p['code'], 'title': p['title'],
                          'original_sha256': (p['original'] or {}).get('sha256'),
                          'source_filename': p['source_filename'],
                          'original_relative_path': (p['original'] or {}).get('relative_path'),
                          'deleted_file_list': [f['path'] for f in p['disk_files']],
                          'bytes': p['bytes'], 'timestamp': now(), 'reason': reason}
                details.append(detail)
                db.execute("INSERT INTO audit_log(at,actor,action,entity_type,entity_id,detail_json,note)"
                           " VALUES(?,?,?,?,?,?,?)",
                           (now(), ACTOR, 'material.purge_transient', 'material', mid,
                            json.dumps(detail, ensure_ascii=False), reason))
            db.execute("INSERT INTO audit_log(at,actor,action,entity_type,entity_id,detail_json,note)"
                       " VALUES(?,?,?,?,NULL,?,?)",
                       (now(), ACTOR, 'material.purge_transient', 'system',
                        json.dumps({'materials_removed': len(ids), 'material_ids': sorted(ids),
                                    'files_planned': sum(len(p['disk_files']) for p in plans.values()),
                                    'bytes_planned': sum(p['bytes'] for p in plans.values()),
                                    'journal': str(journal), 'timestamp': now()}, ensure_ascii=False), reason))
            if db.execute(f"SELECT count(*) FROM material WHERE material_id IN ({marks})", ids).fetchone()[0]:
                raise RuntimeError('post-delete verification failed; transaction rolled back')
            db.commit()
        except BaseException:
            db.rollback()
            raise
    finally:
        db.close()

    # ---- filesystem removal after commit; journal tracks any interruption
    files_removed, bytes_removed, failures = 0, 0, []
    for mid, p in sorted(plans.items()):
        directory = config.materials / p['code']
        for root, dirs, files in os.walk(directory, topdown=False):
            for name in files:
                f = Path(root) / name
                try:
                    size = f.stat().st_size
                    f.unlink()
                    files_removed += 1
                    bytes_removed += size
                except OSError as error:
                    failures.append({'file': str(f), 'error': str(error)})
            for name in dirs:
                try:
                    (Path(root) / name).rmdir()
                except OSError as error:
                    failures.append({'dir': str(Path(root) / name), 'error': str(error)})
        try:
            directory.rmdir()
        except OSError as error:
            failures.append({'dir': str(directory), 'error': str(error)})
    record.update({'status': 'complete' if not failures else 'file_delete_incomplete',
                   'finished_at': now(), 'files_removed': files_removed,
                   'bytes_removed': bytes_removed, 'failures': failures})
    atomic_write(journal, json.dumps(record, ensure_ascii=False, indent=2))
    if failures:
        raise RuntimeError(f'purge committed but file removal incomplete; journal: {journal}')
    return {'action': 'material.purge_transient', 'reason': reason, 'actor': ACTOR,
            'material_ids': sorted(ids), 'materials_removed': len(ids),
            'files_removed': files_removed, 'bytes_removed': bytes_removed,
            'audit_rows': len(ids) + 1, 'journal': str(journal)}
