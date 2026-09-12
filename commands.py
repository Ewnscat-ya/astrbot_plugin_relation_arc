"""Chat command handlers (MIS-100)."""
import json
import re
import time

from astrbot.api.event import filter
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .relation_engine import DEFAULT_VALUES, DIMENSIONS, DISPLAY, PUBLIC_DIMENSIONS

# Command-surface dimension aliases; canonical keys are in DIMENSIONS.
ALIASES = {
    **{key: key for key in DIMENSIONS},
    "信赖": "trust", "认可": "respect", "安心感": "comfort",
    "情感亲近": "closeness", "亲近感": "closeness", "共鸣": "resonance", "恋爱意向": "romance_interest",
}
from .relationship_types import get_type


class CommandsMixin:
    @filter.command("结束正式关系")
    async def end_relationship_binding(self, event: AstrMessageEvent, binding_id: str = ""):
        if not self._capability_allowed(event,"admin_binding_end"):
            yield self._capability_denied(event); return
        if not binding_id:
            yield event.plain_result("用法：/结束正式关系 <binding_id>")
            return
        scope = self.store.binding_scope(binding_id)
        if not scope or not self._stored_scope_allowed(*scope):
            yield self._capability_denied(event); return
        if self.store.end_binding(binding_id, actor="administrator_command"):
            yield event.plain_result("正式关系已结束，历史已保留。")
        else:
            yield event.plain_result("未找到可结束的 active 正式关系。")
    @filter.command("自动黑名单")
    async def settlement_blacklist_admin(self,event: AstrMessageEvent,action: str="",target: str=""):
        if not self._capability_allowed(event,"admin_mutation"):
            yield self._capability_denied(event); return
        scope_kind,scope_id=self._scope(event); action=action.strip()
        if action=="列表":
            entries=[item for item in self.store.settlement_blacklist_entries(scope_kind) if item["scope_id"]==scope_id]
            # This administrator-only private command intentionally exposes only
            # canonical targets needed for exact clearance; never reason/evidence.
            yield event.plain_result("【自动黑名单】\n"+("\n".join(f"- {item['identity']}" for item in entries) if entries else "暂无条目")); return
        if action=="清除":
            identity=self._target_identity(event,target,scope_kind,scope_id)
            if not identity:
                yield event.plain_result("用法：/自动黑名单 清除 <已有用户ID>"); return
            if self.store.clear_settlement_blacklist(identity,scope_kind,scope_id):
                yield event.plain_result("自动黑名单条目已清除。")
            else:
                yield event.plain_result("未找到该自动黑名单条目。")
            return
        yield event.plain_result("用法：/自动黑名单 <列表|清除 用户ID>")
    @filter.command("增减关系维度")
    async def adjust_dimension(self, event: AstrMessageEvent, target: str = "", dimension: str = "", delta: str = ""):
        if not self._capability_allowed(event,"admin_mutation"):
            yield self._capability_denied(event); return
        scope_kind, scope_id = self._scope(event)
        identity = self._target_identity(event, target, scope_kind, scope_id)
        key = ALIASES.get(dimension.strip())
        try:
            raw_delta = self._numeric_tenths(delta)
        except (TypeError, ValueError, OverflowError):
            raw_delta = None
        if not identity or not key or raw_delta is None:
            yield event.plain_result("用法：/增减关系维度 <用户ID> <维度> <变化值>")
            return
        current = self.store.existing_account(identity, scope_kind, scope_id)
        if current is None:
            yield event.plain_result("目标账户不存在；请从账户管理复制规范身份。")
            return
        values = self.store.adjust_dimension(identity, scope_kind, scope_id, key, raw_delta)
        yield event.plain_result(f"用户 {target} 的{DISPLAY[key]}现为 {values[key] / 10:.1f}")
    @filter.command("修改关系维度")
    async def set_dimension(self, event: AstrMessageEvent, target: str = "", dimension: str = "", value: str = ""):
        if not self._capability_allowed(event,"admin_mutation"):
            yield self._capability_denied(event); return
        scope_kind, scope_id = self._scope(event)
        identity = self._target_identity(event, target, scope_kind, scope_id)
        key = ALIASES.get(dimension.strip())
        try:
            raw_value = self._numeric_tenths(value)
        except (TypeError, ValueError, OverflowError):
            raw_value = None
        if not identity or not key or raw_value is None:
            yield event.plain_result("用法：/修改关系维度 <用户ID> <维度> <0.0-100.0>")
            return
        values = self.store.set_dimension(identity, scope_kind, scope_id, key, max(0, min(1000, raw_value)))
        yield event.plain_result(f"用户 {target} 的{DISPLAY[key]}已设置为 {values[key] / 10:.1f}")
    @staticmethod
    def _numeric_tenths(value) -> int:
        import math
        if type(value) not in (str, int, float): raise ValueError("invalid numeric type")
        number=float(value)*10
        if not math.isfinite(number): raise ValueError("finite number required")
        return round(number)
    def _target_identity(self, event: AstrMessageEvent, target: str, scope_kind: str, scope_id: str) -> str | None:
        """Resolve an admin target only to an existing canonical account identity.
    
        Event writes always use ``platform:sender_id``.  Accept a bare ID, @ID,
        the exact canonical identity, or display text ending in ``(ID)``; never
        manufacture an account from a display label.
        """
        raw=target.strip().lstrip("@").strip()
        platform=str(event.get_platform_id())
        if not raw or len(raw)>128: return None
        # Even a copied platform-qualified display string may be legacy
        # ``platform:display(id)``; normalize its suffix before lookup.
        suffix=raw[len(platform)+1:] if raw.startswith(platform + ":") else raw
        match=re.fullmatch(r".*\(([^()\s]+)\)",suffix)
        user_id=(match.group(1) if match else suffix).strip()
        if not user_id or any(char.isspace() for char in user_id) or ":" in user_id:
            return None
        candidate=f"{platform}:{user_id}"
        return candidate if self.store.existing_account(candidate,scope_kind,scope_id) else None
    @filter.command("恋爱路线管理")
    async def admin_route(self, event: AstrMessageEvent, target: str = "", mode: str = ""):
        if not self._capability_allowed(event,"admin_mutation"):
            yield self._capability_denied(event); return
        scope_kind, scope_id = self._scope(event)
        identity = self._target_identity(event, target, scope_kind, scope_id)
        policy = {"隐藏恋爱": "hidden", "关闭观察": "hidden", "恋爱观察": "observing", "显示恋爱": "shown"}.get(mode.strip())
        if not identity or not policy:
            yield event.plain_result("用法：/恋爱路线管理 <用户ID> <恋爱观察|关闭观察|显示恋爱>"); return
        current = self.store.existing_account(identity, scope_kind, scope_id)
        if current is None:
            yield event.plain_result("目标账户不存在；请从账户管理复制规范身份。")
            return
        state = "hidden" if policy == "hidden" else "observing" if policy == "observing" else ("eligible" if self._romance_thresholds_met(current["values"]) else "observing")
        account = self.store.update_account_admin(identity=identity, scope_kind=scope_kind, scope_id=scope_id, expected_revision=current["revision"], values=current["values"], state_changes={"romance_policy":policy,"romance_state":state}, actor="administrator")
        if policy == "hidden":
            yield event.plain_result(f"用户 {target} 的恋爱观察已关闭；后续关系判断不考虑恋爱意向")
        else:
            yield event.plain_result(f"用户 {target} 的恋爱路线已设为 {account['state']['romance_policy']}")
    @filter.command("互动节奏")
    async def set_safety(self, event: AstrMessageEvent, target: str = "", mode: str = ""):
        if not self._capability_allowed(event,"admin_mutation"):
            yield self._capability_denied(event); return
        scope_kind, scope_id = self._scope(event)
        identity = self._target_identity(event, target, scope_kind, scope_id)
        safety = {"正常": "normal", "放缓": "slow_down", "暂停亲密": "pause_intimacy"}.get(mode.strip())
        if not identity or not safety:
            yield event.plain_result("用法：/互动节奏 <用户ID> <正常|放缓|暂停亲密>"); return
        account = self.store.set_interaction_safety_admin(identity, scope_kind, scope_id, safety)
        yield event.plain_result(f"用户 {target} 的互动节奏已设为 {account['state']['interaction_safety']}")
    @filter.command("恋爱总闸")
    async def romance_global_switch(self, event: AstrMessageEvent, mode: str = ""):
        if not self._capability_allowed(event,"admin_config"):
            yield self._capability_denied(event); return
        if mode.strip() not in {"开启", "关闭"}:
            yield event.plain_result("用法：/恋爱总闸 <开启|关闭>"); return
        self.config = self.config_mgr.update({"romance": {"global_enabled": mode.strip() == "开启"}})
        yield event.plain_result(f"恋爱路线全局总闸已{mode.strip()}")
    @filter.command("关系路线")
    async def relationship_route(self, event: AstrMessageEvent, mode: str = ""):
        if not self._capability_allowed(event,"self_route"):
            yield self._capability_denied(event); return
        scope_kind, scope_id = self._scope(event); identity = self._identity(event)
        if not self.config.get("romance", {}).get("global_enabled", True):
            yield event.plain_result("恋爱路线目前由管理员全局关闭")
            return
        mapping = {"恋爱观察": "observing", "关闭观察": "hidden", "隐藏恋爱": "hidden", "显示恋爱": "shown"}
        policy = mapping.get(mode.strip())
        if not policy:
            yield event.plain_result("用法：/关系路线 <恋爱观察|关闭观察|显示恋爱>")
            return
        current = self.store.account(identity, scope_kind, scope_id)
        if policy == "hidden":
            romance_state = "hidden"
        elif policy == "observing":
            romance_state = "observing"
        else:
            romance_state = "eligible" if self._romance_thresholds_met(current["values"]) else "observing"
        account = self.store.set_state(identity, scope_kind, scope_id, romance_policy=policy, romance_state=romance_state)
        if policy == "observing":
            yield event.plain_result("恋爱路线已设为：恋爱观察。仅允许系统观察资格，恋爱意向仍不会结算。")
        elif policy == "shown" and not self._romance_eligible(account["values"], account["state"]):
            yield event.plain_result("已记录显示意愿，但当前尚未满足可攻略资格；恋爱意向仍不会自动结算。")
        elif policy == "hidden":
            yield event.plain_result("恋爱观察已关闭；后续关系判断将不考虑恋爱意向。")
        elif policy == "observing":
            yield event.plain_result("恋爱观察已开启；仅检查资格，恋爱意向仍不会结算。")
        else:
            yield event.plain_result(f"恋爱路线已设为：{mode.strip()}")
    @filter.command("查询全部关系", alias={"查全部关系"})
    async def query_all_scopes(self,event: AstrMessageEvent,page:int=1):
        if not self._capability_allowed(event,"bulk_query"):
            yield self._capability_denied(event); return
        yield event.plain_result(self._query_page(page=page,title="全部关系"))
    @filter.command("查询全局关系", alias={"查全局关系"})
    async def query_global_scope(self,event: AstrMessageEvent,page:int=1):
        if not self._capability_allowed(event,"bulk_query"):
            yield self._capability_denied(event); return
        yield event.plain_result(self._query_page(page=page,title="全局关系",scope_kind="global"))
    @filter.command("查询当前会话关系", alias={"查当前会话关系","查询本会话关系"})
    async def query_current_scope(self,event: AstrMessageEvent,page:int=1):
        if not self._capability_allowed(event,"bulk_query"):
            yield self._capability_denied(event); return
        if self.config["is_global_relation"]:
            yield event.plain_result("当前为全局模式，请使用 /查询全局关系。")
            return
        scope_kind,scope_id=self._scope(event)
        yield event.plain_result(self._query_page(page=page,title="当前会话关系",scope_kind="session",scope_id=scope_id))
    @filter.command("查询关系", alias={"查关系","查看关系"})
    async def query_relation(self, event: AstrMessageEvent, target: str = ""):
        capability="third_party_query" if target.strip() else "self_query"
        if not self._capability_allowed(event,capability):
            yield self._capability_denied(event); return
        scope_kind,scope_id=self._scope(event)
        identity=self._identity(event) if not target.strip() else self._query_target_identity(event,target,scope_kind,scope_id)
        account=self.store.existing_account(identity,scope_kind,scope_id) if identity else None
        if not account:
            yield event.plain_result("当前 scope 未找到目标关系账户。")
            return
        state=self._effective_state(identity,scope_kind,scope_id,account["state"])
        yield event.plain_result("【关系查询】\n"+self._summary(account["values"],state,str(event.get_sender_id()) in self.admins, base_safety=account["state"].get("interaction_safety","normal"), timed=self.store.active_timed_safety(identity,scope_kind,scope_id)))
    def _query_page(self, *, page: int, title: str, scope_kind: str | None = None, scope_id: str | None = None) -> str:
        # MIS-98: totals and pages come from server-side filtered COUNT, so
        # excluded scopes cannot shift pages or totals.
        size = 20
        total = self.store.count_accounts_page(scope_kind=scope_kind, scope_id=scope_id, scope_allowed=self._stored_scope_allowed)
        pages = max(1, (total + size - 1) // size)
        page = max(1, min(int(page), pages))
        accounts = self.store.list_accounts_page(page=page, page_size=size, scope_kind=scope_kind, scope_id=scope_id, scope_allowed=self._stored_scope_allowed)
        rows = []
        for item in accounts:
            label = item["identity"].rsplit(":", 1)[-1]; public = {k: item["values"].get(k, 0) / 10 for k in PUBLIC_DIMENSIONS}
            binding = ",".join(get_type(x["type_key"]).label for x in self.store.active_bindings_for(item["identity"], item["scope_kind"], item["scope_id"]) if get_type(x["type_key"])) or "无"
            rows.append(f"- {label} | {item['scope_kind']} | 信赖 {public['trust']:.1f} 认可 {public['respect']:.1f} 安心 {public['comfort']:.1f} 亲近 {public['closeness']:.1f} 共鸣 {public['resonance']:.1f} | 正式关系 {binding}")
        return f"【{title}】第 {page}/{pages} 页，共 {total} 条\n" + ("\n".join(rows) if rows else "暂无记录")
    def _query_target_identity(self, event: AstrMessageEvent, target: str, scope_kind: str, scope_id: str) -> str | None:
        # Same canonical/display(ID) resolver as management, but no writes.
        return self._target_identity(event, target, scope_kind, scope_id)
    def _query_allowed(self, event: AstrMessageEvent) -> bool:
        if str(event.get_sender_id()) in self.admins: return True
        permissions=self.config.get("query_permission", {})
        return bool(permissions.get("group_normal_user", True) if self._is_group(event) else permissions.get("private_normal_user", True))
    @filter.command("关系记录")
    async def history(self, event: AstrMessageEvent):
        if not self._capability_allowed(event,"self_query"):
            yield self._capability_denied(event); return
        scope_kind, scope_id = self._scope(event)
        identity = self._identity(event)
        rows = self.store.recent(identity, scope_kind, scope_id)
        account = self.store.existing_account(identity, scope_kind, scope_id)
        values = account["values"] if account else dict(DEFAULT_VALUES)
        base_state = account["state"] if account else {}
        state = self._effective_state(identity, scope_kind, scope_id, base_state)
        # MIS-93: one display projection for history. Romance stays behind the
        # existing hiding policy and private-chat reasons never surface in a
        # group, regardless of where the settlement happened.
        is_group = self._is_group(event)
        romance_visible = (not is_group) and self._romance_eligible(values, state)
        show_reason = not is_group
        lines = ["【近期关系记录】"]
        for row in rows:
            changes = json.loads(row["applied_json"])
            shown = "、".join(
                f"{DISPLAY.get(key, key)} {change / 10:+.1f}"
                for key, change in changes.items()
                if change and (key != "romance_interest" or romance_visible))
            suffix = f"：{row['reason']}" if show_reason and row.get("reason") else ""
            lines.append(f"- {shown or '无变化'}{suffix}")
        yield event.plain_result("\n".join(lines))
    @filter.command("关系", alias={"关系状态"})
    async def relation(self, event: AstrMessageEvent):
        if not self._capability_allowed(event,"self_query"):
            yield self._capability_denied(event); return
        scope_kind, scope_id = self._scope(event); identity=self._identity(event)
        account = self.store.account(identity, scope_kind, scope_id)
        timed = self.store.active_timed_safety(identity, scope_kind, scope_id)
        yield event.plain_result("【关系状态】\n" + self._summary(account["values"], self._effective_state(identity,scope_kind,scope_id,account["state"]), str(event.get_sender_id()) in self.admins, base_safety=account["state"].get("interaction_safety","normal"), timed=timed))
