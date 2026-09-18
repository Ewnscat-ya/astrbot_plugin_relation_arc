"""Full-request A/B runner. Reproducible commands: docs/MODEL_AB.md.

--repo selects the actual plugin checkout; keep this runner in the candidate.
Real execution needs an explicit configured-provider factory and authorization.
There is no automatic provider construction or credential discovery.
"""
from __future__ import annotations
import argparse
import asyncio
import copy
import importlib.util
import inspect
import json
from pathlib import Path
import re
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prompt_runtime as runtime
from provider_adapters import HostAdapter, FakeAdapter, normalize_usage, number

ROOT = runtime.ROOT
DIMENSIONS = {'trust', 'respect', 'comfort', 'closeness', 'resonance', 'romance_interest'}


def _valid_payload(p, allowed):
    if not isinstance(p, dict) or type(p.get('schema_version')) is not int or p['schema_version'] != 3:
        return False
    facts = p.get('fact_effects')
    if not isinstance(facts, list) or len(facts) > 64:
        return False
    for fact in facts:
        if not isinstance(fact, dict) or not isinstance(fact.get('effects'), dict):
            return False
        if any(k not in allowed or type(v) is not int or not 0 < abs(v) <= 10 for k, v in fact['effects'].items()):
            return False
        if any(k in fact and not isinstance(fact[k], str) for k in ('evidence', 'reason')):
            return False
    proposal = p.get('relationship_proposal')
    if proposal is not None and (not isinstance(proposal, dict) or proposal.get('action') != 'bind'
        or proposal.get('type_id') not in {'friend', 'close_friend', 'partner', 'romantic_partner', 'spouse'}
        or proposal.get('origin') not in {'user_request', 'character_initiated', 'mutual_dialogue'}
        or proposal.get('mutuality') not in {'clear', 'insufficient'}):
        return False
    safety = p.get('interaction_safety_proposal')
    if safety is not None and (not isinstance(safety, dict)
        or safety.get('level') not in {'slow_down', 'pause_intimacy'}
        or safety.get('reason_code') not in {'boundary_pressure', 'repeated_escalation', 'hostility'}):
        return False
    return True


def _measure(text, user_text='', allowed_dimensions=None):
    text = text if isinstance(text, str) else ''
    opens = len(re.findall(r'<relation_judgment>', text, re.I))
    closes = len(re.findall(r'</relation_judgment>', text, re.I))
    blocks = list(re.finditer(r'<relation_judgment>(.*?)</relation_judgment>', text, re.S))
    first = text.split('\n', 1)[0]
    first_line = bool(re.fullmatch(r'[ \t]*<relation_judgment>.*</relation_judgment>[ \t\r]*', first))
    schema3 = False
    if len(blocks) == 1 and len(blocks[0].group(1)) <= 65536:
        try:
            schema3 = _valid_payload(json.loads(blocks[0].group(1)),
                                    DIMENSIONS if allowed_dimensions is None else allowed_dimensions)
        except (ValueError, RecursionError, TypeError):
            pass
    return dict(protocol_valid=opens == closes == len(blocks) == 1 and first_line and schema3,
                first_line=first_line, schema3=schema3, omitted=opens == 0,
                duplicate_blocks=opens > 1 or closes > 1)


def _write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.writing')
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    for attempt in range(5):
        try:
            temp.replace(path)
            break
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.025 * (2 ** attempt))


