from __future__ import annotations
import json
import time
import re
import asyncio
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger
from astrbot.api.star import Star, Context, register
from astrbot.api.event import filter
from astrbot.api.provider import ProviderRequest, LLMResponse
from astrbot.core.agent.message import TextPart
from astrbot.core.message.components import At, Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .config_manager import ConfigRevisionConflict, PluginConfigManager
from .relation_engine import (
    DEFAULT_VALUES, DIMENSIONS, PUBLIC_DIMENSIONS, DISPLAY, aggregate_effects, apply_delta,
    behavior_projection,
)
from .pages_api import PagesApiMixin
from .commands import CommandHelpersMixin
from . import prompts as relation_prompts
from .relation_protocol import BLOCK, leading_bare_json_span, parse_response, strip_protocol_text
from .relation_store import RelationStore
from .relationship_types import get_type, projected_eligibility, public_directory

PLUGIN_NAME = "astrbot_plugin_relation_arc"
ALIASES = {
    **{key: key for key in DIMENSIONS},
    "信赖": "trust", "认可": "respect", "安心感": "comfort",
    "情感亲近": "closeness", "亲近感": "closeness", "共鸣": "resonance", "恋爱意向": "romance_interest",
}


# MIS-99: metadata.yaml is the single source of truth for the plugin version
# (the @register decorator and the Pages overview both read from here).
def _read_plugin_version() -> str:
    import re
    try:
        match = re.search(r"^version:\s*(.+)$", (Path(__file__).parent / "metadata.yaml").read_text(encoding="utf-8"), re.M)
        if match:
            return match.group(1).strip().strip('"').strip("'")
    except OSError:
        pass
    return "0.1.0"

PLUGIN_VERSION = _read_plugin_version()

