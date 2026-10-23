"""Full regression with isolated artifacts and a mandatory real dowse runtime.

P05_DOWSE_EXE must be supplied explicitly. No production config is loaded.
"""
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    executable = Path(os.environ.get('P05_DOWSE_EXE', 'missing-dowse.exe')).resolve()
    if not executable.is_file():
        raise ValueError('Supply P05_DOWSE_EXE pointing to a real dowse executable; skipping is forbidden')
    root = Path(__file__).resolve().parents[1]/'runs/large-document'
    run = root/('regression-'+dt.datetime.now().strftime('%Y%m%d-%H%M%S'))
    run.mkdir(parents=True)
    temp = root/'tmp'; temp.mkdir(exist_ok=True)
    os.environ['TEMP'] = os.environ['TMP'] = str(temp)
    tempfile.tempdir = str(temp)
    import test_v01
    import test_mobile_ui
    test_v01.BASE = run/'legacy'
    test_mobile_ui.BASE = run/'mobile'
    pattern = sys.argv[1] if len(sys.argv) > 1 else 'test_*.py'
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent), pattern=pattern)
    class Tee:
        def __init__(self, file): self.file = file
        def write(self, value): sys.stderr.write(value); self.file.write(value); self.file.flush()
        def flush(self): sys.stderr.flush(); self.file.flush()
    with (run/'tests.log').open('w', encoding='utf-8') as log:
        result = unittest.TextTestRunner(stream=Tee(log), verbosity=2).run(suite)
    summary = dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
                   passed=result.wasSuccessful() and not result.skipped, artifacts=str(run), dowse_exe=str(executable))
    (run/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