def _cost(r, p):
    if not p or not p.get('source') or not p.get('currency') or any(
        number(r.get(k)) is None for k in ('input_tokens', 'output_tokens')):
        return None
    if p.get('mode') == 'cache':
        if any(number(p.get(k)) is None for k in ('cache_hit_per_1k', 'cache_miss_per_1k', 'output_per_1k')) or any(
            number(r.get(k)) is None for k in ('cache_hit_tokens', 'cache_miss_tokens')):
            return None
        if r['cache_hit_tokens'] + r['cache_miss_tokens'] != r['input_tokens']:
            return None
        cost = r['cache_hit_tokens'] * p['cache_hit_per_1k'] + r['cache_miss_tokens'] * p['cache_miss_per_1k']
    elif p.get('mode') == 'flat' and all(number(p.get(k)) is not None for k in ('input_per_1k', 'output_per_1k')):
        cost = r['input_tokens'] * p['input_per_1k']
    else:
        return None
    return (cost + r['output_tokens'] * p['output_per_1k']) / 1000


async def run_side(side, adapter, repeats, scenarios, warmup, price, out_path, *,
                   repo=ROOT, expected_commit=None, timeout=120):
    payload = dict(format_version=2, side=side, ran=False, completed=False, records=[])
    _write(out_path, payload)
    try:
        if repeats < 1 or warmup < 0 or not scenarios or timeout <= 0:
            raise ValueError('invalid run counts or timeout')
        specs = [runtime.scenario(s, name=f's{i:02}') if isinstance(s, str) else copy.deepcopy(s)
                 for i, s in enumerate(scenarios)]
        if len({s['id'] for s in specs}) != len(specs):
            raise ValueError('scenario IDs must be unique')
        source = runtime.source_info(Path(repo), expected_commit)
        if not adapter.offline and (source['dirty'] or not expected_commit):
            raise ValueError('real runs require clean pinned plugin source')
        plugin_type = runtime.load_plugin(Path(repo))
        metadata = adapter.metadata()
        payload.update(source=source, plugin_commit=source['commit'], host_version=runtime.host_version(),
            adapter=adapter.name, provider=metadata, offline=adapter.offline,
            scenarios=specs, config=dict(repeats=repeats, warmup=warmup, timeout=timeout), price=price,
            harness_sha256=runtime.digest({p.name: p.read_text(encoding='utf-8') for p in
                (Path(__file__), Path(runtime.__file__), Path(__file__).with_name('provider_adapters.py'))}))
        _write(out_path, payload)
        for spec in specs:
            with tempfile.TemporaryDirectory(prefix='relation-ab-') as directory:
                plugin = plugin_type(runtime.SyntheticContext(directory))
                try:
                    event = runtime.configure(plugin, spec)
                    for repetition in range(repeats):
                        req = runtime.new_request(spec)
                        await plugin.inject(event, req)
                        request = await runtime.snapshot(req)
                        for phase, index in [('warmup', i) for i in range(warmup)] + [('measured', 0)]:
                            record = dict(scenario_id=spec['id'], repetition=repetition, phase=phase,
                                phase_index=index, request=request, error=None, state='started',
                                cache_state='unknown' if not warmup or phase == 'warmup' else 'after-explicit-warmup')
                            payload['records'].append(record)
                            payload['ran'] = True
                            _write(out_path, payload)
                            started = time.perf_counter()
                            try:
                                result = await asyncio.wait_for(adapter.complete(copy.deepcopy(req)), timeout)
                                if adapter.metadata() != metadata:
                                    raise RuntimeError('provider settings changed during run')
                                record.update(text=result['text'], **{k: number(result.get('usage', {}).get(k)) for k in
                                    ('input_tokens', 'output_tokens', 'cache_hit_tokens', 'cache_miss_tokens')})
                                record['usage_source'] = result.get('usage', {}).get('source', 'missing')
                                record['response_model'] = result.get('response_model')
                                allowed = DIMENSIONS if spec.get('visible') else DIMENSIONS - {'romance_interest'}
                                record.update(_measure(result['text'], spec['prompt'], allowed))
                                record['cost'] = None if adapter.offline else _cost(record, price)
                                record['state'] = 'ok'
                            except Exception as exc:
                                # Exception strings can contain API keys/headers. Persist only type.
                                record.update(error=type(exc).__name__, state='failed', text=None, cost=None)
                            record['latency_ms'] = (time.perf_counter() - started) * 1000
                            _write(out_path, payload)
                            if record['error'] is not None:
                                break
                finally:
                    await plugin.terminate()
        payload['completed'] = True
    except Exception as exc:
        payload['reason'] = type(exc).__name__
    finally:
        _write(out_path, payload)
    summary = _summary(payload)
    payload['summary'] = summary
    _write(out_path, payload)
    print(json.dumps(dict(side=side, status=summary['status'], records=len(payload['records']), offline=adapter.offline)))
    expected = len(scenarios) * repeats * (warmup + 1)
    return 0 if payload['completed'] and summary['status'] == 'ok' and len(payload['records']) == expected else 2


