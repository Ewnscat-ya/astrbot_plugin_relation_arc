"""Evidence-only prompt/label comparison, not a full third-party integration test.

Read a specified Favour Ultra source file without importing/executing it. Extract
only its literal regex and restricted prompt f-strings, fill synthetic values,
then exercise the selected Relation Arc source's real inject/judge hooks. The
reference plugin's database, lifecycle and live hook dispatch are NOT simulated.
"""
import argparse
import ast
import asyncio
import hashlib
import json
from pathlib import Path
import re
import tempfile
import prompt_runtime as rt


def _render(node, values):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return ''.join(_render(part, values) for part in node.values)
    if isinstance(node, ast.FormattedValue) and node.conversion == -1 and node.format_spec is None:
        key = ast.unparse(node.value)
        if key not in values:
            raise ValueError('unsupported reference template variable: ' + key)
        return str(values[key])
    raise ValueError('only literal strings and explicitly supplied template variables allowed')


def read_reference(path):
    data = path.read_bytes()
    tree = ast.parse(data.decode('utf-8-sig'))
    values = dict(mode_instruction='合成模式：按人设与边界判断，普通礼貌持平。', user_id='synthetic-user',
                  admin_status=False, current_favour=0, current_relationship='无', exclusive_db_text='无',
                  rel_context='', levels_rule='合成默认等级', limit_constraint_text='按配置边界判断。')
    values['self.max_favour_value'] = 100
    values.update({'self.favour_increase_min': 1, 'self.favour_increase_max': 3,
                   'self.favour_decrease_min': 1, 'self.favour_decrease_max': 3})
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = ast.unparse(node.targets[0])
        if target in ('static_prompt', 'dynamic_prompt'):
            # Restrict to the two reviewed templates, not earlier local variables.
            prefix = '<FavorabilityPlugin>' if target == 'static_prompt' else '<FavourContext>'
            if isinstance(node.value, ast.JoinedStr) and isinstance(node.value.values[0], ast.Constant) and node.value.values[0].value.startswith(prefix):
                found[target] = _render(node.value, values)
        if target == 'self.favour_pattern' and isinstance(node.value, ast.Call):
            if ast.unparse(node.value.func) != 're.compile':
                raise ValueError('unexpected reference regex constructor')
            found['regex'] = ast.literal_eval(node.value.args[0])
    if set(found) != {'static_prompt', 'dynamic_prompt', 'regex'}:
        raise ValueError('reference layout differs; inspect this version before adapting')
    found['sha256'] = hashlib.sha256(data).hexdigest()
    return found


async def probe(repo, reference_path, out, expected_commit=None):
    from astrbot.core.agent.message import TextPart
    from astrbot.core.provider.entities import LLMResponse
    reference = read_reference(reference_path)
    pattern = re.compile(reference['regex'], re.IGNORECASE)
    plugin_type = rt.load_plugin(repo)
    records = []
    for persona in rt.PERSONAS:
        for mode in ('relation_only', 'favour_only', 'favour_before_relation', 'favour_after_relation'):
            spec = rt.scenario(persona=persona, name=persona + '-' + mode)
            with tempfile.TemporaryDirectory(prefix='relation-compat-') as directory:
                plugin = plugin_type(rt.SyntheticContext(directory))
                try:
                    event = rt.configure(plugin, spec)
                    req = rt.new_request(spec)
                    def favour_inject():
                        req.system_prompt += '\n\n' + reference['static_prompt']
                        req.extra_user_content_parts.append(TextPart(text=reference['dynamic_prompt']).mark_as_temp())
                    if mode in ('favour_only', 'favour_before_relation'):
                        favour_inject()
                    if mode != 'favour_only':
                        await plugin.inject(event, req)
                    if mode == 'favour_after_relation':
                        favour_inject()
                    snap = await rt.snapshot(req)
                    assert req.system_prompt.startswith(spec['persona'])
                    if mode != 'relation_only':
                        assert reference['static_prompt'] in req.system_prompt
                    relation_tag = ('<relation_judgment>{"schema_version":3,"fact_effects":[],'
                                '"relationship_proposal":null,"interaction_safety_proposal":null}'
                                '</relation_judgment>')
                    if mode == 'favour_only':
                        assert pattern.sub('', '合成正文\n[好感度 持平]').strip() == '合成正文'
                    elif mode == 'relation_only':
                        response = LLMResponse(role='assistant', completion_text=relation_tag + '\n合成正文')
                        await plugin.judge(event, response)
                        assert response.completion_text.strip() == '合成正文'
                    else:
                        combined = relation_tag + '\n合成正文\n[好感度 持平]'
                        response = LLMResponse(role='assistant', completion_text=combined)
                        await plugin.judge(event, response)
                        assert pattern.search(response.completion_text), 'Relation Arc removed the other label'
                        assert pattern.sub('', response.completion_text).strip() == '合成正文'
                        second = LLMResponse(role='assistant', completion_text=pattern.sub('', combined))
                        await plugin.judge(event, second)
                        assert second.completion_text.strip() == '合成正文'
                    records.append(dict(persona=persona, mode=mode, request=snap,
                        synthetic_label_cleaning=True,
                        both_cleaning_orders_tested=mode.startswith('favour_') and mode != 'favour_only',
                        persona_verbatim_preserved=True,
                        model_behavior='NOT TESTED'))
                finally:
                    await plugin.terminate()
    result = dict(source=rt.source_info(repo, expected_commit), host_version=rt.host_version(),
        reference_sha256=reference['sha256'], reference_scope='extracted prompt templates and favour regex only',
        live_hooks_and_reference_database='NOT TESTED', feedback_plugin_identity='UNKNOWN', records=records)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo', type=Path, required=True)
    p.add_argument('--expected-commit')
    p.add_argument('--reference-source', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    result = asyncio.run(probe(a.repo.resolve(), a.reference_source, a.out, a.expected_commit))
    print(f"Checked {len(result['records'])} synthetic prompt/label combinations; model behavior remains untested.")


if __name__ == '__main__':
    main()
