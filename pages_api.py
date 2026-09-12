"""Pages dashboard API handlers (MIS-100)."""
import json
import re

from quart import request, jsonify

from .config_manager import ConfigRevisionConflict
from .relation_engine import DIMENSIONS, PUBLIC_DIMENSIONS
from .relationship_types import get_type, public_directory

# Matches main.PLUGIN_NAME; declared here to avoid circular import.
PLUGIN_NAME = 'astrbot_plugin_relation_arc'


class PagesApiMixin:
    async def _api_backups(self):
        from quart import request, jsonify
        if request.method == "GET":
            return jsonify({"backups": self.store.list_backups(), "retention_hours": self.config.get("backup", {}).get("retention_hours", 168), "scheduler": dict(self._backup_state)})
        payload = await request.get_json()
        if not isinstance(payload, dict) or payload.get("action") != "backup_now":
            return jsonify({"error": "仅支持 backup_now"}), 400
        path = self.store.backup_now("manual")
        return jsonify({"success": True, "name": path.name, "kind": "manual"})
    async def _api_migrations(self):
        from quart import jsonify
        return jsonify({"schema_version": 9, "entries": self.store.list_migrations()})
    async def _api_health(self):
        from quart import jsonify
        days = int(self.config.get("protocol_health", {}).get("retention_days", 30))
        return jsonify(self.store.protocol_health_summary(days))
    async def _api_overview(self):
        from quart import jsonify
        # MIS-98: overview totals come from COUNT queries, never from
        # truncated lists; exclusion filtering rides the same admission.
        scope_allowed = self._stored_scope_allowed
        account_total = self.store.count_accounts_page(scope_allowed=scope_allowed)
        account_global = self.store.count_accounts_page(scope_kind="global", scope_allowed=scope_allowed)
        account_session = self.store.count_accounts_page(scope_kind="session", scope_allowed=scope_allowed)
        binding_total = self.store.count_bindings_page(scope_allowed=scope_allowed)
        binding_active = self.store.count_bindings_page(status="active", scope_allowed=scope_allowed)
        binding_ended = self.store.count_bindings_page(status="ended", scope_allowed=scope_allowed)
        binding_global = self.store.count_bindings_page(scope_kind="global", scope_allowed=scope_allowed)
        binding_session = self.store.count_bindings_page(scope_kind="session", scope_allowed=scope_allowed)
        binding_summary = {"total": binding_total,
                           "active": binding_active,
                           "ended": binding_ended,
                           "global": binding_global,
                           "session": binding_session}
        return jsonify({"schema_version": 11, "plugin_version": self.plugin_version, "relation_scope_mode": "global" if self.config.get("is_global_relation", True) else "session", "accounts": {"total": account_total, "global": account_global, "session": account_session}, "bindings": binding_summary, "backups": {kind: sum(item["kind"] == kind for item in self.store.list_backups()) for kind in ("auto", "manual", "migration", "pre_restore")}})
    async def _api_audit(self):
        from quart import request, jsonify
        scope_filter=request.args.get("scope")
        if scope_filter not in {"global","session"}: scope_filter=None
        # Event cards deliberately omit identity, evidence, reason and raw notes;
        # MIS-93/98: excluded scopes are filtered server-side before pagination.
        try: page=max(1,int(request.args.get("page", 1)))
        except ValueError: page=1
        try: page_size=max(1,min(int(request.args.get("page_size", 100)), self.store.MAX_PAGE_SIZE))
        except ValueError: return jsonify({"error":"page_size 必须是整数"}),400
        total=self.store.count_events_page(scope_kind=scope_filter, scope_allowed=self._stored_scope_allowed)
        rows=self.store.list_events_page(page=page,page_size=page_size,scope_kind=scope_filter,scope_allowed=self._stored_scope_allowed)
        cards=self.store.audit_cards_from_rows(rows)
        return jsonify({"cards":cards,"page":page,"page_size":page_size,"total":total,"pages":max(1,(total+page_size-1)//page_size)})
    async def _api_accounts(self):
        from quart import request, jsonify
        if request.method == "POST":
            payload=await request.get_json()
            if not isinstance(payload,dict) or payload.get("action") != "update_account": return jsonify({"error":"仅支持原子 update_account"}),400
            identity=str(payload.get("identity","")).strip(); scope_kind=str(payload.get("scope_kind", "")); scope_id=str(payload.get("scope_id", ""))
            try:
                revision=payload.get("revision"); raw_values=payload.get("values")
                if type(revision) is not int or revision < 0 or not isinstance(raw_values,dict): raise ValueError("invalid revision or values")
                values={key:self._numeric_tenths(raw_values[key]) for key in DIMENSIONS}
            except (TypeError,ValueError,KeyError,OverflowError): return jsonify({"error":"revision 或六维值无效"}),400
            # MIS-99: safety/policy are explicit-only. Omitting them keeps the
            # route, the base safety and any running automatic timer intact.
            has_policy = "romance_policy" in payload
            has_safety = "interaction_safety" in payload
            policy=str(payload.get("romance_policy", "")); safety=str(payload.get("interaction_safety", ""))
            if has_policy and policy not in {"hidden","observing","shown"}: return jsonify({"error":"路线无效"}),400
            if has_safety and safety not in {"normal","slow_down","pause_intimacy"}: return jsonify({"error":"互动节奏无效"}),400
            if not self._stored_scope_allowed(scope_kind,scope_id): return jsonify({"error":"当前 scope 未开放"}),403
            current=self.store.existing_account(identity,scope_kind,scope_id)
            if not current: return jsonify({"error":"目标账户不存在；拒绝隐式创建"}),404
            state_changes={}
            if has_policy:
                state_changes["romance_policy"]=policy
                state_changes["romance_state"]="hidden" if policy=="hidden" else "observing" if policy=="observing" else ("eligible" if self._romance_thresholds_met(values) else "observing")
            if has_safety:
                state_changes["interaction_safety"]=safety
            cancel_timed = payload.get("clear_timed_safety") is True
            try: account=self.store.update_account_admin(identity=identity,scope_kind=scope_kind,scope_id=scope_id,expected_revision=revision,values=values,state_changes=state_changes,cancel_timed=cancel_timed)
            except RuntimeError: return jsonify({"error":"账户已被更新，请刷新后重试"}),409
            except ValueError: return jsonify({"error":"账户或 scope 无效"}),400
            return jsonify({"success":True,"account":account})
        scope_filter=request.args.get("scope")
        if scope_filter not in {"global","session"}: scope_filter=None
        # MIS-98: server-side pagination and filtering; totals come from COUNT
        # with the same admission policy, never from a truncated list.
        try: page=max(1,int(request.args.get("page", 1)))
        except ValueError: page=1
        try: page_size=max(1,min(int(request.args.get("page_size", 50)), self.store.MAX_PAGE_SIZE))
        except ValueError: return jsonify({"error":"page_size 必须是整数"}),400
        total=self.store.count_accounts_page(scope_kind=scope_filter, scope_allowed=self._stored_scope_allowed)
        accounts=[{key:item[key] for key in ("identity","scope_kind","scope_id","values","state","paused","revision","updated_at")} for item in self.store.list_accounts_page(page=page,page_size=page_size,scope_kind=scope_filter,scope_allowed=self._stored_scope_allowed)]
        return jsonify({"accounts":accounts,"page":page,"page_size":page_size,"total":total,"pages":max(1,(total+page_size-1)//page_size)})
    async def _api_config(self):
        from quart import request, jsonify
        if request.method == "GET":
            return jsonify(self.config)
        data = await request.get_json()
        if not isinstance(data, dict):
            return jsonify({"error": "配置必须是 JSON 对象"}), 400
        expected = data.pop("expected_revision", None)
        if expected is not None and type(expected) is not int:
            return jsonify({"error": "expected_revision 必须是整数"}), 400
        try:
            self.config = self.config_mgr.update(data, expected_revision=expected)
        except ConfigRevisionConflict as exc:
            return jsonify({"error": "配置已被其他窗口更新，请刷新后重试", "current_revision": exc.current_revision}), 409
        except ValueError as exc:
            return jsonify({"error": f"配置无效：{exc}"}), 400
        await self._restart_schedulers()
        return jsonify({"success": True, "config_revision": self.config.get("config_revision", 0)})
    def _register_page_apis(self) -> None:
        # AstrBot Pages owns dashboard authentication; handlers never expose raw message text.
        self.context.register_web_api(f"/{PLUGIN_NAME}/config", self._api_config, ["GET", "POST"], "关系弧线配置")
        self.context.register_web_api(f"/{PLUGIN_NAME}/accounts", self._api_accounts, ["GET", "POST"], "关系弧线账户")
        self.context.register_web_api(f"/{PLUGIN_NAME}/audit", self._api_audit, ["GET"], "关系弧线审计")
        self.context.register_web_api(f"/{PLUGIN_NAME}/backups", self._api_backups, ["GET", "POST"], "关系弧线备份")
        self.context.register_web_api(f"/{PLUGIN_NAME}/overview", self._api_overview, ["GET"], "关系弧线概览")
        self.context.register_web_api(f"/{PLUGIN_NAME}/health", self._api_health, ["GET"], "关系弧线协议健康")
        self.context.register_web_api(f"/{PLUGIN_NAME}/migrations", self._api_migrations, ["GET"], "关系弧线迁移")
        self.context.register_web_api(f"/{PLUGIN_NAME}/bindings", self._api_bindings, ["GET", "POST"], "关系弧线正式关系")
