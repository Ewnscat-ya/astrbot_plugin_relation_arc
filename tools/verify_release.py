"""Portable isolated verification entry point; use the chosen host's Python.

python tools/verify_release.py --out <outside-repo-directory> [--no-host]
Creates no live account/config changes. --no-host deliberately runs core/P0 only.
"""
import argparse
import hashlib
import importlib.util
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def manifest():
    paths = list(ROOT.glob('*.py')) + list(ROOT.glob('metadata.yaml'))
    paths += list((ROOT / 'tests').glob('*.py')) + list((ROOT / 'tests').glob('*.mjs'))
    paths += list((ROOT / 'tools').glob('*.py')) + [p for p in (ROOT / 'pages').rglob('*') if p.is_file()]
    files = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
    return dict(files=files, sha256=hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest())


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--no-host', action='store_true')
    p.add_argument('--only', nargs='+')
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    source = manifest()
    with tempfile.TemporaryDirectory(prefix='isolated-host-', dir=a.out, ignore_cleanup_errors=True) as host_dir:
        os.environ['ASTRBOT_ROOT'] = host_dir
        os.environ['TMP'] = os.environ['TEMP'] = host_dir
        tempfile.tempdir = host_dir
        old_temp = tempfile.TemporaryDirectory
        def isolated_temp(*args, **kwargs):
            if str(kwargs.get('dir', '')).replace('\\', '/').rstrip('/') == 'D:/第三方插件完善/.tmp_test':
                kwargs['dir'] = host_dir
            return old_temp(*args, **kwargs)
        tempfile.TemporaryDirectory = isolated_temp
        sys.path[:0] = [str(ROOT), str(ROOT.parent), str(ROOT / 'tests')]
        spec = importlib.util.spec_from_file_location('astrbot_plugin_relation_arc', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        if a.only:
            suite = unittest.defaultTestLoader.loadTestsFromNames(a.only)
        elif a.no_host:
            suite = unittest.defaultTestLoader.loadTestsFromNames(['test_core', 'test_p0'])
        else:
            suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
        with (a.out / 'unittest.log').open('w', encoding='utf-8') as log:
            result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
        summary = dict(commit=subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip(),
            mode='no-host' if a.no_host else 'host', host=None if a.no_host else importlib.metadata.version('astrbot'),
            tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
            skipped=len(result.skipped), ok=result.wasSuccessful(), source=source,
            source_unchanged=source == manifest(), python=sys.version.split()[0])
        (a.out / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
        print(json.dumps({k:v for k,v in summary.items() if k != 'source'}))
        return 0 if summary['ok'] and summary['source_unchanged'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
