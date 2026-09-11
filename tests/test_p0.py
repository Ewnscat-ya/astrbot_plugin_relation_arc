"""Host-free regression harness; executes real methods, not AstrBot integration."""
import ast
import json
import logging
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
from astrbot_plugin_relation_arc.relation_engine import DIMENSIONS, aggregate_effects, apply_delta
from astrbot_plugin_relation_arc.relation_protocol import BLOCK, parse_response
from astrbot_plugin_relation_arc.relation_store import RelationStore
from astrbot_plugin_relation_arc.relationship_types import get_type, projected_eligibility
from astrbot_plugin_relation_arc.config_manager import PluginConfigManager


def load_methods():
    tree = ast.parse((ROOT / 'main.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'RelationArc')
    cls.bases = []
    cls.decorator_list = []
    for method in cls.body:
        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            method.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), cls], type_ignores=[])
    namespace = dict(globals(), logger=logging.getLogger('p0'), Plain=Plain)
    exec(compile(ast.fix_missing_locations(module), str(ROOT / 'main.py'), 'exec'), namespace)
    return namespace['RelationArc']


class Plain:
    def __init__(self, text): self.text = text


class Event:
    message_str = 'hello'
    unified_msg_origin = 'private:1'
    message_obj = SimpleNamespace(message_id='p0-message')
    def get_platform_id(self): return 'test'
    def get_sender_id(self): return 'user'
    def get_group_id(self): return ''


def verdict(**fields):
    return '<relation_judgment>' + json.dumps(dict(schema_version=3, fact_effects=[], **fields)) + '</relation_judgment>visible'


def malformed_payloads():
    base = {'schema_version':3, 'fact_effects':[]}
    for value in ([], {}, None, True, 3.0, '3'):
        yield {**base, 'schema_version':value}
    for field in ('origin', 'mutuality', 'type_id', 'action'):
        for value in ([], {}, None, True, 7):
            proposal = {'action':'bind', 'type_id':'friend', 'origin':'mutual_dialogue', 'mutuality':'clear'}
            proposal[field] = value
            yield {**base, 'relationship_proposal':proposal}
    for field in ('level', 'reason_code'):
        for value in ([], {}, None, True, 7):
            safety = {'level':'pause_intimacy', 'reason_code':'boundary_pressure'}
            safety[field] = value
            yield {**base, 'interaction_safety_proposal':safety}
    yield {**base, 'fact_effects':[{'effects':{'trust':True}}]}


class ParserP0Tests(unittest.TestCase):
    def test_malformed_fields_never_crash_or_settle(self):
        for payload in malformed_payloads():
            with self.subTest(payload=payload):
                result = parse_response('<relation_judgment>'+json.dumps(payload)+'</relation_judgment>visible', '')
                self.assertEqual('visible', result.clean_text)
                self.assertEqual([], result.effects)
                self.assertIsNone(result.proposal)
                self.assertIsNone(result.safety_proposal)

    def test_nontext_input_and_excessive_json_are_safe(self):
        for value in (None, [], {}, 42, True):
            result = parse_response(value, '')
            self.assertEqual('', result.clean_text)
            self.assertEqual('invalid_text', result.error)
        text = '<relation_judgment>'+('['*1500)+(']'*1500)+'</relation_judgment>visible'
        self.assertEqual('visible', parse_response(text, '').clean_text)
        self.assertIn(parse_response(text, '').error, {'invalid_json', 'invalid_schema'})

    def test_unrelated_bare_malformed_schema_is_not_stripped(self):
        for value in ([], {}, True, 3.0):
            text = json.dumps({'schema_version':value, 'fact_effects':[]})+' visible'
            self.assertEqual(text, parse_response(text, '').clean_text)


class MigrationP0Tests(unittest.TestCase):
    def test_migration_respects_active_exclusivity_and_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            self.assertEqual('migrated', store.migrate_confirmed_binding(identity='test:a', scope_kind='global', scope_id=''))
            self.assertEqual('binding_rejected:exclusive', store.migrate_confirmed_binding(identity='test:b', scope_kind='global', scope_id=''))
            self.assertEqual('binding_rejected:exclusive', store.migrate_confirmed_binding(identity='test:a', scope_kind='global', scope_id='', type_key='romantic_partner'))
            self.assertEqual('already_migrated', store.migrate_confirmed_binding(identity='test:a', scope_kind='global', scope_id=''))
            self.assertEqual('migrated', store.migrate_confirmed_binding(identity='test:b', scope_kind='session', scope_id='room'))
            active = store.active_bindings_for('test:a', 'global', '')
            self.assertTrue(store.end_binding(active[0]['binding_id'], 'admin'))
            self.assertEqual('migrated', store.migrate_confirmed_binding(identity='test:b', scope_kind='global', scope_id=''))
            self.assertEqual(2, len(store.list_bindings(status='active')))


