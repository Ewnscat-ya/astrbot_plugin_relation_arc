"""Generate the frozen baseline behavior-contract samples for MIS-87.

Runs the real plugin stack (RelationArc + RelationStore + protocol parser)
against a synthetic account in an isolated temporary database and prints
deterministic input -> output samples for the six contract categories:

1. normal settlement            4. legal same-turn auto binding
2. anti-farm / repeat decay     5. disabled scope (strip without writes)
3. hidden romance lock          6. same-turn safety escalation + romance block
plus: value boundaries, display units and scope isolation.

No real accounts, messages or credentials are used; all identifiers are
synthetic and the database lives in a TemporaryDirectory.

Usage (from the plugin parent directory, with AstrBot installed):
    python -m unittest discover -s tests   # first: prove the 92-test baseline
    python tools/behavior_contract_samples.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "tests"))

from astrbot.api.provider import ProviderRequest  # noqa: E402
from astrbot.core.message.components import Plain  # noqa: E402
from astrbot.core.message.message_event_result import MessageChain  # noqa: E402

from astrbot_plugin_relation_arc.main import RelationArc  # noqa: E402
from astrbot_plugin_relation_arc.relation_engine import (  # noqa: E402
    DEFAULT_VALUES, DISPLAY, PUBLIC_DIMENSIONS, aggregate_effects, apply_delta,
    behavior_projection,
)
from astrbot_plugin_relation_arc.relationship_types import public_directory  # noqa: E402
from test_main import FakeContext, FakeEvent, FakeResponse  # noqa: E402

BLOCK = '<relation_judgment>{}</relation_judgment>'


def make_event(user_id: str, message_str: str, message_id: str = "message-1") -> FakeEvent:
    event = FakeEvent(user_id=user_id)
    event.message_str = message_str
    # FakeEvent shares message_obj as a class attribute; each sample turn needs
    # its own instance so message-id idempotency is exercised deliberately.
    event.message_obj = type("Message", (), {"message_id": message_id})()
    return event


def block(payload: dict) -> str:
    return BLOCK.format(json.dumps(payload, ensure_ascii=False))


def fmt(values: dict) -> str:
    return json.dumps(values, ensure_ascii=False)


class Samples:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def h2(self, title: str) -> None:
        self.lines.append(f"\n## {title}\n")

    def row(self, label: str, value: str) -> None:
        self.lines.append(f"- **{label}**: {value}")

    def print(self) -> None:
        print("\n".join(self.lines))


async def main() -> None:
    out = Samples()
    out.h2("六维、默认值与展示单位")
    out.row("DIMENSIONS", ", ".join(DEFAULT_VALUES))
    out.row("DEFAULT_VALUES", fmt(DEFAULT_VALUES))
    out.row("展示单位", "整数 0–1000 存储；对外展示 = 原值 / 10（一位小数），如 trust 400 → 40.0")
    out.row("edge_saturation", "score≥700 时单轮正向增量上限 2，≥850 时上限 1（apply_delta 700/850 两档）")
    out.row("range_clamp", "0≤score≤1000 硬夹取")
    one = apply_delta(1000, 8, 0, [1.0], False)
    out.row("样例:1000+8", f"applied={one.applied} notes={list(one.notes)}")
    sat = apply_delta(860, 5, 0, [1.0], False)
    out.row("样例:860+5", f"applied={sat.applied} notes={list(sat.notes)}")
    out.row("public_directory 类型数", str(len(public_directory())) + "（friend/close_friend/partner/romantic_partner/spouse，romance 组排他）")

    out.h2("1. 正常结算（v1 协议，普通正负分）")
    with tempfile.TemporaryDirectory() as tmp:
        plugin = RelationArc(FakeContext(tmp))
        try:
            event = make_event("sample-a", "谢谢你愿意等我")
            payload = {"schema_version": 1, "fact_effects": [
                {"evidence": "谢谢你愿意等我", "reason": "尊重了交流节奏",
                 "effects": {"trust": 4, "comfort": 2}}]}
            response = FakeResponse("当然。" + block(payload))
            await plugin.judge(event, response)
            account = plugin.store.account("qq-adapter:sample-a", "global", "")
            out.row("输入", "completion_text='当然。'+<relation_judgment> v1; effects {trust:+4, comfort:+2}")
            out.row("输出 clean_text", response.completion_text)
            out.row("输出 values", f"trust {account['values']['trust']} (400+4), comfort {account['values']['comfort']} (450+2)")
            out.row("输出 audit", "events 1 行 actor=llm, binding=no_binding")
            rows = plugin.store.recent("qq-adapter:sample-a", "global", "")
            note = json.loads(rows[0]["notes_json"])
            out.row("输出 notes.trust", json.dumps(note.get("trust"), ensure_ascii=False))

            out.h2("2. 反刷：重复事实衰减与消息幂等")
            event2 = make_event("sample-a", "谢谢你愿意等我", message_id="message-2")
            await plugin.judge(event2, FakeResponse(block(payload)))
            account2 = plugin.store.account("qq-adapter:sample-a", "global", "")
            rows2 = plugin.store.recent("qq-adapter:sample-a", "global", "")
            note2 = json.loads(rows2[0]["notes_json"])
            out.row("输入", "新 message_id + 同一 evidence/reason/effects 第二轮提交")
            out.row("输出", f"trust {account2['values']['trust']}（404+{account2['values']['trust']-404}，repeat_factor=0.6）; notes.trust={json.dumps(note2.get('trust'), ensure_ascii=False)}")
            before = account2["values"]
            await plugin.judge(event2, FakeResponse(block(payload)))  # same message_id replay
            account3 = plugin.store.account("qq-adapter:sample-a", "global", "")
            rows3 = plugin.store.recent("qq-adapter:sample-a", "global", "")
            note3 = json.loads(rows3[0]["notes_json"])
            out.row("输入", "同 message_id（message-2）重放")
            out.row("输出", f"values 不变（{fmt(account3['values'])}）；最新行 notes.trust 仍为 {json.dumps(note3.get('trust'), ensure_ascii=False)}（duplicate_event 幂等，events 仍 2 行）")
            out.row("events 总行数", str(len(plugin.store.list_events(limit=50, scope_kind='global'))))

            out.h2("3. 隐藏恋爱：romance_interest 锁定为 0 变化")
            event_r = make_event("sample-r", "做我女朋友吧")
            payload_r = {"schema_version": 1, "fact_effects": [
                {"evidence": "用户表达好感", "reason": "用户提出交往",
                 "effects": {"trust": 2, "romance_interest": 5}}]}
            response_r = FakeResponse("嗯。" + block(payload_r))
            await plugin.judge(event_r, response_r)
            acct_r = plugin.store.account("qq-adapter:sample-r", "global", "")
            rows_r = plugin.store.recent("qq-adapter:sample-r", "global", "")
            note_r = json.loads(rows_r[0]["notes_json"])
            out.row("前置", "romance.default_policy=hidden（默认路线），romance_state 未达 eligible")
            out.row("输入", "v1 effects {trust:+2, romance_interest:+5}")
            out.row("输出", f"trust {acct_r['values']['trust']} (400+2), romance_interest {acct_r['values']['romance_interest']}（不变，applied=0）; notes={json.dumps(note_r.get('romance_interest'), ensure_ascii=False)}")
            event_r2 = make_event("sample-r2", "做我女朋友吧")
            payload_r2 = {"schema_version": 1, "fact_effects": [
                {"evidence": "用户表达好感", "reason": "用户提出交往",
                 "effects": {"romance_interest": 5}}]}
            await plugin.judge(event_r2, FakeResponse("嗯。" + block(payload_r2)))
            rows_r2 = plugin.store.recent("qq-adapter:sample-r2", "global", "")
            out.row("纯恋爱被锁轮", f"romance_interest 单独被锁且无其他变化 → zero_after_policy，零写入、无审计行（events={len(rows_r2)}，本轮行为基线事实）")
            out.row("inject 注入", "恋爱路线未启用：本轮不得考虑/输出 romance_interest；合同 effect_keys 不含 romance_interest")

            out.h2("4. 合法双向明确提案自动绑定（friend）")
            identity = "qq-adapter:sample-b"
            plugin.store.account(identity, "global", "")
            # 达到 friend 阈值：trust 500 / comfort 450 / respect 450
            plugin.store.apply_turn_with_binding(
                event_id="seed:sample-b", identity=identity, scope_kind="global", scope_id="",
                source_kind="private", evidence="seed", reason="seed",
                requested={"trust": 100, "respect": 0, "comfort": 0, "closeness": 0, "resonance": 0},
                applied={"trust": 100}, notes={}, binding=None)
            base = plugin.store.account(identity, "global", "")["values"]
            event_b = make_event("sample-b", "以后也请多指教")
            payload_b = {"schema_version": 3, "fact_effects": [],
                         "relationship_proposal": {"action": "bind", "type_id": "friend",
                                                   "origin": "mutual_dialogue", "mutuality": "clear",
                                                   "summary": "双方明确认可朋友关系"}}
            await plugin.judge(event_b, FakeResponse(block(payload_b)))
            bindings = plugin.store.active_bindings_for(identity, "global", "")
            out.row("前置", f"values 达 friend 阈值 trust={base['trust']} comfort={base['comfort']} respect={base['respect']}（阈值 500/450/450）")
            out.row("输入", "v3 relationship_proposal action=bind type_id=friend origin=mutual_dialogue mutuality=clear（无分数变化轮）")
            out.row("输出", f"binding_created; active_bindings={[(b['type_key']) for b in bindings]}")
            out.row("同轮重复提案", "第二次相同 bind → binding_rejected:duplicate（排他/唯一性保留，账本仍一行）")
            await plugin.judge(event_b, FakeResponse(block(payload_b)))
            rows_b = plugin.store.recent(identity, "global", "")
            note_b = json.loads(rows_b[0]["notes_json"])
            out.row("重放输出", json.dumps(note_b.get("binding"), ensure_ascii=False) + "（duplicate_event 幂等，不产生第二条绑定）")
            # 反例：mutuality 不足
            event_c = make_event("sample-c", "我们算朋友吧")
            payload_c = {"schema_version": 2, "fact_effects": [
                {"effects": {"trust": 3}}],
                "relationship_proposal": {"action": "bind", "type_id": "friend",
                                          "origin": "user_request", "mutuality": "insufficient"}}
            await plugin.judge(event_c, FakeResponse(block(payload_c)))
            rows_c = plugin.store.recent("qq-adapter:sample-c", "global", "")
            note_c = json.loads(rows_c[0]["notes_json"])
            out.row("反例输入", "mutuality=insufficient 且 trust 400+3<500 阈值")
            out.row("反例输出", f"no binding; binding note={json.dumps(note_c.get('binding'), ensure_ascii=False)}（分数达标≠绑定，双向明确才自动绑定）")

            out.h2("5. 禁用 scope：剥离但零写入")
            plugin.config["llm_judgment_enabled"] = False
            event_d = make_event("sample-d", "谢谢")
            payload_d = {"schema_version": 1, "fact_effects": [{"effects": {"trust": 9}}]}
            response_d = FakeResponse("不客气。" + block(payload_d))
            await plugin.judge(event_d, response_d)
            rows_d = plugin.store.recent("qq-adapter:sample-d", "global", "")
            out.row("输入", "llm_judgment_enabled=false + 合法 v1 块")
            out.row("输出 clean_text", response_d.completion_text)
            out.row("输出", f"events 行数={len(rows_d)}（0，无任何结算写入）")

            out.h2("6. 同轮安全提升：v3 提案生效并压制同轮恋爱正向")
            plugin.config["llm_judgment_enabled"] = True
            event_e = make_event("sample-e", "抱抱我嘛，快点")
            payload_e = {"schema_version": 3,
                         "fact_effects": [{"effects": {"trust": 2, "romance_interest": 4}}],
                         "interaction_safety_proposal": {"level": "slow_down", "reason_code": "boundary_pressure"}}
            await plugin.judge(event_e, FakeResponse(block(payload_e)))
            identity_e = "qq-adapter:sample-e"
            acct_e = plugin.store.account(identity_e, "global", "")
            rows_e = plugin.store.recent(identity_e, "global", "")
            note_e = json.loads(rows_e[0]["notes_json"])
            timed = plugin.store.active_timed_safety(identity_e, "global", "")
            out.row("前置", "interaction_safety.llm_mode=llm_auto（默认）")
            out.row("输入", "v3: effects {trust:+2, romance_interest:+4} + safety slow_down/boundary_pressure")
            out.row("输出 values", f"trust {acct_e['values']['trust']} (400+2), romance_interest {acct_e['values']['romance_interest']}（0，同轮被压）")
            out.row("输出 notes", f"romance={json.dumps(note_e.get('romance_interest'), ensure_ascii=False)}; safety={json.dumps(note_e.get('interaction_safety'), ensure_ascii=False)}")
            out.row("输出 timed_safety", f"level={timed['level'] if timed else None}, source={timed['source'] if timed else None}（30 分钟自动时限状态，与管理员基础安全状态分离）")

            out.h2("scope 隔离与群聊定向")
            out.row("is_global_relation=true（默认）", "identity=platform:sender，scope=global；session 模式下同一用户在不同会话是独立账户")
            out.row("group_require_at_or_reply=true（默认）", "群聊未被唤醒/@/引用时 settlement=skipped_group_not_directed，不结算")
            out.row("无稳定 message_id", "settlement=skipped_no_stable_message_id，先于账户创建，不产生首互动行")
            plugin.terminate_sync = None

            out.h2("行为投影（V4 合同，固定文案）")
            out.row("中性默认", behavior_projection(DEFAULT_VALUES, False, False, "normal")[:120] + "……")
            out.row("隐藏/锁定", behavior_projection(DEFAULT_VALUES, False, False, "normal").split("；")[-1])
            out.row("未达资格", behavior_projection(DEFAULT_VALUES, True, False, "normal").split("；")[-1])
            out.row("safety!=normal", behavior_projection(DEFAULT_VALUES, True, True, "slow_down").split("；")[-1])
            out.row("可恋爱高分", behavior_projection({**DEFAULT_VALUES, "romance_interest": 700}, True, True, "normal").split("；")[-1])

            out.h2("维度归因合同（注入文本固定要求）")
            out.row("六维含义", "信赖=可靠真诚守约可托付；认可=能力原则判断值得认真看待；安心感=无压节奏边界受尊重；亲近感=共同记忆与自然日常关心；共鸣=情绪价值经历幽默被真正理解")
            out.row("不计入", "普通礼貌、复读、刷屏、群聊起哄通常持平；单方情话/模板攻略不得仅凭自身提升 romance_interest")
        finally:
            await plugin.terminate()
    out.print()


if __name__ == "__main__":
    asyncio.run(main())
