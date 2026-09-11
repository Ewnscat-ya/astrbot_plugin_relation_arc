from __future__ import annotations

import copy
import json
import shutil
import time
from pathlib import Path
from typing import Any

CONFIG_VERSION = 6

DEFAULT_CONFIG: dict[str, Any] = {
    "config_version": CONFIG_VERSION,
    "enabled": True,
    "is_global_relation": True,
    "private_enabled": True,
    "group_enabled": True,
    "group_require_at_or_reply": True,
    "allowed_sessions": [],
    "blocked_sessions": [],
    "display_mode": "semi_transparent",
    "show_recent_change": True,
    "llm_judgment_enabled": True,
    "raw_delta_limit": 10,
    "repeat_window_minutes": 180,
    "repeat_factors": [1.0, 0.6, 0.3, 0.0],
    "initial_values": {
        "trust": 400, "respect": 500, "comfort": 450,
        "closeness": 150, "resonance": 100, "romance_interest": 0,
    },
    "anti_farm": {
        "rolling_window_hours": 24,
        "positive_change_ceiling": {
            "trust": 50, "respect": 50, "comfort": 40,
            "closeness": 30, "resonance": 30, "romance_interest": 15,
        },
    },
    "romance": {
        "global_enabled": True,
        "default_policy": "hidden",
        "eligibility_thresholds": {
            "trust": 550, "comfort": 550, "closeness": 450, "resonance": 400,
        },
        "safety_default": "normal",
    },
    "query_permission": {"group_normal_user": True, "private_normal_user": True},
    # C0: llm_auto is the current default; model may never lower base safety.
    "interaction_safety": {"llm_mode": "llm_auto", "auto_duration_minutes": 30},
    # Automatic rotation is deliberately restricted to backup/auto by RelationStore.
    "backup": {"enabled": True, "interval_hours": 6, "retention_hours": 168},
    # v5: local-only health counters. They record categories/counts, never messages,
    # identities, evidence, reasons, or model reasoning.
    "protocol_health": {"enabled": True, "retention_days": 30},
    "decay": {"enabled": False, "interval_minutes": 60, "inactive_hours": 168, "step": 5, "floors": {"trust": 0, "respect": 0, "comfort": 0, "closeness": 0, "resonance": 0, "romance_interest": 0}},
    "auto_blacklist": {"enabled": False, "settlement_limit": 100}, 
}