@register(PLUGIN_NAME, "Ewnscat", "独立多维关系、风格投影与可审计关系账本", PLUGIN_VERSION)
class RelationArc(PagesApiMixin, CommandHelpersMixin, Star):
    def __init__(self, context: Context, config: Optional[dict] = None):
        super().__init__(context)
        host_config = context.get_config() or {}
        data_root = Path(host_config.get("plugin.data_dir", host_config.get("data", "./data")))
        self.config_mgr = PluginConfigManager(Path(__file__).parent, data_root)
        self.config = self.config_mgr.load_or_create()
        self.admins = {str(item) for item in host_config.get("admins_id", [])}
        self.store = RelationStore(data_root / "plugin_data" / PLUGIN_NAME, self.config)
        # MIS-125 R2: the desired binding policy activates on reload, inside
        # one store transaction. A refused activation keeps the previous
        # effective policy and is surfaced read-only via policy_status.
        activation = self.store.activate_binding_policy(self.config.get("binding_policy", {}))
        if activation.get("activated"):
            logger.info("[关系弧线] binding_policy activated exclusivity=%s epoch=%s", activation["effective"]["exclusivity"], activation["effective"]["epoch"])
        elif activation.get("reason") == "legacy_conflict":
            logger.warning("[关系弧线] binding_policy activation refused conflicts=%s effective=%s", activation.get("conflict_scopes"), activation["effective"]["exclusivity"])
        elif activation.get("reason") == "busy":
            # MIS-134 N01: deferred, not failed — the plugin registers
            # normally with the current effective policy; the next reload
            # retries the activation and clears pending/error on success.
            logger.warning("[关系弧线] binding_policy activation deferred (mutex busy); effective=%s", activation["effective"]["exclusivity"])
        self.plugin_version = PLUGIN_VERSION
        self._decay_task: asyncio.Task | None = None
        self._backup_task: asyncio.Task | None = None
        self._decay_sig: str | None = None
        self._backup_sig: str | None = None
        self._backup_state: dict[str, Any] = {}
        self._repair_legacy_identity_splits()
        self._migrate_confirmed_legacy_binding()
        self._log_startup_migrations()
        self._register_page_apis()
        if self.config.get("decay",{}).get("enabled"):
            self._decay_task=self._spawn(self._decay_loop(),"relation-arc-decay")
        if self.config.get("backup",{}).get("enabled"):
            self._backup_task=self._spawn(self._backup_loop(),"relation-arc-backup")
        self._decay_sig=self._decay_signature()
        self._backup_sig=self._backup_signature()

    def _spawn(self, coro, name: str):
        """Create a managed task when a loop is running; sync embedding (tests,
        exotic loaders) simply skips the background schedulers."""
        try:
            return asyncio.create_task(coro, name=name)
        except RuntimeError:
            return None

    def _repair_legacy_identity_splits(self) -> None:
        # This repair deletes legacy ghost rows. A same-schema restart does not
        # create a migration backup, so protect the exact pre-delete state here.
        if not self.store.legacy_identity_split_preview():
            return
        snapshot=self.store.backup_now("migration")
        repaired=self.store.repair_legacy_identity_splits()
        if repaired:
            # No identity, message, old/new value or admin target is logged.
            logger.warning("[关系弧线] repaired legacy admin identity splits count=%s backup=%s",len(repaired),snapshot.name)
        diagnostics=self.store.legacy_identity_split_diagnostic_counts()
        if diagnostics:
            # Counts per reason code only; ambiguous history is kept untouched.
            logger.warning("[关系弧线] skipped ambiguous identity splits reasons=%s",json.dumps(diagnostics,sort_keys=True))

    def _migrate_confirmed_legacy_binding(self) -> None:
        # This is intentionally narrower than a state-based bulk migration:
        # exactly one configured administrator + explicitly committed/shown state.
        # Zero or multiple candidates are a no-op, never a guess.
        candidates=self.store.confirmed_admin_candidates(self.admins)
        if len(candidates) != 1: return
        candidate=candidates[0]
        result=self.store.migrate_confirmed_binding(**candidate)
        logger.info("[关系弧线] legacy_confirmed_binding=%s", result)

    def _log_startup_migrations(self) -> None:
        # Logs only version/backup metadata; never identities, messages, evidence or reasoning.
        for item in [*self.config_mgr.migration_events, *self.store.migration_events]:
            logger.info("[关系弧线] migration component=%s from=%s to=%s backup=%s", item["component"], item["from_version"], item["to_version"], item["backup"]) 

    def _health_enabled(self) -> bool:
        return bool(self.config.get("protocol_health", {}).get("enabled", True))

    def _record_health(self, outcome: str, source: str, parsed=None) -> None:
        if not self._health_enabled():
            return
        stats = getattr(parsed, "stats", None) or {}
        self.store.record_protocol_health(outcome, source, bool(stats.get("bare")), len(getattr(parsed, "effects", []) or []), int(self.config.get("protocol_health", {}).get("retention_days", 30)))

    def _identity(self, event: AstrMessageEvent) -> str:
        # get_platform_id is the host-supported unique adapter identity; do not use bare QQ IDs.
        return f"{event.get_platform_id()}:{event.get_sender_id()}"

    def _group_is_directed(self, event: AstrMessageEvent) -> bool:
        """Only accept wake-up / explicit reply-like group interactions for relation settlement."""
        if not self._is_group(event):
            return True
        if not self.config.get("group_require_at_or_reply", True):
            return True
        if event.is_wake_up():
            return True
        # Structured components decide when the adapter provides them: real At
        # / Reply-to-bot evidence only. Ordinary text that merely contains
        # outline markers like "[At:...]" or a quoted-message prefix must never
        # direct a settlement.
        components = getattr(getattr(event, "message_obj", None), "message", None)
        if isinstance(components, list) and components:
            self_id = str(event.get_self_id())
            for component in components:
                if isinstance(component, At) and str(component.qq) == self_id:
                    return True
                if isinstance(component, Reply):
                    sender_id = component.sender_id
                    if sender_id is not None and str(sender_id) == self_id:
                        return True
                    for part in component.chain or []:
                        if isinstance(part, At) and str(part.qq) == self_id:
                            return True
            return False
        # Adapter without structured components: outline markers stay best-effort.
        text = event.get_message_outline()
        return bool("[引用消息(" in text or f"[At:{event.get_self_id()}]" in text)

    def _pin_turn_context(self, event: AstrMessageEvent) -> None:
        """Pin this turn's identity, scope and gate decisions on the event.

        The same host event object flows through on_llm_request and
        on_llm_response. A Pages save between the two hooks must not move
        where (or whether) the turn settles: the reply completes in the
        context the user actually interacted with.
        """
        scope_kind, scope_id = self._scope(event)
        try:
            event._relation_arc_turn_ctx = {
                "identity": self._identity(event),
                "scope_kind": scope_kind,
                "scope_id": scope_id,
                "source_kind": "group" if self._is_group(event) else "private",
                "message_id": getattr(getattr(event, "message_obj", None), "message_id", None),
                "directed": self._group_is_directed(event),
                "settled": self._enabled(event) and self.config.get("llm_judgment_enabled", True),
                # MIS-125 R2: the policy epoch this turn was injected under; a
                # settlement against a different epoch may score but not bind.
                "policy_epoch": self.store.active_binding_policy()["epoch"],
            }
        except Exception:
            # Pinning is an in-memory nicety; never break the host LLM request.
            pass

    def _turn_context(self, event: AstrMessageEvent) -> dict | None:
        ctx = getattr(event, "_relation_arc_turn_ctx", None)
        return ctx if isinstance(ctx, dict) else None

    def _scope(self, event: AstrMessageEvent) -> tuple[str, str]:
        if self.config["is_global_relation"]:
            return "global", ""
        return "session", str(event.unified_msg_origin)

    def _is_group(self, event: AstrMessageEvent) -> bool:
        return bool(event.get_group_id())

    def _scope_enabled(self, event: AstrMessageEvent) -> bool:
        if not self.config.get("enabled",True): return False
        session=str(event.unified_msg_origin)
        # Block wins if an invalid config puts a scope in both lists.
        if session in self.config.get("blocked_sessions",[]): return False
        allowed=self.config.get("allowed_sessions",[])
        return not allowed or session in allowed

    def _enabled(self, event: AstrMessageEvent) -> bool:
        if not self._scope_enabled(event): return False
        return self.config.get("group_enabled", True) if self._is_group(event) else self.config.get("private_enabled", True)

    def _capability_allowed(self,event: AstrMessageEvent,capability: str) -> bool:
        if capability=="settlement": return self._enabled(event)
        admin=str(event.get_sender_id()) in self.admins
        # Do not lock an administrator out of correcting the config lists.
        if capability=="admin_config": return admin
        if not self._scope_enabled(event): return False
        if capability in {"admin_mutation","admin_binding_end","bulk_query"}: return admin and not self._is_group(event)
        if capability=="third_party_query": return not self._is_group(event) and self._query_allowed(event)
        if capability=="self_query": return self._query_allowed(event)
        if capability=="self_route": return not self._is_group(event)
        return False

    def _capability_denied(self,event: AstrMessageEvent):
        return event.plain_result("当前会话未开放此关系功能。")

    def _stored_scope_allowed(self,scope_kind: str,scope_id: str) -> bool:
        # Pages has no source chat event. Session rows obey lists; global rows
        # are managed under dashboard authentication, not guessed session IDs.
        if not self.config.get("enabled",True): return False
        if scope_kind=="global": return scope_id==""
        if scope_kind!="session" or not scope_id: return False
        if scope_id in self.config.get("blocked_sessions",[]): return False
        allowed=self.config.get("allowed_sessions",[])
        return not allowed or scope_id in allowed

    def _binding_candidate(self, proposal: dict | None, projected: dict[str, int], state: dict[str, str], scope_kind: str, scope_id: str, identity: str, event_id: str) -> tuple[dict | None, str]:
        if not proposal or proposal.get("action") != "bind": return None, "no_proposal"
        if proposal.get("mutuality") != "clear": return None, "mutuality"
        relationship_type=get_type(proposal.get("type_id"))
        if relationship_type is None: return None, "unknown_type"
        # Recompute the configured route at commit; never trust cached eligibility.
        if relationship_type.category == "romance":
            if not self._romance_settlement_allowed(projected, state): return None, "romance_route"
            state = {**state, "romance_state": "eligible"}
        eligible, reason=projected_eligibility(relationship_type, projected, state, bool(self.config.get("romance",{}).get("global_enabled",True)))
        if not eligible: return None, reason
        # Scope is passed from _scope(event), never model-authored.
        import hashlib
        binding_id=hashlib.sha256(f"{event_id}:{relationship_type.key}".encode()).hexdigest()[:32]
        return {"binding_id":binding_id,"type_key":relationship_type.key,"unique_scope":relationship_type.exclusive_group or "","origin":proposal["origin"],"summary":proposal.get("summary","")}, "eligible"

    def _romance_thresholds_met(self, values: dict[str, int]) -> bool:
        thresholds = self.config.get("romance", {}).get("eligibility_thresholds", {})
        return all(values.get(key, 0) >= int(limit) for key, limit in thresholds.items() if limit is not None)

    def _romance_eligible(self, values: dict[str, int], state: dict[str, str]) -> bool:
        """Lock chain: global switch, explicit shown policy, dimension thresholds."""
        if not self.config.get("romance", {}).get("global_enabled", True): return False
        if state.get("romance_policy") != "shown": return False
        return self._romance_thresholds_met(values)

    def _romance_settlement_allowed(self, values: dict[str, int], state: dict[str, str]) -> bool:
        return self._romance_eligible(values, state) and state.get("interaction_safety", "normal") == "normal"

    def _effective_state(self, identity: str, scope_kind: str, scope_id: str, base_state: dict[str, str]) -> dict[str, str]:
        return {**base_state,"interaction_safety":self.store.effective_interaction_safety(identity,scope_kind,scope_id)}

    def _summary(self, values: dict[str, int], state: dict[str, str], admin: bool = False, base_safety: str | None = None, timed: dict[str, Any] | None = None) -> str:
        value = lambda key: f"{values.get(key, 0) / 10:.1f}"
        lines = [f"{DISPLAY[key]}：{value(key)}" for key in PUBLIC_DIMENSIONS]
        global_enabled = self.config.get("romance", {}).get("global_enabled", True)
        policy = state.get("romance_policy", "hidden")
        eligible = self._romance_eligible(values, state)
        if admin:
            lines.append(f"恋爱管理：{'总闸开启' if global_enabled else '总闸关闭'} / {policy} / {'已确认' if state.get('romance_state') == 'committed' else '可攻略' if eligible else '未达资格'}")
            lines.append(f"恋爱意向：{value('romance_interest')}")
        elif policy == "observing":
            lines.append("恋爱路线：观察中（尚不结算或显示恋爱意向）")
        elif policy == "shown":
            if global_enabled and eligible:
                lines.append(f"恋爱意向：{value('romance_interest')}")
                lines.append("恋爱路线：可自然发展（不自动确认关系）")
            else:
                lines.append("恋爱路线：显示意愿已记录，当前尚未满足可攻略条件")
        safety_labels = {"slow_down": "建议放缓", "pause_intimacy": "暂缓亲密推进"}
        if state.get("interaction_safety", "normal") != "normal":
            lines.append(f"互动节奏：{safety_labels.get(state.get('interaction_safety'), state.get('interaction_safety'))}")
        if admin and base_safety is not None:
            # MIS-93: administrators see the administrator-owned base state, the
            # current effective state and the automatic override's remaining
            # time as three separate facts; models can never lower the base.
            base = base_safety if base_safety != "normal" else "正常"
            lines.append(f"基础节奏：{safety_labels.get(base, base)}")
            if timed:
                minutes = max(1, int(round((float(timed["expires_at"]) - time.time()) / 60)))
                lines.append(f"当前有效：{safety_labels.get(timed['level'], timed['level'])}（自动，剩余约 {minutes} 分钟）")
        return "\n".join(lines)

    # MIS-158/U01: ownership is tracked per request, never by literal tag
    # text. The private attribute below records exactly what THIS plugin
    # appended to the request: the precise system-prompt addition (separator
    # + fixed block, byte-for-byte) and the exact part objects it created.
    # Cleanup removes only those recorded additions; persona examples with
    # the same markers, other plugins' parts and later appends are untouched.
    _OWN_INJECTION_ATTR = "_relation_arc_own_injection"

    @classmethod
    def _own_injection(cls, req: ProviderRequest) -> dict | None:
        record = getattr(req, cls._OWN_INJECTION_ATTR, None)
        return record if isinstance(record, dict) else None

    @classmethod
    def _rollback_own_injection(cls, req: ProviderRequest) -> None:
        """Remove exactly what this plugin previously added to THIS request.

        The system addition is removed as one exact substring (prefer the
        suffix; fall back to a single splice anywhere in the string, which
        only ever matches the bytes we appended and keeps text other
        plugins appended before/after it). The temporary parts are removed
        by object identity — no text matching at all. With no record (this
        request was never injected by us) the request is left untouched."""
        record = cls._own_injection(req)
        if record is None:
            return
        added_system = record.get("system_added")
        if added_system:
            current = req.system_prompt or ""
            if current.endswith(added_system):
                req.system_prompt = current[:-len(added_system)]
            elif added_system in current:
                # Someone appended after our block: splice out only our
                # exact bytes and keep their suffix intact.
                req.system_prompt = current.replace(added_system, "", 1)
        own_parts = record.get("parts") or []
        if own_parts:
            own_ids = {id(part) for part in own_parts}
            req.extra_user_content_parts = [
                part for part in (req.extra_user_content_parts or [])
                if id(part) not in own_ids]
        setattr(req, cls._OWN_INJECTION_ATTR, None)

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        scope_kind, scope_id = self._scope(event); identity=self._identity(event)
        # Normalize an expiry before feature gates; C1 cleanup cannot depend on LLM.
        self.store.active_timed_safety(identity,scope_kind,scope_id)
        # Pin the turn before any gate so the response settles in the request's
        # own context even if Pages config changes mid-turn.
        self._pin_turn_context(event)
        # MIS-158 lifecycle (U01 ownership): injection is idempotent — the
        # previous own additions recorded on THIS request are rolled back by
        # reference before anything new is added; a disabled gate leaves the
        # request exactly as it was (a request we never injected is not
        # touched at all).
        self._rollback_own_injection(req)
        if not self._enabled(event) or not self.config.get("llm_judgment_enabled", True): return
        account = self.store.account(identity, scope_kind, scope_id)
        values, state = account["values"], self._effective_state(identity,scope_kind,scope_id,account["state"])
        global_romance = self.config.get("romance", {}).get("global_enabled", True)
        eligible = self._romance_eligible(values, state)
        visible = global_romance and state.get("romance_policy") == "shown"
        public_values = {key: values.get(key, 0) for key in PUBLIC_DIMENSIONS}
        active_bindings = self.store.active_bindings_for(self._identity(event), scope_kind, scope_id)
        binding_labels = [get_type(item["type_key"]).label for item in active_bindings if get_type(item["type_key"])]
        # MIS-127 R4: the injected text comes from the shared builders; the
        # cross-user occupancy shows up only as a boolean and only while the
        # ACTIVE policy keeps the exclusion on (never the desired value).
        effective_policy = self.store.active_binding_policy()
        exclusivity_on = effective_policy["exclusivity"] == "scope"
        exclusive_occupied = exclusivity_on and self.store.exclusive_occupied(scope_kind, scope_id, "romance", self._identity(event))
        binding_ctx = relation_prompts.binding_context(binding_labels, exclusivity_on, exclusive_occupied)
        romance_ctx = relation_prompts.romance_context(visible, eligible, values.get("romance_interest", 0))
        dynamic_prompt = relation_prompts.dynamic_prompt(
            json.dumps(public_values, ensure_ascii=False), binding_ctx, romance_ctx,
            state.get("interaction_safety", "normal"), values, visible, eligible)
        reminder = relation_prompts.short_turn_reminder(visible)
        # MIS-158 layout: persona first, then this plugin's fixed rules block
        # appended to the system prompt (stable across turns for the same
        # persona); the per-turn dynamic context and the short format
        # reminder stay temporary parts of the current user message.
        fixed_block = relation_prompts.fixed_rules_block()
        system_before = req.system_prompt or ""
        system_added = ("\n\n" + fixed_block) if system_before else fixed_block
        req.system_prompt = system_before + system_added
        dynamic_part = TextPart(text=dynamic_prompt).mark_as_temp()
        reminder_part = TextPart(text=reminder).mark_as_temp()
        req.extra_user_content_parts.append(dynamic_part)
        req.extra_user_content_parts.append(reminder_part)
        # U01: record the exact additions owned by this plugin on THIS
        # request so a later inject (repeat or disable) rolls back precisely
        # these bytes and objects — never anything matched by tag text.
        setattr(req, self._OWN_INJECTION_ATTR,
                {"system_added": system_added, "parts": [dynamic_part, reminder_part]})

    def _read_and_strip_judgment(self, response: LLMResponse, user_text: str):
        """Read the final provider payload without assuming completion_text is used.

        Some AstrBot providers place their final natural-language output in
        ``result_chain``.  This is still the same final LLM response, not a
        second provider call.  Keep non-text components intact while replacing
        the text payload with the protocol-stripped version.
        """
        result_chain = getattr(response, "result_chain", None)
        chain_parts = list(getattr(result_chain, "chain", []) or []) if result_chain else []
        plain_parts = [part for part in chain_parts if isinstance(part, Plain) and isinstance(part.text, str)]
        plain_text = "\n".join(part.text for part in plain_parts if part.text)
        text_source = "result_chain" if plain_text.strip() else "completion_text"
        completion = getattr(response, "completion_text", "")
        text = plain_text if plain_text.strip() else (completion if isinstance(completion, str) else "")
        parsed = parse_response(text, user_text, self.config["raw_delta_limit"])
        # A valid Relation Arc v1 JSON object leaked at the exact reply start is
        # recovered by the parser and treated as protocol, never user-visible prose.
        has_protocol = bool(BLOCK.search(text) or (parsed.stats or {}).get("bare"))
        if plain_text.strip():
            # The host's final result chain is the outgoing payload. Strip in
            # place so component order survives protocol removal, with the
            # parse-level clean text as the single source of truth (MIS-117):
            # a recovered bare-JSON span is located on the joined payload and
            # mapped back onto its parts, and any per-part cleanup that still
            # differs from the parser output (a block or truncated tail split
            # across parts) falls back to squashing the clean text into the
            # first slot.
            if has_protocol or parsed.clean_text != text.strip():
                offsets = []
                cursor = 0
                for index, part in enumerate(plain_parts):
                    if not part.text:
                        continue
                    offsets.append((index, cursor, cursor + len(part.text)))
                    cursor += len(part.text) + 1  # "\n" join separator
                cleaned_parts = [strip_protocol_text(part.text) for part in plain_parts]
                if parsed.stats and parsed.stats.get("bare"):
                    span = leading_bare_json_span(plain_text)
                    if span:
                        for index, start, end in offsets:
                            if end <= span[0] or start >= span[1]:
                                continue
                            part_text = plain_parts[index].text
                            local_start = max(0, span[0] - start)
                            local_end = min(len(part_text), span[1] - start)
                            cleaned_parts[index] = (part_text[:local_start] + part_text[local_end:]).strip()
                joined_cleaned = "\n".join(cleaned_parts)
                if (BLOCK.search(joined_cleaned) or "relation_judgment" in joined_cleaned
                        or "".join(joined_cleaned.split()) != "".join(parsed.clean_text.split())):
                    replacement_done = False
                    new_chain = []
                    for part in result_chain.chain:
                        if isinstance(part, Plain):
                            if not replacement_done and parsed.clean_text:
                                new_chain.append(Plain(parsed.clean_text))
                                replacement_done = True
                            # Drop all original Plain parts: their joined content
                            # has been replaced above after stripping the control block.
                            continue
                        new_chain.append(part)
                    result_chain.chain = new_chain
                else:
                    cleaned_iter = iter(cleaned_parts)
                    new_chain = []
                    for part in result_chain.chain:
                        if isinstance(part, Plain):
                            cleaned = next(cleaned_iter, "")
                            if cleaned.strip():
                                new_chain.append(Plain(cleaned))
                            continue
                        new_chain.append(part)
                    result_chain.chain = new_chain
        else:
            response.completion_text = parsed.clean_text
        return parsed, has_protocol, text_source, bool(text.strip())

    @filter.on_llm_response(priority=5)
    async def judge(self, event: AstrMessageEvent, response: LLMResponse) -> None:
        # Sanitization is unconditional; feature gates prohibit settlement, not cleanup.
        parsed, has_protocol, text_source, has_text = self._read_and_strip_judgment(response, event.message_str)
        ctx = self._turn_context(event)
        if ctx is not None:
            settled = bool(ctx.get("settled"))
            directed = bool(ctx.get("directed"))
        else:
            settled = self._enabled(event) and self.config.get("llm_judgment_enabled", True)
            directed = self._group_is_directed(event)
        if not settled:
            return
        # Match Favour's behavior: only a non-empty final model output without
        # its required control protocol is actionable.  Never log message text,
        # evidence, identity, or a message/session identifier.
        if not has_protocol:
            if has_text:
                self._record_health("missing_protocol", text_source, parsed)
                logger.warning("[关系弧线] settlement=missing_protocol source=%s", text_source)
            return
        # Always remove a valid control block. A non-directed group message may not settle relation data.
        if not directed:
            self._record_health("skipped_group_not_directed", text_source, parsed)
            logger.debug("[关系弧线] settlement=skipped_group_not_directed")
            return
        if parsed.error:
            self._record_health("invalid_protocol", text_source, parsed)
            logger.info("[关系弧线] settlement=rejected protocol=%s", parsed.error)
            return
        if not parsed.effects and not parsed.proposal and not parsed.safety_proposal:
            stats = parsed.stats or {}
            if stats.get("items", 0) == 0:
                self._record_health("model_no_effects", text_source, parsed)
                logger.info("[关系弧线] settlement=model_no_effects")
            elif stats.get("shape_rejected", 0) or stats.get("non_object", 0) or stats.get("empty_effects", 0):
                self._record_health("invalid_effects", text_source, parsed)
                logger.info("[关系弧线] settlement=invalid_effects")
            else:
                self._record_health("invalid_effects", text_source, parsed)
                logger.info("[关系弧线] settlement=no_valid_fact")
            return
        if ctx is not None:
            identity = ctx["identity"]
            scope_kind, scope_id = ctx["scope_kind"], ctx["scope_id"]
            raw_message_id = ctx.get("message_id")
            pinned_source = ctx.get("source_kind")
        else:
            scope_kind, scope_id = self._scope(event)
            identity = self._identity(event)
            raw_message_id = getattr(getattr(event, "message_obj", None), "message_id", None)
            pinned_source = None
        if raw_message_id is None or not str(raw_message_id).strip() or str(raw_message_id).lower() == "unknown":
            # Check before account(): that method creates first-interaction rows.
            self._record_health("skipped_no_stable_message_id", text_source, parsed)
            logger.warning("[关系弧线] settlement=skipped_no_stable_message_id")
            return
        # C4 runs only after valid Relation Arc settlement parsing and stable
        # identity. Ordinary chat and health counters are never touched here.
        if self.config.get("auto_blacklist",{}).get("enabled") and self.store.is_settlement_blacklisted(identity,scope_kind,scope_id):
            logger.info("[关系弧线] settlement=blacklist_skipped")
            return
        # MIS-92: everything below (state read -> policy/window/eligibility ->
        # writes) runs inside one store transaction with in-transaction
        # recomputation; policy stays here as pure callbacks.
        safety_proposal = parsed.safety_proposal
        all_effects = [item["effects"] for item in parsed.effects]
        requested_all = {dimension: aggregate_effects(all_effects, dimension, self.config["raw_delta_limit"]) for dimension in DIMENSIONS}
        fact_signature = self.store.effect_signature(requested_all)
        import hashlib
        # events.event_id is a database-wide primary key, so a bare adapter
        # message ID is insufficient: distinct platforms/sessions may reuse it.
        event_key="\x1f".join((identity,scope_kind,scope_id,str(raw_message_id)))
        event_id="llm:"+hashlib.sha256(event_key.encode("utf-8")).hexdigest()

        def romance_gate(current_values, current_state):
            return self._romance_settlement_allowed(current_values, current_state)

        def binding_gate(projected, final_state):
            return self._binding_candidate(parsed.proposal, projected, final_state, scope_kind, scope_id, identity, event_id)

        values, status, info = self.store.settle_turn(
            event_id=event_id, identity=identity, scope_kind=scope_kind, scope_id=scope_id,
            source_kind=pinned_source or ("group" if self._is_group(event) else "private"),
            evidence=" | ".join(item["evidence"] for item in parsed.effects),
            reason=" | ".join(item["reason"] for item in parsed.effects),
            requested_all=requested_all, fact_signature=fact_signature,
            repeat_key=parsed.effects[0]["evidence"] if parsed.effects else "",
            safety_proposal=safety_proposal,
            policy={
                "repeat_window_minutes": self.config["repeat_window_minutes"],
                "repeat_factors": self.config["repeat_factors"],
                "anti_farm": self.config.get("anti_farm", {}),
                "safety_mode": self.config.get("interaction_safety", {}).get("llm_mode", "administrator_only"),
                "auto_duration_minutes": int(self.config.get("interaction_safety", {}).get("auto_duration_minutes", 30)),
                "type_cooldown_hours": {item["key"]: item["cooldown_hours"] for item in public_directory()},
                "expected_epoch": ctx.get("policy_epoch") if ctx is not None else None,
            },
            romance_gate=romance_gate, binding_gate=binding_gate)
        if status == "duplicate":
            # A replay must never fake success in health or blacklist counts.
            self._record_health("duplicate_event", text_source, parsed)
            logger.info("[关系弧线] settlement=duplicate_event")
            return
        if status == "noop":
            self._record_health("zero_after_policy", text_source, parsed)
            logger.info("[关系弧线] settlement=zero_after_policy binding=%s", info.get("binding_reason"))
            return
        binding_status = info.get("status", "no_binding")
        # MIS-134 R02/R03: classification follows the WHOLE turn's actual
        # result — real score/safety/binding success is "applied"; a binding
        # rejection with no actual change is "binding_rejected". Both are
        # recorded; neither is silently dropped. info["applied"] already
        # carries the actual deltas (paused turns report {}).
        actual_applied = info.get("applied", {}) or {}
        # R02: a non-empty dict may still be all zeros (e.g. a delta fully
        # consumed by the rolling window) — only non-zero values are real.
        actual_action = any(actual_applied.values()) or info.get("timed_safety") is not None             or binding_status in ("binding_created", "binding_upgraded")
        if str(binding_status).startswith("binding_rejected") and not actual_action:
            settlement_outcome = "binding_rejected"
        elif not actual_action:
            # A paused receipt or otherwise change-less committed round.
            settlement_outcome = "zero_after_policy"
        else:
            settlement_outcome = "applied"
        self._record_health(settlement_outcome, text_source, parsed)
        blacklist=self.config.get("auto_blacklist",{})
        if blacklist.get("enabled") and self.store.settlement_event_count(identity,scope_kind,scope_id) >= int(blacklist["settlement_limit"]):
            self.store.blacklist_settlement(identity,scope_kind,scope_id,"settlement_limit")
            logger.info("[关系弧线] settlement=blacklist_added")
        logger.info("[关系弧线] settlement=%s dimensions=%s binding=%s", settlement_outcome, ",".join(sorted(key for key,value in actual_applied.items() if value)), binding_status)


    # MIS-117: decorated chat entries live in this class body. The host
    # registers a handler under the function's __module__ captured at
    # decoration time and binds/dispatches it only for the plugin registered
    # under that exact module; mixin-defined commands would never be claimed.
    # Implementation helpers stay in commands.py.
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
        tier_label = {"romantic_partner": "恋人", "spouse": "此生挚爱"}
        for row in rows:
            changes = json.loads(row["applied_json"])
            shown = "、".join(
                f"{DISPLAY.get(key, key)} {change / 10:+.1f}"
                for key, change in changes.items()
                if change and (key != "romance_interest" or romance_visible))
            suffix = f"：{row['reason']}" if show_reason and row.get("reason") else ""
            # MIS-134 C08: the relationship outcome of the turn is part of the
            # record, not just the score. Romance tier names follow the same
            # hiding policy as the rest of the romance state.
            binding_result = ""
            try:
                binding_notes = (json.loads(row["notes_json"]).get("binding") or {}).get("notes", [])
            except (ValueError, TypeError):
                binding_notes = []
            for note in binding_notes:
                # MIS-134 R04/R05: every creation names its type via the fixed
                # directory; the hiding projection applies to the EVENT
                # MEANING, not just the trailing tier name — a romance-tier
                # build or the unique upgrade edge leaves no identifiable
                # marker in groups or hidden private chats, while rejection
                # reasons (tier-free protocol codes) stay visible.
                if note.startswith("binding_created:"):
                    rel_type = get_type(note.split(":", 1)[1])
                    if rel_type and (rel_type.category != "romance" or romance_visible):
                        binding_result = f"；正式关系建立：{rel_type.label}"
                elif note == "binding_upgraded:romantic_partner->spouse":
                    if romance_visible:
                        binding_result = "；正式关系已升级：恋人 → 此生挚爱"
                elif note.startswith("binding_rejected:"):
                    binding_result = "；关系提案未通过（" + note.split(":", 1)[1] + "）"
            lines.append(f"- {shown or '无变化'}{suffix}{binding_result}")
        yield event.plain_result("\n".join(lines))
    @filter.command("关系", alias={"关系状态"})
    async def relation(self, event: AstrMessageEvent):
        if not self._capability_allowed(event,"self_query"):
            yield self._capability_denied(event); return
        scope_kind, scope_id = self._scope(event); identity=self._identity(event)
        account = self.store.account(identity, scope_kind, scope_id)
        timed = self.store.active_timed_safety(identity, scope_kind, scope_id)
        yield event.plain_result("【关系状态】\n" + self._summary(account["values"], self._effective_state(identity,scope_kind,scope_id,account["state"]), str(event.get_sender_id()) in self.admins, base_safety=account["state"].get("interaction_safety","normal"), timed=timed))

    async def _decay_loop(self):
        while True:
            try:
                interval=max(1,int(self.config["decay"]["interval_minutes"]))*60
                last=self.store.get_scheduler_state("decay_last_run")
                now=time.time()
                wait=0.0 if last is None or now-float(last)>=interval else interval-(now-float(last))
                if wait>0:
                    await asyncio.sleep(wait)
                decay=self.config["decay"]
                # Persisted due-check + marker share the decay transaction, so a
                # restart can never apply the same period twice (MIS-96).
                self.store.decay_if_due(floors=decay["floors"],step=decay["step"],
                                        inactive_before=time.time()-decay["inactive_hours"]*3600,
                                        scope_allowed=self._stored_scope_allowed,
                                        interval_seconds=interval)
            except asyncio.CancelledError: raise
            except Exception:
                logger.exception("[关系弧线] decay scheduler failed; retrying")
            await asyncio.sleep(1)

    def _decay_signature(self) -> str:
        return json.dumps(self.config.get("decay", {}), sort_keys=True)

    def _backup_signature(self) -> str:
        return json.dumps(self.config.get("backup", {}), sort_keys=True)

    def _run_backup_cycle(self) -> None:
        """MIS-95/MIS-117: one full auto snapshot (sqlite + config + manifest)
        + retention rotation, with run status."""
        backup=self.config["backup"]
        path=self._create_full_backup("auto")
        rotated=self.store.cleanup_auto_backups(int(backup["retention_hours"]))
        interval=max(1,int(backup["interval_hours"]))*3600
        self._backup_state.update({"last_run":time.time(),"last_success":True,"last_error":None,
                                   "last_file":path.name,"rotated":rotated,"next_run":time.time()+interval})

    async def _backup_loop(self):
        while True:
            try:
                self._run_backup_cycle()
            except asyncio.CancelledError: raise
            except Exception as exc:
                self._backup_state.update({"last_run":time.time(),"last_success":False,"last_error":str(exc)[:120]})
                logger.exception("[关系弧线] backup scheduler failed; retrying")
            await asyncio.sleep(max(1,int(self.config["backup"]["interval_hours"]))*3600)

    def _create_full_backup(self, kind: str) -> Path:
        """MIS-95: SQLite snapshot plus config copy plus a verifiable manifest
        (schema/config versions, integrity check, sha256 of both files)."""
        import hashlib
        import shutil
        import sqlite3
        sqlite_path=self.store.backup_now(kind)
        stem=sqlite_path.stem
        config_dst=sqlite_path.with_name(stem+".config.json")
        shutil.copy2(self.config_mgr.path, config_dst)
        check=sqlite3.connect(sqlite_path)
        try:
            integrity=check.execute("PRAGMA integrity_check").fetchone()[0]
            schema_version=int(check.execute("PRAGMA user_version").fetchone()[0])
        finally:
            check.close()
        def digest(file: Path) -> str:
            return hashlib.sha256(file.read_bytes()).hexdigest()
        manifest={"created_at":time.time(),"schema_version":schema_version,
                  "config_version":self.config["config_version"],"integrity":integrity,
                  "binding_policy":self.store.policy_status(),
                  "sha256":{"sqlite":digest(sqlite_path),"config":digest(config_dst)}}
        sqlite_path.with_name(stem+".manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=1),encoding="utf-8")
        return sqlite_path

    async def _restart_decay_scheduler(self):
        await self._restart_schedulers()

    async def _restart_schedulers(self):
        # MIS-96: signature-aware restart - unrelated config saves keep the
        # running tasks and their period timing untouched.
        for task_name, sig_attr, signer in (("_decay_task", "_decay_sig", self._decay_signature),
                                            ("_backup_task", "_backup_sig", self._backup_signature)):
            signature = signer()
            if getattr(self, sig_attr, None) == signature and getattr(self, task_name) is not None:
                continue
            task = getattr(self, task_name)
            if task:
                task.cancel()
                try: await task
                except asyncio.CancelledError: pass
                setattr(self, task_name, None)
            setattr(self, sig_attr, signature)
        if self.config.get("decay",{}).get("enabled") and self._decay_task is None:
            self._decay_task=self._spawn(self._decay_loop(),"relation-arc-decay")
        if self.config.get("backup",{}).get("enabled") and self._backup_task is None:
            self._backup_task=self._spawn(self._backup_loop(),"relation-arc-backup")

    async def terminate(self):
        for task_name in ("_decay_task", "_backup_task"):
            task = getattr(self, task_name)
            if task:
                task.cancel()
                try: await task
                except asyncio.CancelledError: pass
                setattr(self, task_name, None)
        self.store.close()
        logger.info("[关系弧线] 已清理运行时资源。")







    async def _api_bindings(self):
        from quart import request, jsonify
        if request.method == "POST":
            payload=await request.get_json()
            if not isinstance(payload,dict) or payload.get("action") != "end": return jsonify({"error":"仅支持 end"}),400
            binding_id=str(payload.get("binding_id","")); scope=self.store.binding_scope(binding_id)
            if not scope or not self._stored_scope_allowed(*scope): return jsonify({"error":"当前 scope 未开放"}),403
            if not self.store.end_binding(binding_id, actor="page_administrator"): 
                return jsonify({"error":"未找到可结束的 active 正式关系"}),404
            return jsonify({"success":True})
        scope_filter=request.args.get("scope")
        if scope_filter not in {"global","session"}: scope_filter=None
        status=request.args.get("status")
        if status not in {"active","ended"}: status=None
        # MIS-98: server-side pagination and filtering for bindings.
        try: page=max(1,int(request.args.get("page", 1)))
        except ValueError: page=1
        try: page_size=max(1,min(int(request.args.get("page_size", 100)), self.store.MAX_PAGE_SIZE))
        except ValueError: return jsonify({"error":"page_size 必须是整数"}),400
        total=self.store.count_bindings_page(scope_kind=scope_filter,status=status,scope_allowed=self._stored_scope_allowed)
        bindings=[]
        for item in self.store.list_bindings_page(page=page,page_size=page_size,scope_kind=scope_filter,status=status,scope_allowed=self._stored_scope_allowed):
            relationship_type=get_type(item["type_key"])
            bindings.append({**item,"label":relationship_type.label if relationship_type else item["type_key"]})
        return jsonify({"bindings":bindings,"directory":public_directory(),"page":page,"page_size":page_size,"total":total,"pages":max(1,(total+page_size-1)//page_size)})


