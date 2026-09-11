from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_relation_arc.main import RelationArc
from astrbot_plugin_relation_arc.relation_engine import DEFAULT_VALUES
from astrbot.api.provider import ProviderRequest
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain


class FakeContext:
    def __init__(self, directory: str):
        self.directory = directory
        self.apis = []

    def get_config(self):
        return {"data": self.directory, "admins_id": ["admin"]}

    def register_web_api(self, *args):
        self.apis.append(args)


class FakeEvent:
    unified_msg_origin = "friend:1"
    message_str = "谢谢你愿意等我"
    message_obj = type("Message", (), {"message_id": "message-1"})()

    def __init__(self, user_id="user-1", group_id="", wake=False, outline=""):
        self.user_id = user_id
        self.group_id = group_id
        self.wake = wake
        self.outline = outline

    def get_platform_id(self):
        return "qq-adapter"

    def get_sender_id(self):
        return self.user_id

    def get_group_id(self):
        return self.group_id

    def get_self_id(self):
        return "bot-1"

    def is_wake_up(self):
        return self.wake

    def get_message_outline(self):
        return self.outline

    def plain_result(self, text):
        return text


class FakeResponse:
    def __init__(self, text="", result_chain=None):
        self.completion_text = text
        self.result_chain = result_chain


class RelationArcMainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.context = FakeContext(self.temp.name)
        self.plugin = RelationArc(self.context)

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def test_injection_uses_favour_style_dynamic_then_contract(self):
        event = FakeEvent()
        req = ProviderRequest(prompt="hello", system_prompt="persona")
        await self.plugin.inject(event, req)
        parts = req.extra_user_content_parts
        self.assertEqual(2, len(parts))
        self.assertIn("RelationArcDynamicContext", parts[0].text)
        self.assertIn("RelationArcOutputContract", parts[1].text)
        self.assertIn("第一行", parts[1].text)
        self.assertEqual("persona", req.system_prompt)

    async def test_response_is_stripped_and_applied(self):
        event = FakeEvent()
        response = FakeResponse(
            "当然。<relation_judgment>{\"schema_version\":1,\"fact_effects\":[{\"evidence\":\"谢谢你愿意等我\",\"reason\":\"尊重了交流节奏\",\"effects\":{\"trust\":4,\"comfort\":2}}]}</relation_judgment>"
        )
        await self.plugin.judge(event, response)
        self.assertEqual("当然。", response.completion_text)
        values = self.plugin.store.account("qq-adapter:user-1")["values"]
        self.assertEqual(404, values["trust"])
        self.assertEqual(452, values["comfort"])

    async def test_scope_mode_switch_does_not_leak_binding_into_other_scope(self):
        event=FakeEvent(); identity=self.plugin._identity(event)
        self.plugin.store.migrate_confirmed_binding(identity=identity,scope_kind="global",scope_id="")
        self.plugin.config["is_global_relation"]=False
        req=ProviderRequest(prompt="hello",system_prompt="persona")
        await self.plugin.inject(event,req)
        text="\n".join(part.text for part in req.extra_user_content_parts)
        self.assertIn("当前正式关系：无",text)
        self.assertEqual([],self.plugin.store.active_bindings_for(identity,"session",str(event.unified_msg_origin)))
        self.assertEqual(1,len(self.plugin.store.active_bindings_for(identity,"global","")))

    async def test_b3_injection_only_shows_current_scope_own_binding(self):
        event=FakeEvent()
        scope_kind,scope_id=self.plugin._scope(event); identity=self.plugin._identity(event)
        self.plugin.store.migrate_confirmed_binding(identity=identity,scope_kind=scope_kind,scope_id=scope_id)
        self.plugin.store.migrate_confirmed_binding(identity="qq-adapter:other",scope_kind=scope_kind,scope_id=scope_id)
        req=ProviderRequest()
        await self.plugin.inject(event,req)
        injected="\n".join(getattr(part,"text","") for part in req.extra_user_content_parts)
        self.assertIn("此生挚爱",injected)
        self.assertNotIn("qq-adapter:other",injected)
        self.assertNotIn("other",injected)

    async def test_b3_admin_command_ends_active_binding_and_keeps_history(self):
        event=FakeEvent(user_id="admin")
        identity="qq-adapter:target-2"
        self.plugin.store.migrate_confirmed_binding(identity=identity,scope_kind="global",scope_id="")
        binding=self.plugin.store.active_bindings_for(identity,"global","")[0]
        result=await self.plugin.end_relationship_binding(event,binding["binding_id"]).__anext__()
        self.assertIn("已结束",result)
        self.assertEqual([],self.plugin.store.active_bindings_for(identity,"global", ""))
        self.assertEqual(1,len(self.plugin.store.list_bindings(status="ended")))

    async def test_c0_llm_auto_escalates_but_administrator_only_does_not(self):
        event=FakeEvent()
        payload='<relation_judgment>{"schema_version":3,"fact_effects":[],"interaction_safety_proposal":{"level":"slow_down","reason_code":"boundary_pressure"}}</relation_judgment>visible reply'
        # Exercise administrator_only explicitly; C0's shipped default is llm_auto.
        self.plugin.config["interaction_safety"]["llm_mode"]="administrator_only"
        await self.plugin.judge(event, FakeResponse(payload))
        kind,sid=self.plugin._scope(event); identity=self.plugin._identity(event)
        self.assertEqual("normal",self.plugin.store.existing_account(identity,kind,sid)["state"]["interaction_safety"])
        self.plugin.config["interaction_safety"]["llm_mode"]="llm_auto"
        event.message_obj=type("Message",(),{"message_id":"c0-escalate"})()
        response=FakeResponse(payload)
        await self.plugin.judge(event,response)
        self.assertEqual("visible reply",response.completion_text)
        self.assertEqual("normal",self.plugin.store.existing_account(identity,kind,sid)["state"]["interaction_safety"])
        self.assertEqual("slow_down",self.plugin.store.effective_interaction_safety(identity,kind,sid))

    async def test_c1_query_uses_effective_timed_safety(self):
        event=FakeEvent(); kind,sid=self.plugin._scope(event); identity=self.plugin._identity(event)
        self.plugin.store.account(identity,kind,sid)
        self.plugin.store.apply_turn_with_binding(event_id="query-timer",identity=identity,scope_kind=kind,scope_id=sid,source_kind="private",evidence="",reason="",requested={},applied={},notes={},binding=None,timed_safety={"level":"slow_down","duration_minutes":30})
        result=await self.plugin.query_relation(event).__anext__()
        self.assertIn("建议放缓",result)

    async def test_c1_pages_explicit_safety_update_clears_timer(self):
        identity="qq-adapter:target-pages"; self.plugin.store.account(identity,"global","")
        self.plugin.store.apply_turn_with_binding(event_id="pages-timer",identity=identity,scope_kind="global",scope_id="",source_kind="private",evidence="",reason="",requested={},applied={},notes={},binding=None,timed_safety={"level":"slow_down","duration_minutes":30})
        account=self.plugin.store.existing_account(identity,"global","")
        self.plugin.store.update_account_admin(identity=identity,scope_kind="global",scope_id="",expected_revision=account["revision"],values=account["values"],state_changes={"interaction_safety":"normal"})
        self.assertIsNone(self.plugin.store.active_timed_safety(identity,"global",""))

    async def test_b2_clear_eligible_proposal_auto_binds_once(self):
        event = FakeEvent()
        scope_kind, scope_id = self.plugin._scope(event)
        identity = self.plugin._identity(event)
        for key, value in {"trust": 700, "respect": 700, "comfort": 650, "closeness": 650, "resonance": 550}.items():
            self.plugin.store.set_dimension(identity, scope_kind, scope_id, key, value)
        response = FakeResponse('<relation_judgment>{"schema_version":2,"fact_effects":[],"relationship_proposal":{"action":"bind","type_id":"friend","origin":"mutual_dialogue","mutuality":"clear","summary":"safe"}}</relation_judgment>自然回复')
        await self.plugin.judge(event, response)
        bindings = self.plugin.store.list_bindings(status="active")
        self.assertEqual(1, len(bindings)); self.assertEqual("friend", bindings[0]["type_key"])
        self.assertEqual("自然回复", response.completion_text)
        await self.plugin.judge(event, response)
        self.assertEqual(1, len(self.plugin.store.list_bindings(status="active")))

    async def test_b2_one_sided_or_locked_proposal_does_not_bind(self):
        event = FakeEvent()
        response = FakeResponse('<relation_judgment>{"schema_version":2,"fact_effects":[],"relationship_proposal":{"action":"bind","type_id":"romantic_partner","origin":"user_request","mutuality":"insufficient","summary":"safe"}}</relation_judgment>自然回复')
        await self.plugin.judge(event, response)
        self.assertEqual([], self.plugin.store.list_bindings(status="active"))

    async def test_leaked_leading_bare_json_is_stripped_and_applied(self):
        event = FakeEvent()
        response = FakeResponse(
            '{"schema_version":1,"fact_effects":[{"effects":{"trust":4}}]}\n自然回复。'
        )
        await self.plugin.judge(event, response)
        self.assertEqual("自然回复。", response.completion_text)
        self.assertEqual(404, self.plugin.store.account("qq-adapter:user-1")["values"]["trust"])

    async def test_leaked_leading_bare_json_in_result_chain_is_stripped(self):
        event = FakeEvent()
        response = FakeResponse("fallback", MessageChain(chain=[Plain(
            '{"schema_version":1,"fact_effects":[{"effects":{"comfort":2}}]}\n自然回复。'
        )]))
        await self.plugin.judge(event, response)
        self.assertEqual("自然回复。", response.result_chain.chain[0].text)
        self.assertEqual(452, self.plugin.store.account("qq-adapter:user-1")["values"]["comfort"])

    async def test_unrelated_leading_json_is_not_stripped(self):
        event = FakeEvent()
        original='{"schema_version":99,"note":"ordinary content"}\n自然回复。'
        response = FakeResponse(original)
        await self.plugin.judge(event, response)
        self.assertEqual(original, response.completion_text)
        self.assertEqual(DEFAULT_VALUES["trust"], self.plugin.store.account("qq-adapter:user-1")["values"]["trust"])

    async def test_result_chain_is_stripped_and_applied_without_second_call(self):
        event = FakeEvent()
        response = FakeResponse(
            "internal fallback must not be used",
            MessageChain(chain=[Plain(
                "当然。<relation_judgment>{\"schema_version\":1,\"fact_effects\":[{\"evidence\":\"谢谢你愿意等我\",\"reason\":\"尊重了交流节奏\",\"effects\":{\"trust\":4,\"comfort\":2}}]}</relation_judgment>"
            )]),
        )
        await self.plugin.judge(event, response)
        self.assertEqual("internal fallback must not be used", response.completion_text)
        self.assertEqual(["当然。"], [part.text for part in response.result_chain.chain if isinstance(part, Plain)])
        values = self.plugin.store.account("qq-adapter:user-1")["values"]
        self.assertEqual(404, values["trust"])
        self.assertEqual(452, values["comfort"])

    async def test_same_message_id_in_distinct_users_does_not_cross_deduplicate(self):
        first=FakeEvent(user_id="user-a"); second=FakeEvent(user_id="user-b")
        first.message_obj=type("Message",(),{"message_id":"shared-id"})(); second.message_obj=type("Message",(),{"message_id":"shared-id"})()
        verdict='<relation_judgment>{"schema_version":1,"fact_effects":[{"effects":{"trust":2}}]}</relation_judgment>ok'
        await self.plugin.judge(first,FakeResponse(verdict)); await self.plugin.judge(second,FakeResponse(verdict))
        scope_kind,scope_id=self.plugin._scope(first)
        self.assertEqual(402,self.plugin.store.existing_account(self.plugin._identity(first),scope_kind,scope_id)["values"]["trust"])
        self.assertEqual(402,self.plugin.store.existing_account(self.plugin._identity(second),scope_kind,scope_id)["values"]["trust"])

    async def test_no_stable_message_id_strips_but_never_settles(self):
        event=FakeEvent(); event.message_obj=type("Message",(),{"message_id":None})()
        scope_kind,scope_id=self.plugin._scope(event); identity=self.plugin._identity(event)
        response=FakeResponse('<relation_judgment>{"schema_version":1,"fact_effects":[{"effects":{"trust":2}}]}</relation_judgment>正常回复')
        await self.plugin.judge(event,response)
        self.assertEqual("正常回复",response.completion_text)
        self.assertIsNone(self.plugin.store.existing_account(identity,scope_kind,scope_id))

    async def test_missing_protocol_does_not_apply_or_rewrite(self):
        event = FakeEvent()
        response = FakeResponse("", MessageChain(chain=[Plain("正常正文")]))
        await self.plugin.judge(event, response)
        self.assertEqual(["正常正文"], [part.text for part in response.result_chain.chain if isinstance(part, Plain)])
        values = self.plugin.store.account("qq-adapter:user-1")["values"]
        self.assertEqual(400, values["trust"])

    async def test_empty_final_output_is_safely_skipped(self):
        event = FakeEvent()
        response = FakeResponse("")
        await self.plugin.judge(event, response)
        self.assertEqual("", response.completion_text)
        values = self.plugin.store.account("qq-adapter:user-1")["values"]
        self.assertEqual(400, values["trust"])

    async def test_result_chain_without_protocol_is_not_rewritten(self):
        event = FakeEvent()
        chain = MessageChain(chain=[Plain("正常正文"), Plain("第二段")])
        response = FakeResponse("", chain)
        await self.plugin.judge(event, response)
        self.assertEqual(["正常正文", "第二段"], [part.text for part in response.result_chain.chain if isinstance(part, Plain)])
        values = self.plugin.store.account("qq-adapter:user-1")["values"]
        self.assertEqual(400, values["trust"])

    async def test_non_directed_group_strips_but_does_not_apply(self):
        event = FakeEvent(group_id="group-1", wake=False, outline="普通消息")
        response = FakeResponse(
            "收到。<relation_judgment>{\"schema_version\":1,\"fact_effects\":[{\"evidence\":\"谢谢你愿意等我\",\"reason\":\"尊重了交流节奏\",\"effects\":{\"trust\":4}}]}</relation_judgment>"
        )
        await self.plugin.judge(event, response)
        self.assertEqual("收到。", response.completion_text)
        values = self.plugin.store.account("qq-adapter:user-1")["values"]
        self.assertEqual(400, values["trust"])

    async def test_hidden_romance_is_stripped_but_policy_blocks_write(self):
        event = FakeEvent()
        response = FakeResponse('<relation_judgment>{"schema_version":1,"fact_effects":[{"effects":{"romance_interest":4}}]}</relation_judgment>正文')
        await self.plugin.judge(event, response)
        self.assertEqual('正文', response.completion_text)
        self.assertEqual(0, self.plugin.store.account("qq-adapter:user-1")["values"]["romance_interest"])

    async def test_user_can_observe_but_not_show_before_eligible(self):
        event = FakeEvent()
        text = await self.plugin.relationship_route(event, "恋爱观察").__anext__()
        self.assertIn("恋爱观察", text)
        state = self.plugin.store.account("qq-adapter:user-1")["state"]
        self.assertEqual("observing", state["romance_policy"])
        text = await self.plugin.relationship_route(event, "显示恋爱").__anext__()
        self.assertIn("尚未满足", text)

    async def test_observing_policy_still_blocks_romance_settlement(self):
        """Observing only permits eligibility checks; it must not unlock romance writes."""
        identity = "qq-adapter:user-1"
        self.plugin.store.set_state(identity, "global", "", romance_policy="observing", romance_state="observing")
        for dimension, value in (("trust", 600), ("comfort", 600), ("closeness", 500), ("resonance", 450)):
            self.plugin.store.set_dimension(identity, "global", "", dimension, value)
        response = FakeResponse('<relation_judgment>{"schema_version":1,"fact_effects":[{"effects":{"romance_interest":6}}]}</relation_judgment>正文')
        await self.plugin.judge(FakeEvent(), response)
        self.assertEqual(0, self.plugin.store.account(identity)["values"]["romance_interest"])

    async def test_shown_and_eligible_allows_romance_settlement(self):
        identity = "qq-adapter:user-1"
        for dimension, value in (("trust", 600), ("comfort", 600), ("closeness", 500), ("resonance", 450)):
            self.plugin.store.set_dimension(identity, "global", "", dimension, value)
        self.plugin.store.set_state(identity, "global", "", romance_policy="shown", romance_state="eligible")
        response = FakeResponse('<relation_judgment>{"schema_version":1,"fact_effects":[{"effects":{"romance_interest":6}}]}</relation_judgment>正文')
        await self.plugin.judge(FakeEvent(), response)
        self.assertEqual(6, self.plugin.store.account(identity)["values"]["romance_interest"])

    async def test_paused_interaction_safety_blocks_romance(self):
        identity = "qq-adapter:user-1"
        for dimension, value in (("trust", 600), ("comfort", 600), ("closeness", 500), ("resonance", 450)):
            self.plugin.store.set_dimension(identity, "global", "", dimension, value)
        self.plugin.store.set_state(identity, "global", "", romance_policy="shown", romance_state="eligible", interaction_safety="pause_intimacy")
        response = FakeResponse('<relation_judgment>{"schema_version":1,"fact_effects":[{"effects":{"romance_interest":6}}]}</relation_judgment>正文')
        await self.plugin.judge(FakeEvent(), response)
        self.assertEqual(0, self.plugin.store.account(identity)["values"]["romance_interest"])

    async def test_global_switch_off_blocks_romance_even_when_shown(self):
        identity = "qq-adapter:user-1"
        for dimension, value in (("trust", 600), ("comfort", 600), ("closeness", 500), ("resonance", 450)):
            self.plugin.store.set_dimension(identity, "global", "", dimension, value)
        self.plugin.store.set_state(identity, "global", "", romance_policy="shown", romance_state="eligible")
        self.plugin.config["romance"]["global_enabled"] = False
        response = FakeResponse('<relation_judgment>{"schema_version":1,"fact_effects":[{"effects":{"romance_interest":6}}]}</relation_judgment>正文')
        await self.plugin.judge(FakeEvent(), response)
        self.assertEqual(0, self.plugin.store.account(identity)["values"]["romance_interest"])

    async def test_injection_warns_model_when_romance_locked(self):
        event = FakeEvent()
        req = ProviderRequest(prompt="hello", system_prompt="persona")
        await self.plugin.inject(event, req)
        self.assertIn("不得", req.extra_user_content_parts[0].text)
        self.assertIn("不得考虑、推断、讨论或输出 romance_interest", req.extra_user_content_parts[0].text)

    async def test_admin_can_set_interaction_pace(self):
        event = FakeEvent("admin")
        scope_kind, scope_id = self.plugin._scope(event)
        self.plugin.store.account("qq-adapter:target-2", scope_kind, scope_id)
        result = "".join([item async for item in self.plugin.set_safety(event, "target-2", "暂停亲密")])
        self.assertIn("pause_intimacy", result)
        self.assertEqual("pause_intimacy", self.plugin.store.account("qq-adapter:target-2")["state"]["interaction_safety"])

    async def test_legacy_audit_dimension_does_not_break_history(self):
        identity = "qq-adapter:user-1"
        self.plugin.store.apply(event_id="legacy:1", identity=identity, scope_kind="global", scope_id="",
                                source_kind="private", evidence="", reason="", requested={"boundary_pressure": 2},
                                applied={"boundary_pressure": 2}, notes={})
        text = "".join([item async for item in self.plugin.history(FakeEvent())])
        self.assertIn("boundary_pressure", text)

    async def test_admin_can_close_romance_globally(self):
        event = FakeEvent(user_id="admin")
        text = await self.plugin.romance_global_switch(event, "关闭").__anext__()
        self.assertIn("关闭", text)
        self.assertFalse(self.plugin.config["romance"]["global_enabled"])

    async def test_default_user_summary_hides_internal_romance_and_normal_pace(self):
        account = self.plugin.store.account("qq-adapter:user-1")
        text = self.plugin._summary(account["values"], account["state"], False)
        self.assertNotIn("恋爱路线", text)
        self.assertNotIn("互动节奏", text)
        self.assertNotIn("恋爱意向", text)

    async def test_observing_user_summary_and_close_observation(self):
        event = FakeEvent()
        await self.plugin.relationship_route(event, "恋爱观察").__anext__()
        account = self.plugin.store.account("qq-adapter:user-1")
        self.assertIn("观察中", self.plugin._summary(account["values"], account["state"], False))
        text = await self.plugin.relationship_route(event, "关闭观察").__anext__()
        self.assertIn("关闭", text)
        account = self.plugin.store.account("qq-adapter:user-1")
        self.assertEqual("hidden", account["state"]["romance_policy"])
        self.assertNotIn("恋爱路线", self.plugin._summary(account["values"], account["state"], False))

    async def test_hidden_injection_omits_romance_value_and_forbids_romance_effect(self):
        event = FakeEvent()
        req = ProviderRequest(prompt="hello", system_prompt="persona")
        await self.plugin.inject(event, req)
        dynamic, contract = (part.text for part in req.extra_user_content_parts)
        self.assertNotIn("恋爱意向原始值", dynamic)
        self.assertIn("不得考虑、推断、讨论或输出 romance_interest", dynamic)
        self.assertNotIn("romance_interest", contract)

    async def test_admin_can_close_observation_for_target(self):
        event = FakeEvent(user_id="admin")
        scope_kind, scope_id = self.plugin._scope(event)
        self.plugin.store.account("qq-adapter:target-2", scope_kind, scope_id)
        await self.plugin.admin_route(event, "target-2", "恋爱观察").__anext__()
        text = await self.plugin.admin_route(event, "target-2", "关闭观察").__anext__()
        self.assertIn("已关闭", text)
        self.assertEqual("hidden", self.plugin.store.account("qq-adapter:target-2")["state"]["romance_policy"])

    async def test_active_romance_injection_keeps_anti_force_protection(self):
        event = FakeEvent()
        scope_kind, scope_id = self.plugin._scope(event)
        identity = self.plugin._identity(event)
        for key, value in {"trust": 600, "comfort": 600, "closeness": 500, "resonance": 500, "romance_interest": 300}.items():
            self.plugin.store.set_dimension(identity, scope_kind, scope_id, key, value)
        self.plugin.store.set_state(identity, scope_kind, scope_id, romance_policy="shown", romance_state="eligible")
        req = ProviderRequest(prompt="hello", system_prompt="persona")
        await self.plugin.inject(event, req)
        dynamic, contract = (part.text for part in req.extra_user_content_parts)
        self.assertIn("反强推保护", dynamic)
        self.assertIn("单方表白、土味情话", dynamic)
        self.assertIn("礼物按角色人设、自然度和频率判断", dynamic)
        self.assertIn("romance_interest", contract)

    async def test_admin_display_target_resolves_to_existing_event_identity(self):
        event = FakeEvent(user_id="admin")
        scope_kind, scope_id = self.plugin._scope(event)
        self.plugin.store.account("qq-adapter:target-2", scope_kind, scope_id)
        result = await self.plugin.set_dimension(event, "显示名(target-2)", "信赖", "32.7").__anext__()
        self.assertIn("32.7", result)
        self.assertEqual(327, self.plugin.store.existing_account("qq-adapter:target-2", scope_kind, scope_id)["values"]["trust"])
        self.assertIsNone(self.plugin.store.existing_account("qq-adapter:显示名(target-2)", scope_kind, scope_id))

    async def test_admin_legacy_platform_display_identity_resolves_canonically(self):
        event=FakeEvent(user_id="admin")
        scope_kind,scope_id=self.plugin._scope(event)
        self.plugin.store.account("qq-adapter:target-2",scope_kind,scope_id)
        result=await self.plugin.set_dimension(event,"qq-adapter:显示名(target-2)","信赖","31.0").__anext__()
        self.assertIn("31.0",result)
        self.assertEqual(310,self.plugin.store.existing_account("qq-adapter:target-2",scope_kind,scope_id)["values"]["trust"])

    async def test_all_admin_target_mutators_reject_unknown_without_creating_account(self):
        event=FakeEvent(user_id="admin"); scope_kind,scope_id=self.plugin._scope(event); before=len(self.plugin.store.list_accounts())
        cases=(self.plugin.set_dimension(event,"显示名(no-account)","信赖","33.0"),self.plugin.adjust_dimension(event,"显示名(no-account)","信赖","1.0"),self.plugin.admin_route(event,"显示名(no-account)","恋爱观察"),self.plugin.set_safety(event,"显示名(no-account)","放缓"))
        for action in cases:
            text=await action.__anext__()
            self.assertIn("用法",text)
            self.assertEqual(before,len(self.plugin.store.list_accounts()))
        self.assertIsNone(self.plugin.store.existing_account("qq-adapter:no-account",scope_kind,scope_id))

    async def test_admin_route_existing_account_changes_injection_state(self):
        event=FakeEvent(user_id="admin"); target="qq-adapter:target-3"; scope_kind,scope_id=self.plugin._scope(event)
        self.plugin.store.account(target,scope_kind,scope_id)
        await self.plugin.admin_route(event,"显示名(target-3)","恋爱观察").__anext__()
        account=self.plugin.store.existing_account(target,scope_kind,scope_id)
        self.assertEqual("observing",account["state"]["romance_policy"])
        self.assertEqual("observing",account["state"]["romance_state"])
        self.assertEqual(1,len(self.plugin.store.list_accounts()))

    async def test_admin_unknown_target_is_rejected_without_creating_account(self):
        event = FakeEvent(user_id="admin")
        scope_kind, scope_id = self.plugin._scope(event)
        before=len(self.plugin.store.list_accounts())
        result = await self.plugin.set_dimension(event, "不存在(unknown-404)", "信赖", "32.7").__anext__()
        self.assertIn("用法", result)
        self.assertEqual(before,len(self.plugin.store.list_accounts()))
        self.assertIsNone(self.plugin.store.existing_account("qq-adapter:unknown-404", scope_kind, scope_id))

    async def test_admin_command_updates_target_not_sender(self):
        event = FakeEvent(user_id="admin")
        scope_kind, scope_id = self.plugin._scope(event)
        self.plugin.store.account("qq-adapter:target-2", scope_kind, scope_id)
        result = await self.plugin.set_dimension(event, "target-2", "信赖", "32.7").__anext__()
        self.assertIn("32.7", result)
        self.assertEqual(327, self.plugin.store.account("qq-adapter:target-2")["values"]["trust"])
        self.assertEqual(400, self.plugin.store.account("qq-adapter:admin")["values"]["trust"])

    async def test_invalid_admin_dimension_input_is_rejected(self):
        event = FakeEvent(user_id="admin")
        result = await self.plugin.set_dimension(event, "target-2", "不存在", "abc").__anext__()
        self.assertIn("用法", result)

    async def test_pages_accounts_script_keeps_admin_edit_controls(self):
        script = (ROOT / "pages" / "settings" / "app.js").read_text(encoding="utf-8")
        for marker in ("data-dimension", "data-policy", "data-safety", "data-save", "apiPost('accounts'", "data-filter"):
            self.assertIn(marker, script)
        self.assertNotIn("d.events", script)

    async def test_b3_pages_binding_manager_contract(self):
        root=Path(__file__).resolve().parents[1] / "pages" / "settings"
        text=(root / "index.html").read_text(encoding="utf-8")+(root / "app.js").read_text(encoding="utf-8")
        for marker in ('data-tab="bindings"', "apiGet('bindings'", "apiPost('bindings'", 'data-end-binding', 'renderBindings'):
            self.assertIn(marker, text)

    async def test_c4_blacklisted_settlement_does_not_mutate_or_pollute_health(self):
        event=FakeEvent(); kind,sid=self.plugin._scope(event); identity=self.plugin._identity(event)
        self.plugin.config["auto_blacklist"]={"enabled":True,"settlement_limit":1}
        self.plugin.store.account(identity,kind,sid)
        self.plugin.store.blacklist_settlement(identity,kind,sid,"settlement_limit")
        before=self.plugin.store.existing_account(identity,kind,sid)["values"]["trust"]
        health_before=self.plugin.store.protocol_health_summary()["total"]
        payload='<relation_judgment>{"schema_version":3,"fact_effects":[{"effects":{"trust":10}}]}</relation_judgment>visible'
        response=FakeResponse(payload); await self.plugin.judge(event,response)
        self.assertEqual("visible",response.completion_text)
        self.assertEqual(before,self.plugin.store.existing_account(identity,kind,sid)["values"]["trust"])
        self.assertEqual(health_before,self.plugin.store.protocol_health_summary()["total"])

    async def test_c4_private_admin_can_list_and_clear_exact_entry(self):
        event=FakeEvent(user_id="admin"); kind,sid=self.plugin._scope(event); identity="qq-adapter:target-c4"
        self.plugin.store.account(identity,kind,sid); self.plugin.store.blacklist_settlement(identity,kind,sid,"settlement_limit")
        listed=await self.plugin.settlement_blacklist_admin(event,"列表").__anext__()
        self.assertIn(identity,listed)
        cleared=await self.plugin.settlement_blacklist_admin(event,"清除","target-c4").__anext__()
        self.assertIn("已清除",cleared); self.assertFalse(self.plugin.store.is_settlement_blacklisted(identity,kind,sid))

    async def test_c2_blocked_and_allow_miss_never_create_or_mutate_account(self):
        event=FakeEvent(); admin=FakeEvent(user_id="admin"); kind,sid=self.plugin._scope(event); identity=self.plugin._identity(event)
        self.plugin.config["blocked_sessions"]=[str(event.unified_msg_origin)]
        self.assertIn("未开放",await self.plugin.relation(event).__anext__())
        self.assertIn("未开放",await self.plugin.history(event).__anext__())
        self.assertIn("未开放",await self.plugin.set_dimension(admin,"user-1","信赖","50").__anext__())
        self.assertIsNone(self.plugin.store.existing_account(identity,kind,sid))
        self.plugin.config["blocked_sessions"]=[]; self.plugin.config["allowed_sessions"]=["other:scope"]
        payload='<relation_judgment>{"schema_version":3,"fact_effects":[{"effects":{"trust":2}}]}</relation_judgment>reply'
        await self.plugin.judge(event,FakeResponse(payload))
        self.assertIsNone(self.plugin.store.existing_account(identity,kind,sid))

    async def test_c2_group_rejects_third_party_and_bulk_before_lookup(self):
        admin=FakeEvent(user_id="admin",group_id="group-1"); user=FakeEvent(user_id="user-1",group_id="group-1")
        self.assertIn("未开放",await self.plugin.query_relation(user,"target").__anext__())
        self.assertIn("未开放",await self.plugin.query_global_scope(admin,1).__anext__())
        self.assertIn("未开放",await self.plugin.query_all_scopes(admin,1).__anext__())

    async def test_query_permission_and_scope_commands(self):
        admin=FakeEvent(user_id="admin"); user=FakeEvent(user_id="user-1")
        scope_kind,scope_id=self.plugin._scope(admin)
        self.plugin.store.account("qq-adapter:target-9",scope_kind,scope_id)
        text=await self.plugin.query_relation(user,"显示名(target-9)").__anext__()
        self.assertIn("关系查询",text)
        self.plugin.config["query_permission"]["private_normal_user"]=False
        text=await self.plugin.query_relation(user,"target-9").__anext__()
        self.assertIn("当前会话未开放",text)
        text=await self.plugin.query_global_scope(admin,1).__anext__()
        self.assertIn("全局关系",text)
        text=await self.plugin.query_global_scope(user,1).__anext__()
        self.assertIn("当前会话未开放",text)

    async def test_query_respects_existing_romance_visibility_policy(self):
        event=FakeEvent(user_id="user-1"); target="qq-adapter:target-10"; scope_kind,scope_id=self.plugin._scope(event)
        self.plugin.store.account(target,scope_kind,scope_id)
        account=self.plugin.store.existing_account(target,scope_kind,scope_id)
        high={**account["values"],"trust":600,"comfort":600,"closeness":500,"resonance":500}
        self.plugin.store.update_account_admin(identity=target,scope_kind=scope_kind,scope_id=scope_id,expected_revision=account["revision"],values=high,state_changes={"romance_policy":"shown","romance_state":"eligible"})
        text=await self.plugin.query_relation(event,"target-10").__anext__()
        self.assertIn("恋爱意向",text)
        self.plugin.store.set_state(target,scope_kind,scope_id,romance_policy="observing",romance_state="observing")
        text=await self.plugin.query_relation(event,"target-10").__anext__()
        self.assertNotIn("恋爱意向：",text)

    async def test_query_config_controls_present_in_pages(self):
        script=(ROOT / "pages" / "settings" / "app.js").read_text(encoding="utf-8")
        for marker in ('query-group','query-private','query_permission'):
            self.assertIn(marker,script)

    async def test_page_apis_are_registered(self):
        self.assertEqual(8, len(self.context.apis))
        self.assertEqual(
            {"/astrbot_plugin_relation_arc/config", "/astrbot_plugin_relation_arc/accounts", "/astrbot_plugin_relation_arc/audit", "/astrbot_plugin_relation_arc/backups", "/astrbot_plugin_relation_arc/overview", "/astrbot_plugin_relation_arc/health", "/astrbot_plugin_relation_arc/migrations", "/astrbot_plugin_relation_arc/bindings"},
            {item[0] for item in self.context.apis},
        )


if __name__ == "__main__":
    unittest.main()