def _merge(base: dict, incoming: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in incoming.items():
        result[key] = _merge(result[key], value) if key in result and isinstance(result[key], dict) and isinstance(value, dict) else value
    return result


class PluginConfigManager:
    """Owns only Relation Arc's standalone JSON configuration.

    Favour inspired the explicit legacy backup-before-migration sequence. This
    implementation keeps migration backups in a protected plugin-local folder.
    """

    def __init__(self, plugin_dir: Path, data_root: Path):
        del plugin_dir  # retain v4 constructor shape.
        self.data_dir = data_root / "plugin_data" / "astrbot_plugin_relation_arc"
        self.path = self.data_dir / "config.json"
        self.migration_dir = self.data_dir / "backups" / "migration"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.migration_dir.mkdir(parents=True, exist_ok=True)
        self.config: dict[str, Any] = {}
        self.migration_events: list[dict[str, Any]] = []

    def _protected_backup(self, old_version: int) -> Path:
        stamp = f"{time.strftime('%Y%m%d_%H%M%S')}_{time.time_ns()}"
        destination = self.migration_dir / f"config_v{old_version}_to_v{CONFIG_VERSION}_{stamp}.json"
        shutil.copy2(self.path, destination)
        return destination

    def _migrate(self, raw: dict[str, Any]) -> dict[str, Any]:
        old_version = raw.get("config_version", 0)
        if type(old_version) is not int: raise ValueError("config_version must be an integer")
        if old_version > CONFIG_VERSION:
            raise ValueError(f"unsupported future config version: {old_version}")
        if old_version >= CONFIG_VERSION:
            return _merge(DEFAULT_CONFIG, raw)
        backup = self._protected_backup(old_version)
        migrated = _merge(DEFAULT_CONFIG, raw)
        migrated["config_version"] = CONFIG_VERSION
        self.migration_events.append({
            "component": "config", "from_version": old_version,
            "to_version": CONFIG_VERSION, "backup": backup.name,
        })
        return migrated

    def load_or_create(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("config root must be an object")
                candidate = self._migrate(loaded)
                self._validate(candidate)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                # Never overwrite malformed or future configuration with defaults.
                raise RuntimeError(f"Relation Arc config cannot be loaded safely: {exc}") from exc
        else:
            candidate = copy.deepcopy(DEFAULT_CONFIG)
            self._validate(candidate)
        self.save(candidate)
        self.config.clear()
        self.config.update(candidate)
        return self.config

    def save(self, candidate: dict[str, Any] | None = None) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.config if candidate is None else candidate, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def update(self, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("config must be an object")
        if "config_version" in value and type(value["config_version"]) is not int:
            raise ValueError("config_version must be an integer")
        # Pages submits a full form today, but v5 accepts safe partial patches too.
        # Merge from the active config so omitted keys cannot silently reset.
        candidate = _merge(self.config or DEFAULT_CONFIG, value)
        candidate["config_version"] = CONFIG_VERSION
        self._validate(candidate)
        # Publish only after atomic replacement; keep caller root aliases live.
        self.save(candidate)
        self.config.clear()
        self.config.update(candidate)
        return self.config

    @staticmethod
    def _validate(config: dict[str, Any]) -> None:
        def boolean(key: str) -> None:
            if not isinstance(config.get(key), bool): raise ValueError(f"{key} must be boolean")
        for key in ("enabled","is_global_relation","private_enabled","group_enabled","group_require_at_or_reply","llm_judgment_enabled"):
            boolean(key)
        if type(config.get("raw_delta_limit")) is not int or not 1 <= config["raw_delta_limit"] <= 10: raise ValueError("raw_delta_limit must be 1..10")
        if type(config.get("repeat_window_minutes")) is not int or not 1 <= config["repeat_window_minutes"] <= 10080: raise ValueError("repeat_window_minutes must be 1..10080")
        factors=config.get("repeat_factors")
        if not isinstance(factors,list) or not 1 <= len(factors) <= 32 or any(not isinstance(x,(int,float)) or isinstance(x,bool) or not 0 <= x <= 1 for x in factors): raise ValueError("repeat_factors must be 1..32 numbers in 0..1")
        for key in ("allowed_sessions","blocked_sessions"):
            if not isinstance(config.get(key),list) or any(not isinstance(x,str) or not x.strip() for x in config[key]): raise ValueError(f"{key} must be string list")
        safety=config.get("interaction_safety", {})
        if not isinstance(safety,dict) or safety.get("llm_mode") not in ("administrator_only","llm_suggest","llm_auto"): raise ValueError("interaction_safety.llm_mode invalid")
        if type(safety.get("auto_duration_minutes")) is not int or not 1 <= safety["auto_duration_minutes"] <= 10080: raise ValueError("interaction_safety.auto_duration_minutes invalid")
        query=config.get("query_permission", {})
        if not isinstance(query,dict) or not isinstance(query.get("group_normal_user"),bool) or not isinstance(query.get("private_normal_user"),bool): raise ValueError("query_permission invalid")
        initial=config.get("initial_values")
        if not isinstance(initial,dict) or set(initial)!={"trust","respect","comfort","closeness","resonance","romance_interest"} or any(type(v) is not int or not 0<=v<=1000 for v in initial.values()): raise ValueError("initial_values must contain six integer dimensions in 0..1000")
        anti=config.get("anti_farm",{}); ceilings=anti.get("positive_change_ceiling") if isinstance(anti,dict) else None
        if not isinstance(anti,dict) or type(anti.get("rolling_window_hours")) is not int or not 1<=anti["rolling_window_hours"]<=8760 or not isinstance(ceilings,dict) or any(k not in initial or type(v) is not int or v<0 or v>1000 for k,v in ceilings.items()): raise ValueError("anti_farm invalid")
        romance=config.get("romance",{}); thresholds=romance.get("eligibility_thresholds") if isinstance(romance,dict) else None
        if not isinstance(romance,dict) or not isinstance(romance.get("global_enabled"),bool) or romance.get("default_policy") not in ("hidden","observing","shown") or romance.get("safety_default") not in ("normal","slow_down","pause_intimacy") or not isinstance(thresholds,dict) or any(k not in initial or type(v) is not int or not 0<=v<=1000 for k,v in thresholds.items()): raise ValueError("romance invalid")
        health=config.get("protocol_health",{})
        if not isinstance(health,dict) or not isinstance(health.get("enabled"),bool) or type(health.get("retention_days")) is not int or not 1<=health["retention_days"]<=3650: raise ValueError("protocol_health invalid")
        decay=config.get("decay",{}); floors=decay.get("floors") if isinstance(decay,dict) else None
        if not isinstance(decay,dict) or not isinstance(decay.get("enabled"),bool) or type(decay.get("interval_minutes")) is not int or not 1<=decay["interval_minutes"]<=10080 or type(decay.get("inactive_hours")) is not int or not 1<=decay["inactive_hours"]<=87600 or type(decay.get("step")) is not int or not 1<=decay["step"]<=1000 or not isinstance(floors,dict) or set(floors)!=set(initial) or any(type(v) is not int or not 0<=v<=1000 for v in floors.values()): raise ValueError("decay invalid")
        blacklist=config.get("auto_blacklist",{})
        if not isinstance(blacklist,dict) or not isinstance(blacklist.get("enabled"),bool) or type(blacklist.get("settlement_limit")) is not int or not 1<=blacklist["settlement_limit"]<=100000: raise ValueError("auto_blacklist invalid")
        backup=config.get("backup",{})
        if not isinstance(backup,dict) or not isinstance(backup.get("enabled"),bool) or type(backup.get("interval_hours")) is not int or not 1<=backup["interval_hours"]<=8760 or type(backup.get("retention_hours")) is not int or not 1<=backup["retention_hours"]<=87600: raise ValueError("backup invalid")
