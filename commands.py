"""Non-decorated command-surface helpers (MIS-100).

The decorated chat command entries live in main.py's class body on purpose:
the host registers a handler under the function's ``__module__`` captured at
decoration time and later binds/dispatches it only for the plugin registered
under that exact module. A decorator applied inside this mixin module would
register here and never be claimed by the plugin (MIS-117).
"""
import re

from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .relation_engine import PUBLIC_DIMENSIONS
from .relationship_types import get_type


class CommandHelpersMixin:
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
