"""Regenerate offline release evidence on two explicit source checkouts."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import prompt_runtime as rt
import prompt_samples_dump_full as samples
import prompt_eval
import model_ab_test as ab


def frozen(repo, out):
    # Execute the selected checkout's original frozen-sample script in a fresh
    # interpreter. Only already installed dependency paths are inherited.
    with tempfile.TemporaryDirectory(prefix='relation-frozen-') as host_root:
        env = dict(os.environ, ASTRBOT_ROOT=host_root, PYTHONIOENCODING='utf-8')
        command = 'import sys,runpy;sys.path[:0]=' + repr(sys.path) + ';runpy.run_path(' + repr(str(repo / 'tools/behavior_contract_samples.py')) + ',run_name="__main__")'
        run = subprocess.run([sys.executable, '-B', '-c', command], env=env, capture_output=True,
                             text=True, encoding='utf-8', cwd=repo, timeout=120)
        if run.returncode:
            raise RuntimeError('frozen behavior generator failed')
        start = run.stdout.find('## 六维、默认值与展示单位')
        if start < 0:
            raise ValueError('frozen output heading missing')
        result = run.stdout[start:].strip() + '\n'
        out.write_text(result, encoding='utf-8')
        return hashlib.sha256(result.encode()).hexdigest()


async def generate(baseline, candidate, out, reference=None):
    out.mkdir(parents=True, exist_ok=True)
    result = dict(host_version=rt.host_version(), baseline=rt.source_info(baseline, rt.BASELINE),
                  candidate=rt.source_info(candidate), real_model='NOT RUN')
    for side, repo in [('baseline', baseline), ('candidate', candidate)]:
        await samples.export(repo, out / 'requests', side)
        code = await ab.run_side(side, ab.FakeAdapter(), 3, rt.default_scenarios(), 0, None,
                                 out / (side + '_offline_ab.json'), repo=repo)
        if code:
            raise RuntimeError('offline A/B execution failed')
        result[side + '_frozen_sha256'] = frozen(repo, out / (side + '_frozen.md'))
        if reference:
            import compatibility_probe
            report = await compatibility_probe.probe(repo, reference, out / (side + '_compatibility.json'))
            result[side + '_compatibility_cases'] = len(report['records'])
    result['frozen_equal'] = result['baseline_frozen_sha256'] == result['candidate_frozen_sha256']
    if not result['frozen_equal']:
        raise AssertionError('frozen behavior changed')
    evaluation = prompt_eval.evaluate(out / 'requests', out / 'requests')
    (out / 'prompt_evaluation.json').write_text(json.dumps(evaluation, ensure_ascii=False, indent=2), encoding='utf-8')
    if ab.compare(out / 'baseline_offline_ab.json', out / 'candidate_offline_ab.json'):
        raise AssertionError('offline A/B outputs are not comparable')
    result['paired_request_snapshots'] = len(evaluation['rows'])
    result['offline_calls_per_side'] = 60
    result['source_unchanged'] = result['candidate'] == rt.source_info(candidate)
    if not result['source_unchanged']:
        raise AssertionError('candidate source changed during evidence generation')
    (out / 'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline', type=Path, required=True)
    p.add_argument('--candidate', type=Path, default=rt.ROOT)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--reference-source', type=Path)
    a = p.parse_args()
    asyncio.run(generate(a.baseline.resolve(), a.candidate.resolve(), a.out.resolve(), a.reference_source))


if __name__ == '__main__':
    main()
