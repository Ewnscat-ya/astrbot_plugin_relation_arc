"""Export equivalent synthetic inputs through actual inject/assembly/history chains.

Run this candidate tool with --repo for each pinned baseline/candidate checkout.
No production data or provider call. Source hashes distinguish dirty code from HEAD.
"""
import argparse
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import prompt_runtime as rt


def scenarios():
    common = dict(text='用户原文', image=True, persona='ordinary')
    return {name: rt.scenario(**{**common, **options}, name=name) for name, options in {
        'empty_system': {'persona': ''},
        'persona_repeat_inject': {},
        'other_plugin_parts': {'persona': rt.PERSONAS['ordinary'] + '\n<OtherPlugin>x</OtherPlugin>'},
        'disable_reenable': {}, 'session_scope': {'session': True},
        'route_shown_eligible': {'session': True},
        'policy_exclusivity_on': {'session': True},
    }.items()}


async def export(repo, out_dir, side, expected_commit=None):
    source = rt.source_info(repo, expected_commit)
    plugin_type = rt.load_plugin(repo)
    out_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for name, spec in scenarios().items():
        with tempfile.TemporaryDirectory(prefix='relation-sample-') as directory:
            plugin = plugin_type(rt.SyntheticContext(directory))
            try:
                event = rt.configure(plugin, spec)
                req = rt.new_request(spec)
                record = dict(format_version=2, side=side, scenario=name, inputs=copy.deepcopy(spec),
                    source=source, host_version=rt.host_version(), snapshots=[])
                async def capture(phase):
                    await plugin.inject(event, req)
                    kind, scope = plugin._scope(event)
                    account = plugin.store.account(plugin._identity(event), kind, scope)
                    policy = plugin.store.active_binding_policy()
                    record['snapshots'].append(dict(phase=phase, request=await rt.snapshot(req),
                        state_inputs=dict(values=account['values'], scope_kind=kind,
                            enabled=plugin.config['llm_judgment_enabled'],
                            romance_policy=account['state']['romance_policy'],
                            romance_state=account['state']['romance_state'],
                            exclusivity=policy['exclusivity'], rebind_cooldown=policy['rebind_cooldown'])))
                await capture('initial')
                if name in ('persona_repeat_inject', 'other_plugin_parts'):
                    kind, scope = plugin._scope(event)
                    plugin.store.set_dimension(plugin._identity(event), kind, scope, 'trust', 432)
                    await capture('refreshed')
                elif name == 'disable_reenable':
                    plugin.config['llm_judgment_enabled'] = False
                    await capture('disabled')
                    plugin.config['llm_judgment_enabled'] = True
                    await capture('reenabled')
                elif name == 'route_shown_eligible':
                    rt.configure(plugin, {**spec, 'visible': True, 'eligible': True,
                        'values': {'trust': 850, 'comfort': 850, 'closeness': 850, 'resonance': 750}})
                    await capture('route_shown_eligible')
                elif name == 'policy_exclusivity_on':
                    plugin.store.activate_binding_policy({'exclusivity': 'scope', 'rebind_cooldown': 'off'})
                    await capture('exclusivity_on')
                record['generator_sha256'] = rt.digest({p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                                        for p in (Path(__file__), Path(rt.__file__))})
                (out_dir / f'{side}_full_request_{name}.json').write_text(
                    json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
                records.append(record)
            finally:
                await plugin.terminate()
    return records


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo', type=Path, required=True)
    p.add_argument('--expected-commit')
    p.add_argument('--side', choices=['baseline', 'candidate'], required=True)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    records = asyncio.run(export(a.repo.resolve(), a.out, a.side, a.expected_commit))
    print(f'Exported {len(records)} full-request scenarios; no model called.')


if __name__ == '__main__':
    main()
