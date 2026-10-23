"""Shared evidence checks for CLI and WebUI, including private project files."""
from .common import digest, safe_path


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