def _summary(payload):
    all_records = payload.get('records', [])
    measured = [r for r in all_records if r.get('phase', 'measured') == 'measured']
    records = [r for r in measured if r.get('error') is None and r.get('state', 'ok') == 'ok']
    out = dict(records_total=len(all_records), measured=len(measured), records_ok=len(records),
               cache_hit_rate=None, total_cost=None)
    for label, key in [('protocol_valid_rate', 'protocol_valid'), ('first_line_rate', 'first_line'),
                       ('omission_rate', 'omitted'), ('duplicate_rate', 'duplicate_blocks')]:
        out[label] = (sum(r[key] for r in records) / len(records)
                      if records and all(type(r.get(key)) is bool for r in records) else None)
    for label, key in [('median_latency_ms', 'latency_ms'), ('median_input_tokens', 'input_tokens')]:
        values = [number(r.get(key)) for r in records]
        out[label] = statistics.median(values) if values and None not in values else None
    paired = [r for r in records if all(number(r.get(k)) is not None for k in ('cache_hit_tokens', 'cache_miss_tokens'))]
    out['cache_field_reported_fraction'] = len(paired) / len(records) if records else None
    if records and len(paired) == len(records):
        denominator = sum(r['cache_hit_tokens'] + r['cache_miss_tokens'] for r in paired)
        out['cache_hit_rate'] = sum(r['cache_hit_tokens'] for r in paired) / denominator if denominator else None
    costs = [number(r.get('cost')) for r in all_records]
    if costs and None not in costs:
        out['total_cost'] = sum(costs)
    out['status'] = ('ok' if records and out['protocol_valid_rate'] is not None
                     and any(r.get('protocol_valid') for r in records)
                     and all(r.get('error') is None and r.get('state', 'ok') == 'ok' for r in all_records)
                     else 'no-valid-samples' if not records else 'incomplete-or-invalid')
    return out


def compare(baseline_path, candidate_path):
    base, cand = [json.loads(Path(p).read_text(encoding='utf-8')) for p in (baseline_path, candidate_path)]
    problems = []
    for side, p in [('baseline', base), ('candidate', cand)]:
        if p.get('format_version') != 2 or not p.get('ran') or not p.get('completed') or p.get('side') != side:
            problems.append(side + ': missing, incomplete or wrong-side run')
        if _summary(p)['status'] != 'ok':
            problems.append(side + ': failed requests or no valid samples')
        config = p.get('config', {})
        expected = [(s['id'], r, phase, i) for s in p.get('scenarios', [])
                    for r in range(config.get('repeats', 0))
                    for phase, i in [('warmup', i) for i in range(config.get('warmup', 0))] + [('measured', 0)]]
        actual = [(r.get('scenario_id'), r.get('repetition'), r.get('phase'), r.get('phase_index'))
                  for r in p.get('records', [])]
        if not expected or expected != actual:
            problems.append(side + ': planned request coverage is incomplete')
    for key in ('scenarios', 'provider', 'host_version', 'config', 'price', 'harness_sha256'):
        if key not in base or base.get(key) != cand.get(key):
            problems.append('incomparable ' + key)
    if base.get('source', {}).get('source_sha256') == cand.get('source', {}).get('source_sha256'):
        problems.append('both sides loaded the same source')
    def keys(p):
        return [(r.get('scenario_id'), r.get('repetition'), r.get('phase'), r.get('phase_index')) for r in p.get('records', [])]
    if keys(base) != keys(cand) or len(keys(base)) != len(set(keys(base))):
        problems.append('unpaired or duplicate requests')
    if any(b.get('response_model') != c.get('response_model')
           for b, c in zip(base.get('records', []), cand.get('records', []))):
        problems.append('provider returned different model identifiers')
    report = dict(status='UNKNOWN' if problems else 'offline-chain-only' if base.get('offline') else 'measured',
                  problems=problems, baseline=_summary(base), candidate=_summary(cand),
                  note='No cold-cache assumption. Costs require complete usage and sourced pricing; offline is not model evidence.')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if problems else 0


