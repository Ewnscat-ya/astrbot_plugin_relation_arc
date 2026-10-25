"""Regression: equal text is not evidence that a system segment is ours."""
import copy
from dataclasses import asdict, replace
import tempfile
import unittest

from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import TextPart
from test_main import FakeContext, FakeEvent
from astrbot_plugin_relation_arc.main import RelationArc
from astrbot_plugin_relation_arc.prompts import fixed_rules_block


class AnchoredOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.plugin = RelationArc(FakeContext(self.temp.name))

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def test_identical_literal_before_own_block_survives_later_append(self):
        persona = '引用原文\n\n' + fixed_rules_block() + '\n引用结束。'
        request = ProviderRequest(prompt='hi', system_prompt=persona)
        await self.plugin.inject(FakeEvent(), request)
        request.system_prompt += '\n其他插件的尾部规则。'
        self.plugin.config['llm_judgment_enabled'] = False
        await self.plugin.inject(FakeEvent(), request)
        self.assertEqual(persona + '\n其他插件的尾部规则。', request.system_prompt)

    async def test_identical_literal_appended_after_own_block_is_not_removed(self):
        request = ProviderRequest(prompt='hi', system_prompt='persona')
        await self.plugin.inject(FakeEvent(), request)
        suffix = '\n其他插件引用：\n\n' + fixed_rules_block()
        request.system_prompt += suffix
        self.plugin.config['llm_judgment_enabled'] = False
        await self.plugin.inject(FakeEvent(), request)
        self.assertEqual('persona' + suffix, request.system_prompt)

    async def test_prepend_append_and_copies_preserve_foreign_parts(self):
        for copier in (copy.copy, copy.deepcopy, replace,
                       lambda r: ProviderRequest(**asdict(r))):
            with self.subTest(copy=copier.__name__):
                self.plugin.config['llm_judgment_enabled'] = True
                request = ProviderRequest(prompt='hi', system_prompt='persona\n\n\n')
                request.extra_user_content_parts.append(TextPart(text='foreign'))
                await self.plugin.inject(FakeEvent(), request)
                request = copier(request)
                request.system_prompt = '前置\n' + request.system_prompt + '\n后置'
                await self.plugin.inject(FakeEvent(), request)
                self.assertEqual(3, len(request.extra_user_content_parts))
                self.plugin.config['llm_judgment_enabled'] = False
                await self.plugin.inject(FakeEvent(), request)
                self.assertEqual('前置\npersona\n\n\n\n后置', request.system_prompt)
                self.assertEqual(['foreign'], [p.text for p in request.extra_user_content_parts])

    async def test_external_system_replacement_is_not_replaced_by_saved_persona(self):
        request = ProviderRequest(prompt='hi', system_prompt='old persona')
        await self.plugin.inject(FakeEvent(), request)
        request.system_prompt = 'new persona\n\n'
        self.plugin.config['llm_judgment_enabled'] = False
        await self.plugin.inject(FakeEvent(), request)
        self.assertEqual('new persona\n\n', request.system_prompt)

    async def test_ambiguous_duplicate_anchor_is_preserved_without_stacking(self):
        request = ProviderRequest(prompt='hi', system_prompt='persona')
        await self.plugin.inject(FakeEvent(), request)
        request.system_prompt += '\nCopy: ' + request.system_prompt
        original = request.system_prompt
        await self.plugin.inject(FakeEvent(), request)
        self.assertEqual(original, request.system_prompt)
        self.assertEqual(2, len(request.extra_user_content_parts))

    async def test_metadata_is_not_serialized_to_provider_or_history(self):
        from astrbot.core.agent.message import Message, dump_messages_with_checkpoints
        request = ProviderRequest(prompt='hi', system_prompt='persona')
        await self.plugin.inject(FakeEvent(), request)
        assembled = await request.assemble_context()
        self.assertNotIn('_relation_arc_own_injection', str(assembled))
        self.assertNotIn('_relation_arc_own_injection', str(asdict(request)))
        self.assertNotIn('RelationArcDynamicContext', str(
            dump_messages_with_checkpoints([Message.model_validate(assembled)])))
