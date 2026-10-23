"""Shared evidence checks for CLI and WebUI, including private project files."""
from .common import digest, safe_path


def check_documents(config, db):
    import hashlib
    from .common import normalize
    from .document_index import health
    errors, sources, expected = [], [], set()
    documents = list(db.execute('SELECT * FROM project_document'))
    unit_count = db.execute('SELECT count(*) FROM project_document_unit').fetchone()[0]
    counts = {r[0]: (r[1],r[2]) for r in db.execute('SELECT document_id,count(*),sum(length(text_raw)) FROM project_document_unit GROUP BY document_id')}
    for d in documents:
        source = db.execute('SELECT * FROM project_file WHERE project_file_id=?', (d['project_file_id'],)).fetchone()
        if source is None or source['sha256'] != d['source_sha256']:
            sources.append(d['document_id'] + ': source metadata mismatch')
        else:
            try:
                path = safe_path(config.repository_root, source['relative_path'])
                if not path.is_file() or path.stat().st_size != source['size_bytes'] or digest(path) != source['sha256']:
                    sources.append(d['document_id'] + ': source evidence size/SHA mismatch')
            except (OSError, ValueError) as error:
                sources.append(str(error))
        count, length = counts.get(d['document_id'], (0,0))
        if d['unit_count'] != count or d['text_length'] != length:
            errors.append(d['document_id'] + ': rebuildable derivative count/length mismatch')
        if db.execute('SELECT 1 FROM project_document_unit WHERE document_id=? AND run_id IS NOT ? LIMIT 1', (d['document_id'],d['current_run_id'])).fetchone():
            errors.append(d['document_id'] + ': rebuildable derivative run mismatch')
    for u in db.execute('SELECT * FROM project_document_unit'):
        try:
            p = safe_path(config.document_root, u['projection_path'])
            expected.add(p)
            canonical = f'{u["document_id"]}/{u["run_id"]}/unit-{u["unit_index"]:06d}.txt'
            valid = (u['projection_path'] == canonical and normalize(u['text_raw']) == u['text_norm']
                     and hashlib.sha256(u['text_norm'].encode()).hexdigest() == u['projection_sha256']
                     and p.is_file() and digest(p) == u['projection_sha256'])
            if not valid:
                errors.append(u['unit_id'] + ': rebuildable derivative projection/mapping error')
        except (OSError, ValueError) as error:
            errors.append(u['unit_id'] + ': rebuildable derivative error: ' + str(error))
    if config.document_root.exists():
        for p in config.document_root.rglob('*'):
            if p.is_file() and p.resolve() not in expected:
                errors.append('orphan rebuildable projection: ' + str(p))
    orphan_documents = db.execute('SELECT count(*) FROM project_document d LEFT JOIN project_file f USING(project_file_id) WHERE f.project_file_id IS NULL').fetchone()[0]
    orphan_units = db.execute('SELECT count(*) FROM project_document_unit u LEFT JOIN project_document d USING(document_id) LEFT JOIN project_document_run r ON r.run_id=u.run_id WHERE d.document_id IS NULL OR r.run_id IS NULL').fetchone()[0]
    index = health(config, db)
    return dict(documents=len(documents), units=unit_count, projections=len(expected), source_errors=sources,
                derivative_errors=errors, orphan_documents=orphan_documents, orphan_units=orphan_units,
                index=index, healthy=not (sources or errors or orphan_documents or orphan_units) and index['healthy'])


def check_files(config, db, table):
    if table not in ('material_file', 'project_file', 'search_projection'):
        raise ValueError('invalid evidence table')
    checked, errors = 0, []
    for row in db.execute('SELECT relative_path,sha256 FROM ' + table):
        checked += 1
        try:
            path = safe_path(config.repository_root, row['relative_path'])
            ok = path.is_file() and digest(path) == row['sha256']
        except (OSError, ValueError):
            ok = False
        if not ok:
            errors.append(row['relative_path'])
    return dict(checked=checked, errors=errors)
