"""A standalone SQLite FTS5 derivative. No connection to Material/dowse.

Trigram phrases support CJK and identifier substrings. One/two-character terms
use a literal scan of this index, constrained to the selected project. All
returned identities/text are resolved against the authoritative document DB.
"""
from contextlib import closing
import hashlib
import sqlite3

from .common import normalize


def connect(config, readonly=False):
    if readonly:
        db = sqlite3.connect(config.document_index.as_uri() + '?mode=ro', uri=True, timeout=5)
        db.execute('PRAGMA query_only=ON')
    else:
        config.document_index.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(config.document_index, timeout=5)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS units USING fts5(
            project_id UNINDEXED, document_id UNINDEXED, run_id UNINDEXED,
            unit_id UNINDEXED, text_norm, tokenize='trigram')""")
    db.row_factory = sqlite3.Row
    return db


def publish(config, source_db, document_id=None):
    with closing(connect(config)) as index:
        with index:
            if document_id:
                index.execute('DELETE FROM units WHERE document_id=?', (document_id,))
            else:
                index.execute('DELETE FROM units')
            where, params = (' WHERE d.document_id=?', (document_id,)) if document_id else ('', ())
            for r in source_db.execute('''SELECT f.project_id,u.* FROM project_document_unit u
                JOIN project_document d USING(document_id) JOIN project_file f USING(project_file_id)''' + where, params):
                index.execute('INSERT INTO units(project_id,document_id,run_id,unit_id,text_norm) VALUES(?,?,?,?,?)',
                              (r['project_id'], r['document_id'], r['run_id'], r['unit_id'], r['text_norm'].casefold()))
            index.execute("INSERT INTO units(units) VALUES('integrity-check')")


def matches(config, project_id, query):
    query = normalize(query).strip().casefold()
    if not query or len(query) > 200:
        raise ValueError('请输入 1–200 字的搜索词')
    with closing(connect(config, True)) as index:
        if len(query) >= 3:
            sql, args = ' AND units MATCH ?', ('"' + query.replace('"', '""') + '"',)
        else:
            sql, args = ' AND instr(text_norm,?)>0', (query,)
        # Filter project before returning any identity. Bound by caller pagination
        # through a streaming cursor, never load all unit text in RAM.
        for r in index.execute('SELECT unit_id,run_id,document_id FROM units WHERE project_id=?' + sql + ' ORDER BY rowid', (project_id, *args)):
            yield dict(r)


def health(config, db):
    expected = {r['unit_id']: (r['project_id'], r['document_id'], r['run_id'],
                hashlib.sha256(r['text_norm'].casefold().encode()).hexdigest()) for r in db.execute('''
        SELECT f.project_id,u.* FROM project_document_unit u JOIN project_document d USING(document_id)
        JOIN project_file f USING(project_file_id)''')}
    if not config.document_index.exists():
        return dict(healthy=not expected, units=0, error='missing rebuildable project index' if expected else None)
    try:
        with closing(connect(config, True)) as index:
            if index.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('project index integrity failure')
            actual = {r['unit_id']: (r['project_id'], r['document_id'], r['run_id'],
                      hashlib.sha256(r['text_norm'].encode()).hexdigest()) for r in index.execute('SELECT * FROM units')}
            count = index.execute('SELECT count(*) FROM units').fetchone()[0]
        return dict(healthy=actual == expected and count == len(expected), units=count,
                    error=None if actual == expected and count == len(expected) else 'rebuildable project index mapping/text mismatch')
    except (OSError, sqlite3.Error, ValueError) as error:
        return dict(healthy=False, units=0, error='rebuildable derivative error: ' + str(error))