class JudgeP0Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plugin = object.__new__(load_methods())
        self.plugin.store = RelationStore(Path(self.temp.name) / 'store')
        self.plugin.config = PluginConfigManager(Path(self.temp.name) / 'plugin', Path(self.temp.name)).load_or_create()
        for key in DIMENSIONS:
            self.plugin.store.set_dimension('test:user', 'global', '', key, 900)
        self.plugin.store.set_state('test:user', 'global', '', romance_policy='shown', romance_state='eligible')

    async def test_malformed_protocol_is_stripped_in_both_output_paths(self):
        before = self.plugin.store.account('test:user')
        for payload in malformed_payloads():
            for chain in (False, True):
                with self.subTest(payload=payload, chain=chain):
                    text = '<relation_judgment>'+json.dumps(payload)+'</relation_judgment>visible'
                    marker = object()
                    response = SimpleNamespace(completion_text=text, result_chain=SimpleNamespace(chain=[marker, Plain(text)]) if chain else None)
                    await self.plugin.judge(Event(), response)
                    self.assertEqual('visible', response.result_chain.chain[1].text if chain else response.completion_text)
                    if chain: self.assertIs(marker, response.result_chain.chain[0])
                    self.assertEqual(before, self.plugin.store.account('test:user'))
        self.assertEqual([], self.plugin.store.list_bindings(status='active'))
        self.assertIsNone(self.plugin.store.active_timed_safety('test:user','global',''))

    async def test_nontext_completion_is_safe(self):
        for value in ([], {}, 42, True):
            response = SimpleNamespace(completion_text=value)
            await self.plugin.judge(Event(), response)
            self.assertEqual('', response.completion_text)

    async def test_malformed_plain_type_does_not_prevent_stripping(self):
        response = SimpleNamespace(completion_text=None, result_chain=SimpleNamespace(chain=[Plain({'unexpected':1}), Plain(verdict())]))
        await self.plugin.judge(Event(), response)
        self.assertEqual(['visible'], [p.text for p in response.result_chain.chain])

    async def test_disabled_settlement_strips_without_writes(self):
        payload = verdict(relationship_proposal={'action':'bind','type_id':'romantic_partner','origin':'mutual_dialogue','mutuality':'clear'}, interaction_safety_proposal={'level':'pause_intimacy','reason_code':'boundary_pressure'})
        for gate in ('llm_judgment_enabled', 'enabled', 'private_enabled'):
            for chain in (False, True):
                with self.subTest(gate=gate, chain=chain):
                    self.plugin.config[gate] = False
                    before = self.plugin.store.account('test:user')
                    health = self.plugin.store.protocol_health_summary()
                    response = SimpleNamespace(completion_text=payload, result_chain=SimpleNamespace(chain=[Plain(payload)]) if chain else None)
                    await self.plugin.judge(Event(), response)
                    self.assertEqual('visible', response.result_chain.chain[0].text if chain else response.completion_text)
                    self.assertEqual(before, self.plugin.store.account('test:user'))
                    self.assertEqual([], self.plugin.store.list_bindings(status='active'))
                    self.assertIsNone(self.plugin.store.active_timed_safety('test:user','global',''))
                    self.assertEqual(health, self.plugin.store.protocol_health_summary())
                    self.plugin.config[gate] = True

    async def test_same_turn_safety_blocks_binding_and_romance(self):
        self.plugin.config['interaction_safety']['llm_mode'] = 'llm_auto'
        payload = json.loads(verdict().split('>', 1)[1].split('<')[0])
        payload.update(fact_effects=[{'effects': {'romance_interest': 5}}], relationship_proposal={'action':'bind','type_id':'romantic_partner','origin':'mutual_dialogue','mutuality':'clear'}, interaction_safety_proposal={'level':'pause_intimacy','reason_code':'boundary_pressure'})
        response = SimpleNamespace(completion_text='<relation_judgment>'+json.dumps(payload)+'</relation_judgment>visible')
        await self.plugin.judge(Event(), response)
        self.assertEqual('visible', response.completion_text)
        self.assertEqual([], self.plugin.store.list_bindings(status='active'))
        self.assertEqual(900, self.plugin.store.account('test:user')['values']['romance_interest'])
        self.assertEqual('pause_intimacy', self.plugin.store.effective_interaction_safety('test:user','global',''))
