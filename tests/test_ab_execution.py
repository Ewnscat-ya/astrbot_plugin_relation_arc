"""Full provider inputs and negative execution cases; no network or paid calls."""
import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import model_ab_test as ab
import prompt_runtime as rt
from astrbot.api.provider import ProviderRequest
from astrbot.core.provider.entities import LLMResponse, TokenUsage
from astrbot.core.provider.provider import Provider


class CapturingProvider(Provider):
    def __init__(self):
        super().__init__({'custom_extra_body': {'temperature': 0.2}, 'key': ['never-export-me']}, {})
        self.set_model('synthetic-model')
        self.received = []

    def get_current_key(self): return 'offline'
    def set_key(self, key): pass
    async def get_models(self): return ['synthetic-model']
    async def text_chat(self, **kwargs):
        self.received.append(copy.deepcopy(kwargs))
        return LLMResponse(role='assistant', completion_text='offline', usage=TokenUsage(8, 2, 3))


class ProviderInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.plugin = rt.load_plugin(ROOT)(rt.SyntheticContext(self.temp.name))

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def request(self, image=False):
        spec = rt.scenario(persona='rules', image=image, visible=True, eligible=True)
        event = rt.configure(self.plugin, spec)
        req = rt.new_request(spec)
        await self.plugin.inject(event, req)
        return spec, req

    async def test_host_adapter_preserves_all_request_fields(self):
        spec, req = await self.request(image=True)
        req.audio_urls = ['data:audio/wav;base64,c3ludGhldGlj']
        req.model, req.session_id = 'explicit-synthetic-model', 'isolated'
        provider = CapturingProvider()
        context = SimpleNamespace(get_provider_by_id=lambda name: provider if name == 'configured' else None)
        adapter = ab.HostAdapter.from_context(context, 'configured', channel='test')
        await adapter.complete(req)
        actual = provider.received[0]
        for key in ('prompt', 'contexts', 'system_prompt', 'image_urls', 'audio_urls', 'model', 'session_id', 'func_tool', 'tool_calls_result'):
            self.assertEqual(getattr(req, key), actual[key], key)
        self.assertEqual([p.text for p in req.extra_user_content_parts], [p.text for p in actual['extra_user_content_parts']])
        self.assertIsNot(req.contexts, actual['contexts'])
        self.assertTrue(actual['system_prompt'].startswith(spec['persona']))
        self.assertNotIn('never-export-me', str(adapter.metadata()))

    async def test_real_openai_provider_payload_to_sdk_boundary(self):
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
        from openai.types.chat import ChatCompletion
        spec, req = await self.request()
        provider = ProviderOpenAIOfficial(dict(type='openai_chat_completion', key=['offline-placeholder'],
            api_base='https://offline.invalid/v1', model='synthetic-model', custom_extra_body={'temperature': 0.2}), {})
        completion = ChatCompletion(id='offline', object='chat.completion', created=0, model='synthetic-model',
            choices=[dict(index=0, finish_reason='stop', message=dict(role='assistant', content='offline'))],
            usage=dict(prompt_tokens=12, completion_tokens=3, total_tokens=15,
                       prompt_cache_hit_tokens=7, prompt_cache_miss_tokens=5))
        try:
            with patch.object(provider.client.chat.completions, 'create', new=AsyncMock(return_value=completion)) as api:
                result = await ab.HostAdapter(provider, channel='offline-sdk-spy').complete(req)
                kwargs = api.call_args.kwargs
                messages = kwargs['messages']
                self.assertEqual(req.system_prompt, messages[0]['content'])
                self.assertEqual(spec['history'], messages[1:-1])
                self.assertEqual([req.prompt] + [p.text for p in req.extra_user_content_parts],
                                 [p['text'] for p in messages[-1]['content']])
                self.assertEqual({'temperature': 0.2}, kwargs['extra_body'])
                self.assertEqual('synthetic-model', kwargs['model'])
                self.assertEqual(7, result['usage']['cache_hit_tokens'])
                self.assertEqual(5, result['usage']['cache_miss_tokens'])
        finally:
            await provider.client.close()

    async def test_oracle_rejects_missing_dynamic_parts(self):
        _, req = await self.request()
        req.extra_user_content_parts = []
        with self.assertRaises(AssertionError):
            await ab.FakeAdapter().complete(req)

    async def test_oracle_checks_full_part_order_and_persistence(self):
        _, req = await self.request(image=True)
        adapter = ab.FakeAdapter()
        await adapter.complete(req)
        snapshot = adapter.received[0]
        self.assertIn('RelationArcDynamicContext', str(snapshot['messages']))
        self.assertNotIn('RelationArcDynamicContext', str(snapshot['persisted_current_message']))
        self.assertEqual('image_url', snapshot['messages'][-1]['content'][-1]['type'])

    def test_abstract_or_missing_provider_is_not_constructed(self):
        with self.assertRaises(ValueError):
            ab.HostAdapter.from_context(SimpleNamespace(get_provider_by_id=lambda _: None), 'absent', channel='test')

    async def test_explicit_factory_builds_concrete_provider_without_calling_it(self):
        import provider_factory_openai as factory
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                factory.make()
        supplied = dict(RELATION_AB_API_KEY='offline-placeholder', RELATION_AB_BASE_URL='https://offline.invalid/v1',
                        RELATION_AB_MODEL='synthetic-model', RELATION_AB_CHANNEL='synthetic-channel',
                        RELATION_AB_PARAMETERS_JSON='{"temperature":0.25,"max_tokens":128}')
        with patch.dict(os.environ, supplied, clear=True):
            adapter = factory.make()
            try:
                self.assertIsInstance(adapter, ab.HostAdapter)
                self.assertEqual('synthetic-model', adapter.metadata()['model'])
                self.assertEqual(0.25, adapter.metadata()['generation']['temperature'])
            finally:
                await adapter.close()

    async def test_oracle_rejects_swapped_plugin_parts(self):
        _, req = await self.request()
        req.extra_user_content_parts[-2:] = reversed(req.extra_user_content_parts[-2:])
        with self.assertRaises(AssertionError):
            await ab.FakeAdapter().complete(req)