async def _factory(reference):
    path, name = reference.rsplit(':', 1)
    spec = importlib.util.spec_from_file_location('relation_authorized_provider', Path(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = getattr(module, name)()
    return await result if inspect.isawaitable(result) else result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run')
    run.add_argument('--side', choices=['baseline', 'candidate'], required=True)
    run.add_argument('--repo', type=Path, required=True)
    run.add_argument('--expected-commit')
    run.add_argument('--out', type=Path, required=True)
    run.add_argument('--adapter', choices=['fake', 'factory'], default='factory')
    run.add_argument('--provider-factory', help='authorized local file.py:function returning HostAdapter')
    run.add_argument('--allow-paid', action='store_true')
    run.add_argument('--max-calls', type=int)
    run.add_argument('--repeats', type=int, default=3)
    run.add_argument('--scenarios', type=int, default=20)
    run.add_argument('--warmup', type=int, default=0)
    run.add_argument('--timeout', type=float, default=120)
    run.add_argument('--price-json', type=Path)
    run.add_argument('--plan-only', action='store_true')
    comp = sub.add_parser('compare')
    comp.add_argument('baseline', type=Path)
    comp.add_argument('candidate', type=Path)
    args = p.parse_args()
    if args.command == 'compare':
        return compare(args.baseline, args.candidate)
    async def execute():
        if not (1 <= args.scenarios <= 20) or args.repeats < 1 or args.warmup < 0 or args.timeout <= 0:
            raise ValueError('invalid scenario/repeat/warmup/timeout')
        source = runtime.source_info(args.repo, args.expected_commit)
        count = args.scenarios * args.repeats * (args.warmup + 1)
        if args.plan_only:
            _write(args.out, dict(side=args.side, ran=False, planned_calls=count, source=source, reason='plan-only; no adapter loaded'))
            return 0
        if args.adapter == 'fake':
            adapter = FakeAdapter()
        else:
            if not args.allow_paid or not args.provider_factory or not args.expected_commit or source['dirty']:
                raise ValueError('authorization, factory and clean pinned source required')
            if args.max_calls is None or count > args.max_calls:
                raise ValueError('explicit call budget exceeded')
            adapter = await _factory(args.provider_factory)
            if not isinstance(adapter, HostAdapter):
                raise ValueError('factory must return HostAdapter')
        price = json.loads(args.price_json.read_text(encoding='utf-8')) if args.price_json else None
        try:
            return await run_side(args.side, adapter, args.repeats, runtime.default_scenarios()[:args.scenarios],
                                  args.warmup, price, args.out, repo=args.repo,
                                  expected_commit=args.expected_commit, timeout=args.timeout)
        finally:
            if hasattr(adapter, 'close'):
                await adapter.close()
    try:
        return asyncio.run(execute())
    except Exception as exc:
        _write(args.out, dict(side=args.side, ran=False, completed=False, reason=type(exc).__name__))
        print('NOT RUN: invalid inputs/source pin or missing authorized provider. See docs/MODEL_AB.md.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
