"""Compare equivalent full-request exports. Characters are not tokens/cache/cost."""
import argparse
import hashlib
import json
import os
from pathlib import Path


def evaluate(baseline_dir, candidate_dir):
    def read(directory, side):
        return {p.name.split('_full_request_', 1)[1]: json.loads(p.read_text(encoding='utf-8'))
                for p in Path(directory).glob(side + '_full_request_*.json')}
    old, new = read(baseline_dir, 'baseline'), read(candidate_dir, 'candidate')
    if not old or old.keys() != new.keys():
        raise ValueError('both sides need the identical nonempty scenario matrix')
    for records in (old, new):
        reference = next(iter(records.values()))
        if any(any(r[key] != reference[key] for key in ('source', 'host_version', 'generator_sha256'))
               for r in records.values()):
            raise ValueError('mixed stale evidence or source versions')
    if next(iter(old.values()))['generator_sha256'] != next(iter(new.values()))['generator_sha256']:
        raise ValueError('different evidence generators')
    rows, groups = [], {}
    for name in sorted(old):
        b, c = old[name], new[name]
        if b['inputs'] != c['inputs'] or b['host_version'] != c['host_version']:
            raise ValueError('incomparable source inputs or host versions')
        if [s['phase'] for s in b['snapshots']] != [s['phase'] for s in c['snapshots']]:
            raise ValueError('different lifecycle phases')
        for bs, cs in zip(b['snapshots'], c['snapshots']):
            if bs['state_inputs'] != cs['state_inputs']:
                raise ValueError('different effective scenario state')
            row = dict(scenario=c['scenario'], phase=cs['phase'])
            for side, item in [('baseline', bs), ('candidate', cs)]:
                req, inputs = item['request'], b['inputs']
                persona, foreign = inputs['persona'], inputs['foreign_parts']
                if not req['system_prompt'].startswith(persona):
                    raise ValueError('persona was not preserved')
                parts = req['parts']
                if [p['text'] for p in parts[:len(foreign)]] != foreign:
                    raise ValueError('foreign components changed')
                fixed = req['system_prompt'][len(persona):]
                dynamic_chars = sum(len(p['text']) for p in parts[len(foreign):])
                row[side] = dict(fixed_chars=len(fixed), temporary_chars=dynamic_chars,
                    total_plugin_chars=len(fixed) + dynamic_chars,
                    full_request_chars=len(json.dumps(req['messages'], ensure_ascii=False)),
                    fixed_sha256=hashlib.sha256(fixed.encode()).hexdigest())
                # Disabled phases intentionally remove the fixed rules; they
                # must not shorten the cache prefix of enabled requests.
                key = persona + (' [disabled]' if not item['state_inputs']['enabled'] else '')
                groups.setdefault(key, {'baseline': [], 'candidate': []})[side].append(
                    json.dumps(req['messages'], ensure_ascii=False, separators=(',', ':')))
                persisted = str(req['persisted_current_message'])
                if 'RelationArcDynamicContext' in persisted or 'RelationArcTurnNote' in persisted:
                    raise ValueError('temporary instruction persisted')
            rows.append(row)
    prefixes = [dict(persona_sha256=hashlib.sha256(persona.encode()).hexdigest(),
                     samples=len(data['baseline']),
                     baseline_chars=len(os.path.commonprefix(data['baseline'])),
                     candidate_chars=len(os.path.commonprefix(data['candidate'])))
                for persona, data in groups.items() if len(data['baseline']) > 1]
    return dict(format_version=2, unit='characters; not tokens/cache/cost', rows=rows,
                stable_serialized_request_prefixes=prefixes,
                baseline_source=next(iter(old.values()))['source'],
                candidate_source=next(iter(new.values()))['source'],
                host_version=next(iter(new.values()))['host_version'])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline-dir', type=Path, required=True)
    p.add_argument('--candidate-dir', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    result = evaluate(a.baseline_dir, a.candidate_dir)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"Compared {len(result['rows'])} paired lifecycle requests.")


if __name__ == '__main__':
    main()