class UsageTests(unittest.TestCase):
    def test_host_usage_keeps_cache_unknown(self):
        value = ab.normalize_usage(LLMResponse(role='assistant', usage=TokenUsage(8, 2, 3)))
        self.assertEqual(10, value['input_tokens'])
        self.assertEqual(3, value['output_tokens'])
        self.assertIsNone(value['cache_hit_tokens'])

    def test_missing_and_negative_usage_do_not_become_zero(self):
        for usage in (None, {}, {'input_tokens': -1, 'output_tokens': True}):
            result = ab.normalize_usage(SimpleNamespace(usage=usage))
            self.assertIsNone(result['input_tokens'])
            self.assertIsNone(result['cache_miss_tokens'])

    def test_cache_rate_uses_sums_and_partial_coverage_is_unknown(self):
        records = [dict(error=None, protocol_valid=True, cache_hit_tokens=9, cache_miss_tokens=1),
                   dict(error=None, protocol_valid=True, cache_hit_tokens=0, cache_miss_tokens=90)]
        self.assertEqual(0.09, ab._summary({'records': records})['cache_hit_rate'])
        records[1]['cache_miss_tokens'] = None
        self.assertIsNone(ab._summary({'records': records})['cache_hit_rate'])

    def test_zero_denominator_and_empty_are_unknown(self):
        for records in ([], [dict(error=None, cache_hit_tokens=0, cache_miss_tokens=0)]):
            self.assertIsNone(ab._summary({'records': records})['cache_hit_rate'])

    def test_cost_requires_complete_fields_and_known_price_basis(self):
        price = dict(mode='cache', source='synthetic price', currency='test',
                     cache_hit_per_1k=1, cache_miss_per_1k=2, output_per_1k=3)
        record = dict(input_tokens=100, output_tokens=20, cache_hit_tokens=60, cache_miss_tokens=40)
        self.assertAlmostEqual(0.2, ab._cost(record, price))
        record['output_tokens'] = None
        self.assertIsNone(ab._cost(record, price))
        self.assertIsNone(ab._cost({}, {}))


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name) / 'out.json'

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def run_tool(self, adapter, **kwargs):
        args = dict(side='candidate', adapter=adapter, repeats=1,
                    scenarios=[rt.scenario()], warmup=0, price=None, out_path=self.out, repo=ROOT)
        args.update(kwargs)
        code = await ab.run_side(**args)
        return code, json.loads(self.out.read_text(encoding='utf-8'))

    async def test_full_requests_fixed_history_and_warmups_are_persisted(self):
        adapter = ab.FakeAdapter()
        code, data = await self.run_tool(adapter, repeats=2, warmup=1)
        self.assertEqual(0, code, data)
        self.assertEqual(4, len(data['records']))
        self.assertEqual(2, data['summary']['measured'])
        self.assertEqual([rt.PRESET_HISTORY] * 4, [r['contexts'] for r in adapter.received])
        self.assertTrue(data['offline'])
        self.assertIsNone(data['summary']['total_cost'])
        self.assertEqual(rt.source_info(ROOT)['source_sha256'], data['source']['source_sha256'])

    async def test_failed_warmup_returns_nonzero_and_is_recorded(self):
        class Fails(ab.FakeAdapter):
            async def complete(self, req): raise RuntimeError('secret-in-error')
        code, data = await self.run_tool(Fails(), warmup=2)
        self.assertEqual(2, code)
        self.assertEqual(1, len(data['records']))
        self.assertEqual('warmup', data['records'][0]['phase'])
        self.assertNotIn('secret-in-error', self.out.read_text(encoding='utf-8'))

    async def test_empty_and_wrong_source_never_call_provider(self):
        for kwargs in (dict(scenarios=[]), dict(expected_commit='0' * 40)):
            adapter = ab.FakeAdapter()
            code, data = await self.run_tool(adapter, **kwargs)
            self.assertEqual(2, code)
            self.assertFalse(data['ran'])
            self.assertEqual([], adapter.received)

    async def test_zero_protocol_valid_and_timeout_fail(self):
        class Invalid(ab.FakeAdapter):
            async def complete(self, req): return dict(text='plain only', usage={})
        class Slow(ab.FakeAdapter):
            async def complete(self, req): await asyncio.sleep(1)
        for adapter, kwargs in [(Invalid(), {}), (Slow(), dict(timeout=0.001))]:
            code, data = await self.run_tool(adapter, **kwargs)
            self.assertEqual(2, code)
            self.assertEqual(1, len(data['records']))

    async def test_two_real_source_checkouts_are_loaded_and_compared(self):
        base = Path(self.temp.name) / 'baseline'
        subprocess.run(['git', 'clone', '--quiet', '--shared', '--no-checkout', str(ROOT), str(base)], check=True)
        subprocess.run(['git', '-C', str(base), 'checkout', '--quiet', '--detach', rt.BASELINE], check=True)
        specs = [rt.scenario(persona='rules')]
        candidate = Path(self.temp.name) / 'candidate.json'
        self.assertEqual(0, await ab.run_side('baseline', ab.FakeAdapter(), 1, specs, 0, None, self.out,
                                            repo=base, expected_commit=rt.BASELINE))
        self.assertEqual(0, await ab.run_side('candidate', ab.FakeAdapter(), 1, specs, 0, None, candidate, repo=ROOT))
        self.assertEqual(0, ab.compare(self.out, candidate))
        old = json.loads(self.out.read_text(encoding='utf-8'))
        new = json.loads(candidate.read_text(encoding='utf-8'))
        self.assertEqual(specs[0]['persona'], old['records'][0]['request']['system_prompt'])
        self.assertIn('RelationArcTurnNote', str(new['records'][0]['request']['parts']))
        # A matching truncation on both sides is still an incomplete run.
        original_base, original_candidate = copy.deepcopy(old), copy.deepcopy(new)
        old['records'] = []
        new['records'] = []
        ab._write(self.out, old)
        ab._write(candidate, new)
        self.assertEqual(2, ab.compare(self.out, candidate))
        ab._write(self.out, original_base)
        new = original_candidate
        new['provider']['model'] = 'mismatch'
        ab._write(candidate, new)
        self.assertEqual(2, ab.compare(self.out, candidate))
        import prompt_samples_dump_full as samples
        import prompt_eval
        exports = Path(self.temp.name) / 'samples'
        baseline_records = await samples.export(base, exports, 'baseline', rt.BASELINE)
        candidate_records = await samples.export(ROOT, exports, 'candidate')
        report = prompt_eval.evaluate(exports, exports)
        self.assertEqual(7, len(baseline_records))
        self.assertEqual(13, len(report['rows']))
        disabled = next(r for r in report['rows'] if r['phase'] == 'disabled')
        self.assertEqual(0, disabled['candidate']['total_plugin_chars'])
        self.assertGreater(disabled['baseline']['total_plugin_chars'], 0)
        self.assertNotEqual(report['baseline_source']['source_sha256'], report['candidate_source']['source_sha256'])
        tamper = exports / 'candidate_full_request_empty_system.json'
        edited = json.loads(tamper.read_text(encoding='utf-8'))
        edited['inputs']['history'] = []
        ab._write(tamper, edited)
        with self.assertRaises(ValueError):
            prompt_eval.evaluate(exports, exports)

    def test_atomic_record_writer_retries_transient_sharing_violation(self):
        original = Path.replace
        attempts = []
        def replace(path, target):
            attempts.append(path)
            if len(attempts) < 3:
                raise PermissionError('synthetic sharing violation')
            return original(path, target)
        with patch.object(Path, 'replace', replace):
            ab._write(self.out, {'records': [1]})
        self.assertEqual(3, len(attempts))
        self.assertEqual({'records': [1]}, json.loads(self.out.read_text(encoding='utf-8')))

    def test_plan_and_missing_authorization_never_import_factory(self):
        factory = Path(self.temp.name) / 'factory.py'
        factory.write_text('raise RuntimeError("must not import")', encoding='utf-8')
        command = [sys.executable, '-B', str(ROOT / 'tools/model_ab_test.py'), 'run', '--side', 'candidate',
                   '--repo', str(ROOT), '--out', str(self.out), '--provider-factory', str(factory) + ':make']
        plan = subprocess.run(command + ['--plan-only'], capture_output=True, text=True)
        self.assertEqual(0, plan.returncode, plan.stderr)
        self.assertFalse(json.loads(self.out.read_text())['ran'])
        denied = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(2, denied.returncode)
        self.assertEqual('ValueError', json.loads(self.out.read_text())['reason'])
