from __future__ import annotations

import json
import os
import hashlib
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .relation_engine import DIMENSIONS, DEFAULT_VALUES, apply_delta, clamp
from .relationship_types import get_type

SCHEMA_VERSION = 12
BACKUP_KINDS = {"auto", "manual", "migration", "pre_restore"}
SAFETY_RANK = {"normal": 0, "slow_down": 1, "pause_intimacy": 2}



class RelationStore:
    """Small synchronous SQLite store; event handlers use only bounded local I/O."""

    def __init__(self, data_dir: Path, config: dict[str, Any] | None = None):
        # Keep the manager-owned root reference for successfully saved hot updates.
        self.config = config if config is not None else {}
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "relation_arc.sqlite3"
        self.backup_dir = data_dir / "backups"
        for kind in BACKUP_KINDS:
            (self.backup_dir / kind).mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.migration_events: list[dict[str, Any]] = []
        # MIS-124 R1: set when a desired policy activation was refused (R2
        # fills the activation machinery); surfaced read-only via policy_status.
        self.policy_activation_error: dict[str, Any] | None = None
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _backup_path(self, kind: str, prefix: str = "relation_arc") -> Path:
        if kind not in BACKUP_KINDS:
            raise ValueError("unknown backup kind")
        return self.backup_dir / kind / f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{time.time_ns()}.sqlite3"

    def _sqlite_backup(self, destination: Path) -> Path:
        with self.lock:
            source = self._conn()
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
        return destination

    def _init(self) -> None:
        existed = self.path.exists() and self.path.stat().st_size > 0
        old_version = 0
        if existed:
            # Read-only probe on a bare connection: no journal-mode or schema
            # writes happen before the database's own version is known.
            probe = sqlite3.connect(self.path, timeout=10)
            try:
                try:
                    old_version = int(probe.execute("PRAGMA user_version").fetchone()[0])
                except sqlite3.DatabaseError as exc:
                    raise RuntimeError("relation database is not a readable SQLite file; refusing to open or modify it") from exc
            finally:
                probe.close()
            if old_version > SCHEMA_VERSION:
                # Never downgrade a newer database: refuse while the file keeps
                # its version and data untouched.
                raise ValueError(f"unsupported future database schema version {old_version}; this build supports up to {SCHEMA_VERSION}")
            migration_backup = self._sqlite_backup(self._backup_path("migration", f"schema_v{old_version}_to_v{SCHEMA_VERSION}")) if old_version < SCHEMA_VERSION else None
        else:
            migration_backup = None
        with self._connection() as conn:
            # One explicit transaction: any failure rolls the whole schema
            # change back, so an interrupted migration never leaves a
            # half-upgraded database behind.
            conn.execute("BEGIN IMMEDIATE")
            self._apply_schema_ddl(conn)
            self._stamp_schema_version(conn, old_version, migration_backup.name if migration_backup else "")
            self._ensure_active_policy_row(conn)
            self._reconcile_binding_constraints(conn)

    def _apply_schema_ddl(self, conn) -> None:
        """Idempotent schema bring-up shared by fresh init and restore staging
        (MIS-117): CREATE IF NOT EXISTS for every table and index plus the
        column patches, safe to run on any copy from version 1 upward."""
        conn.execute("CREATE TABLE IF NOT EXISTS accounts (identity TEXT NOT NULL, scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL DEFAULT '', values_json TEXT NOT NULL, paused INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL, state_json TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(identity,scope_kind,scope_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, identity TEXT NOT NULL, scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL, source_kind TEXT NOT NULL, evidence TEXT NOT NULL, reason TEXT NOT NULL, requested_json TEXT NOT NULL, applied_json TEXT NOT NULL, notes_json TEXT NOT NULL, actor TEXT NOT NULL, created_at REAL NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS migration_log (id INTEGER PRIMARY KEY AUTOINCREMENT, component TEXT NOT NULL, from_version INTEGER NOT NULL, to_version INTEGER NOT NULL, backup_name TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS protocol_health (id INTEGER PRIMARY KEY AUTOINCREMENT, outcome TEXT NOT NULL, source TEXT NOT NULL, bare_recovery INTEGER NOT NULL DEFAULT 0, effect_count INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)")
        # C1: automatic safety is separate from administrator-owned base state.
        conn.execute("CREATE TABLE IF NOT EXISTS timed_safety (identity TEXT NOT NULL, scope_kind TEXT NOT NULL CHECK(scope_kind IN ('global','session')), scope_id TEXT NOT NULL DEFAULT '', level TEXT NOT NULL CHECK(level IN ('slow_down','pause_intimacy')), expires_at REAL NOT NULL, generation INTEGER NOT NULL, source TEXT NOT NULL CHECK(source='llm_auto'), created_at REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(identity,scope_kind,scope_id))")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_timed_safety_expiry ON timed_safety(expires_at)")
        # C4 is settlement-only: this table is never consulted by ordinary chat hooks.
        conn.execute("CREATE TABLE IF NOT EXISTS settlement_blacklist (identity TEXT NOT NULL, scope_kind TEXT NOT NULL CHECK(scope_kind IN ('global','session')), scope_id TEXT NOT NULL DEFAULT '', reason_code TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(identity,scope_kind,scope_id))")
        # B0: a separate ledger from accounts. It intentionally starts empty:
        # only B2's validated same-turn proposal chain may create a binding.
        conn.execute("CREATE TABLE IF NOT EXISTS relationship_bindings (binding_id TEXT PRIMARY KEY, identity TEXT NOT NULL, scope_kind TEXT NOT NULL CHECK(scope_kind IN ('global','session')), scope_id TEXT NOT NULL DEFAULT '', type_key TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','ended')), unique_scope TEXT NOT NULL DEFAULT '', origin_event_id TEXT NOT NULL DEFAULT '', state_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, updated_at REAL NOT NULL, ended_at REAL)")
        # MIS-96: persisted scheduler period markers (decay_last_run), so a
        # restart or duplicate start can never apply the same period twice.
        conn.execute("CREATE TABLE IF NOT EXISTS scheduler_state (key TEXT PRIMARY KEY, value REAL NOT NULL)")
        # MIS-124 R1: the authoritative active binding policy lives in the
        # database (single row); the JSON config only holds the desired
        # values. The epoch is never reused, so in-flight turns can detect a
        # policy change between inject and settlement.
        conn.execute("CREATE TABLE IF NOT EXISTS binding_policy (id INTEGER PRIMARY KEY CHECK(id=1), exclusivity TEXT NOT NULL CHECK(exclusivity IN ('none','scope')), rebind_cooldown TEXT NOT NULL CHECK(rebind_cooldown IN ('off','type_default')), epoch INTEGER NOT NULL, updated_at REAL NOT NULL)")
        # MIS-125 R2: historical same-user conflicts are kept and diagnosed,
        # never deleted; rows here block only the writes that would aggravate
        # the conflict for that identity/scope.
        conn.execute("CREATE TABLE IF NOT EXISTS binding_conflicts (identity TEXT NOT NULL, scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL, kind TEXT NOT NULL, conflict_count INTEGER NOT NULL, first_seen REAL NOT NULL, PRIMARY KEY(identity,scope_kind,scope_id,kind))")
        # MIS-134 C01/C03: cross-instance mutual exclusion between the restore
        # final phase and policy activation. Single row, time-bounded: a
        # crashed restore never blocks activation forever.
        conn.execute("CREATE TABLE IF NOT EXISTS restore_lease (id INTEGER PRIMARY KEY CHECK(id=1), until REAL NOT NULL, owner TEXT NOT NULL DEFAULT '')")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_binding_scope_status ON relationship_bindings(identity,scope_kind,scope_id,status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_binding_unique_active ON relationship_bindings(scope_kind,scope_id,unique_scope,status)")
        # MIS-125 R2: the blanket unique index enforced cross-user romance
        # exclusivity unconditionally. Policy-gated constraints replace it; a
        # backup that carries the old index must not re-lock relationships.
        conn.execute("DROP INDEX IF EXISTS idx_binding_unique_active_uq")
        # MIS-97: settlement window scans and health retention filter by
        # these columns on every turn; without them events degrade to a
        # full table scan as data grows.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_window ON events(identity,scope_kind,scope_id,actor,created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_protocol_health_created ON protocol_health(created_at)")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
        if "state_json" not in columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN state_json TEXT NOT NULL DEFAULT '{}'")
        if "last_interaction" not in columns:
            # C3 deliberately distinguishes accepted interaction time from any edit/read timestamp.
            conn.execute("ALTER TABLE accounts ADD COLUMN last_interaction REAL NOT NULL DEFAULT 0")
        binding_columns = {row[1] for row in conn.execute("PRAGMA table_info(relationship_bindings)")}
        if "end_reason" not in binding_columns:
            # MIS-124 R1 groundwork (consumed by R3): 'upgraded' marks a romance
            # replacement that is not a real breakup and never starts a cooldown.
            conn.execute("ALTER TABLE relationship_bindings ADD COLUMN end_reason TEXT NOT NULL DEFAULT ''")

    def _ensure_active_policy_row(self, conn) -> None:
        """MIS-124 R1: the authoritative policy row must exist before any
        binding write. The first row seeds from the validated desired config;
        a missing field falls back to the 0eb5859 behaviour."""
        if conn.execute("SELECT 1 FROM binding_policy WHERE id=1").fetchone():
            return
        desired = self.config.get("binding_policy") if isinstance(self.config.get("binding_policy"), dict) else {}
        conn.execute("INSERT INTO binding_policy(id,exclusivity,rebind_cooldown,epoch,updated_at) VALUES(1,?,?,1,?)",
                     (desired.get("exclusivity", "scope"), desired.get("rebind_cooldown", "type_default"), time.time()))

    def active_binding_policy(self) -> dict[str, Any]:
        """Authoritative effective policy. The JSON config only holds desired
        values; the database row decides what binding writes enforce."""
        with self.lock, self._connection() as conn:
            row = conn.execute("SELECT exclusivity,rebind_cooldown,epoch,updated_at FROM binding_policy WHERE id=1").fetchone()
            if not row:
                self._ensure_active_policy_row(conn)
                row = conn.execute("SELECT exclusivity,rebind_cooldown,epoch,updated_at FROM binding_policy WHERE id=1").fetchone()
        return {"exclusivity": row["exclusivity"], "rebind_cooldown": row["rebind_cooldown"],
                "epoch": int(row["epoch"]), "updated_at": float(row["updated_at"])}

    def policy_status(self) -> dict[str, Any]:
        """MIS-124 R1: read-only runtime state for the Pages API (desired vs
        effective vs pending). Never writable through the config endpoint."""
        effective = self.active_binding_policy()
        desired = self.config.get("binding_policy") if isinstance(self.config.get("binding_policy"), dict) else {}
        source = self.config.get("binding_policy_source") if isinstance(self.config.get("binding_policy_source"), dict) else {}
        return {
            "desired": {"exclusivity": desired.get("exclusivity"), "rebind_cooldown": desired.get("rebind_cooldown")},
            "effective": {"exclusivity": effective["exclusivity"], "rebind_cooldown": effective["rebind_cooldown"],
                          "epoch": effective["epoch"]},
            "pending": (desired.get("exclusivity") != effective["exclusivity"])
                       or (desired.get("rebind_cooldown") != effective["rebind_cooldown"]),
            "activation_error": self.policy_activation_error,
            "source": {"exclusivity": source.get("exclusivity"), "rebind_cooldown": source.get("rebind_cooldown")},
        }

    def _stamp_schema_version(self, conn, from_version: int, backup_name: str) -> None:
        if from_version < SCHEMA_VERSION:
            conn.execute("INSERT INTO migration_log(component,from_version,to_version,backup_name,created_at) VALUES(?,?,?,?,?)", ("sqlite", from_version, SCHEMA_VERSION, backup_name, time.time()))
            self.migration_events.append({"component":"sqlite", "from_version":from_version, "to_version":SCHEMA_VERSION, "backup":backup_name})
        # SQLite PRAGMA cannot bind parameters, so the version literal is
        # written directly and verified against SCHEMA_VERSION on read-back;
        # any drift fails loudly instead of silently mislabelling a database.
        conn.execute("PRAGMA user_version=12")
        written = conn.execute("PRAGMA user_version").fetchone()[0]
        if written != SCHEMA_VERSION:
            raise RuntimeError(f"schema version drift: user_version={written} != SCHEMA_VERSION={SCHEMA_VERSION}")

    def _reconcile_binding_constraints(self, conn) -> dict[str, Any]:
        """MIS-125 R2 / MIS-134 C02+C04: align the constraint objects with
        the authoritative policy row, inside the caller's transaction.

        Diagnosis always runs BEFORE any constraint object is created: a
        ledger that already carries same-user duplicates or a multi-user
        romance occupancy (possible in pre-v12 databases) keeps its rows and
        gets them recorded in binding_conflicts — the affected indexes stay
        absent until the conflicts are resolved, so opening the plugin never
        fails and the administrator can end exact bindings to clear them."""
        policy = self._active_policy_row(conn)
        conflicts = self._same_user_conflicts(conn)
        # MIS-134 C02: the index predicate is scope-level uniqueness among
        # active romance rows, so ANY duplicate there (cross-user or the
        # same user holding two tiers) blocks its creation.
        scope_conflicts = self._scope_exclusive_conflicts(conn) if policy["exclusivity"] == "scope" else []
        conn.execute("DELETE FROM binding_conflicts")
        conn.executemany("INSERT INTO binding_conflicts(identity,scope_kind,scope_id,kind,conflict_count,first_seen) VALUES(?,?,?,?,?,?)",
                         [(row["identity"], row["scope_kind"], row["scope_id"], row["kind"], row["count"], time.time())
                          for row in (*conflicts, *scope_conflicts)])
        if not conflicts:
            self._create_same_user_indexes(conn)
        if policy["exclusivity"] == "scope" and not scope_conflicts:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_binding_romance_scope_uq ON relationship_bindings(scope_kind,scope_id) WHERE status='active' AND type_key IN ('romantic_partner','spouse')")
        else:
            conn.execute("DROP INDEX IF EXISTS idx_binding_romance_scope_uq")
        return {"exclusivity": policy["exclusivity"], "epoch": int(policy["epoch"]),
                "legacy_conflicts": len(conflicts) + len(scope_conflicts)}

    def activate_binding_policy(self, desired: dict[str, Any]) -> dict[str, Any]:
        """MIS-125 R2: activate the desired policy after a reload.

        One BEGIN IMMEDIATE transaction: the cross-user romance index follows
        the new exclusivity and the epoch advances only on a real change (a
        restart with unchanged policy never churns the epoch). An activation
        that would turn exclusivity on while any persisted scope already
        holds romance bindings from several users is refused: the previous
        policy stays effective, the desired JSON is kept and the conflict is
        reported without touching a single relationship row."""
        if not isinstance(desired, dict):
            desired = {}
        wanted_exclusivity = desired.get("exclusivity")
        wanted_cooldown = desired.get("rebind_cooldown")
        with self.lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._active_policy_row(conn)
            self.policy_activation_error = None
            # MIS-134 C03: a running restore holds the lease and will inject
            # the CURRENT policy; an activation racing it could be clobbered
            # by the final copy. Refuse while the lease is valid (desired is
            # kept; ordinary chat is unaffected).
            if self._restore_lock_held():
                self.policy_activation_error = {"message": "恢复正在进行中，请稍后重载激活", "conflict_scopes": 0}
                conn.execute("ROLLBACK")
                row = self._active_policy_row(conn)
                return {"activated": False, "reason": "restore_in_progress", **self.policy_activation_error,
                        "effective": {"exclusivity": row["exclusivity"], "rebind_cooldown": row["rebind_cooldown"],
                                      "epoch": int(row["epoch"])}}
            if wanted_exclusivity == "scope" and current["exclusivity"] != "scope":
                clash_count = int(conn.execute("SELECT COUNT(*) FROM (SELECT scope_kind,scope_id FROM relationship_bindings WHERE status='active' AND type_key IN ('romantic_partner','spouse') GROUP BY scope_kind,scope_id HAVING COUNT(DISTINCT identity)>1)").fetchone()[0])
                if clash_count:
                    self.policy_activation_error = {
                        "message": f"{clash_count} 个 scope 已有多个用户的恋爱关系；请先按 binding_id 结束多余关系，再重载激活",
                        "conflict_scopes": clash_count,
                    }
                    conn.execute("ROLLBACK")
                    row = self._active_policy_row(conn)
                    return {"activated": False, "reason": "legacy_conflict", **self.policy_activation_error,
                            "effective": {"exclusivity": row["exclusivity"], "rebind_cooldown": row["rebind_cooldown"],
                                          "epoch": int(row["epoch"])}}
            changed = (wanted_exclusivity != current["exclusivity"]) or (wanted_cooldown != current["rebind_cooldown"])
            epoch = int(current["epoch"])
            if changed:
                epoch += 1
                conn.execute("UPDATE binding_policy SET exclusivity=?,rebind_cooldown=?,epoch=?,updated_at=? WHERE id=1",
                             (wanted_exclusivity, wanted_cooldown, epoch, time.time()))
            summary = self._reconcile_binding_constraints(conn)
            conn.commit()
        return {"activated": changed, "reason": None if changed else "unchanged",
                "effective": {"exclusivity": summary["exclusivity"],
                              "rebind_cooldown": wanted_cooldown if changed else current["rebind_cooldown"],
                              "epoch": summary["epoch"]},
                "legacy_conflicts": summary["legacy_conflicts"]}

    @staticmethod
    def _active_policy_row(conn) -> sqlite3.Row:
        return conn.execute("SELECT exclusivity,rebind_cooldown,epoch FROM binding_policy WHERE id=1").fetchone()

    @staticmethod
    def _same_user_conflicts(conn) -> list[dict[str, Any]]:
        # MIS-134 C04: the diagnosis key carries the concrete type, so two
        # duplicated types for one identity are two distinct diagnostic rows
        # instead of a primary-key collision.
        rows = conn.execute("SELECT identity,scope_kind,scope_id,'identity_type:'||type_key AS kind,COUNT(*) AS count FROM relationship_bindings WHERE status='active' GROUP BY identity,scope_kind,scope_id,type_key HAVING COUNT(*)>1 UNION ALL SELECT identity,scope_kind,scope_id,'identity_romance' AS kind,COUNT(*) AS count FROM relationship_bindings WHERE status='active' AND type_key IN ('romantic_partner','spouse') GROUP BY identity,scope_kind,scope_id HAVING COUNT(*)>1").fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _scope_exclusive_conflicts(conn) -> list[dict[str, Any]]:
        """MIS-134 C02: scopes violating the cross-user romance uniqueness —
        several users holding romance bindings, or one user holding two tiers
        (both violate the same scope-level index predicate). Recorded with an
        empty identity placeholder; they block new romance writes in that
        scope until an administrator ends the extra bindings by exact
        binding_id."""
        rows = conn.execute("SELECT scope_kind,scope_id,COUNT(*) AS rows_count,COUNT(DISTINCT identity) AS users FROM relationship_bindings WHERE status='active' AND type_key IN ('romantic_partner','spouse') GROUP BY scope_kind,scope_id HAVING COUNT(*)>1").fetchall()
        return [{"identity": "", "scope_kind": row["scope_kind"], "scope_id": row["scope_id"],
                 "kind": "scope_exclusive", "count": int(row["rows_count"])} for row in rows]

    @staticmethod
    def _create_same_user_indexes(conn) -> None:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_binding_identity_type_uq ON relationship_bindings(identity,scope_kind,scope_id,type_key) WHERE status='active'")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_binding_identity_romance_uq ON relationship_bindings(identity,scope_kind,scope_id) WHERE status='active' AND type_key IN ('romantic_partner','spouse')")

    def legacy_binding_conflict_count(self) -> int:
        with self.lock, self._connection() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM binding_conflicts").fetchone()[0])

    def _legacy_conflict_exists(self, conn, identity: str, scope_kind: str, scope_id: str, is_romance: bool = False) -> bool:
        """MIS-134 C02: identity-level conflicts always block; a scope-level
        multi-user romance conflict additionally blocks new romance writes in
        that scope (friend/partner tiers keep working there)."""
        if conn.execute("SELECT 1 FROM binding_conflicts WHERE identity=? AND scope_kind=? AND scope_id=? LIMIT 1", (identity, scope_kind, scope_id)).fetchone():
            return True
        if is_romance and conn.execute("SELECT 1 FROM binding_conflicts WHERE identity='' AND scope_kind=? AND scope_id=? AND kind='scope_exclusive' LIMIT 1", (scope_kind, scope_id)).fetchone():
            return True
        return False

    def _admit_binding(self, conn, binding: dict[str, Any] | None, identity: str, scope_kind: str, scope_id: str, active_policy: sqlite3.Row, expected_epoch: int | None, allow_romance_replace: bool = False, now: float | None = None) -> tuple[dict[str, Any] | None, str]:
        """MIS-125 R2: shared in-transaction admission ladder for every
        binding write path (settlement, store primitive, legacy migration).
        Order: legacy conflicts -> stale policy epoch -> cross-user romance
        exclusion (active policy only) -> same-user romance level -> same-type
        duplicate -> optional rebind cooldown (MIS-134 C06: the cooldown is
        part of the ladder, so no write path can bypass it; ends carrying the
        upgraded marker never seed one). Returns (binding_or_None, status).

        ``allow_romance_replace`` (MIS-126 R3) marks the one legitimate
        romance-tier transition: the backend-recognised upgrade of the
        caller's own active romantic_partner to spouse. Every other tier
        change stays rejected."""
        if binding is None:
            return None, "no_binding"
        rel_type = get_type(binding["type_key"])
        is_romance = bool(rel_type and rel_type.category == "romance")
        if self._legacy_conflict_exists(conn, identity, scope_kind, scope_id, is_romance):
            return None, "binding_rejected:legacy_conflict"
        if expected_epoch is not None and int(expected_epoch) != int(active_policy["epoch"]):
            return None, "binding_rejected:policy_changed"
        if is_romance and active_policy["exclusivity"] == "scope":
            if conn.execute("SELECT 1 FROM relationship_bindings WHERE scope_kind=? AND scope_id=? AND status='active' AND type_key IN ('romantic_partner','spouse') AND identity<>? LIMIT 1", (scope_kind, scope_id, identity)).fetchone():
                return None, "binding_rejected:exclusive"
        own_romance = conn.execute("SELECT type_key FROM relationship_bindings WHERE identity=? AND scope_kind=? AND scope_id=? AND status='active' AND type_key IN ('romantic_partner','spouse') LIMIT 1", (identity, scope_kind, scope_id)).fetchone()
        if is_romance and own_romance is not None and own_romance["type_key"] != binding["type_key"] and not allow_romance_replace:
            # A lower romance tier can never silently replace a higher one.
            return None, "binding_rejected:romance_occupied"
        if conn.execute("SELECT 1 FROM relationship_bindings WHERE identity=? AND scope_kind=? AND scope_id=? AND type_key=? AND status='active' LIMIT 1", (identity, scope_kind, scope_id, binding["type_key"])).fetchone():
            return None, "binding_rejected:duplicate"
        if active_policy["rebind_cooldown"] == "type_default":
            # MIS-134 C06: cooldown counting from the latest REAL end of the
            # same identity/type/scope (upgraded replacements are excluded);
            # durations come from the single type directory.
            current = time.time() if now is None else float(now)
            cooldown_hours = int(get_type(binding["type_key"]).cooldown_hours)
            last_ended = conn.execute("SELECT ended_at FROM relationship_bindings WHERE identity=? AND scope_kind=? AND scope_id=? AND type_key=? AND status='ended' AND (end_reason IS NULL OR end_reason<>'upgraded') ORDER BY ended_at DESC LIMIT 1", (identity, scope_kind, scope_id, binding["type_key"])).fetchone()
            if (cooldown_hours > 0 and last_ended is not None and last_ended["ended_at"] is not None
                    and current - float(last_ended["ended_at"]) < cooldown_hours * 3600):
                return None, "binding_rejected:cooldown"
        return binding, "binding_admitted"

    def _default_values(self) -> dict[str, int]:
        return {**DEFAULT_VALUES, **self.config.get("initial_values", {})}

    @staticmethod
    def _legacy_default_state() -> dict[str, str]:
        return {"romance_policy": "hidden", "romance_state": "hidden", "interaction_safety": "normal"}

    def _default_state(self) -> dict[str, str]:
        romance = self.config.get("romance", {})
        policy = romance.get("default_policy", "hidden")
        return {"romance_policy": policy, "romance_state": policy, "interaction_safety": romance.get("safety_default", "normal")}

    @staticmethod
    def _row_account(row) -> dict[str, Any]:
        values = {**DEFAULT_VALUES, **json.loads(row["values_json"])}
        raw_state = row["state_json"] if "state_json" in row.keys() else "{}"
        try: state = json.loads(raw_state or "{}")
        except json.JSONDecodeError: state = {}
        return {"values": values, "state": {**RelationStore._legacy_default_state(), **state}, "paused": bool(row["paused"]), "revision": row["revision"], "last_interaction":float(row["last_interaction"]) if "last_interaction" in row.keys() else 0.0}

    def _legacy_split_candidates(self, conn) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Shared eligibility for preview and repair (single source of truth).

        A split is repairable only when the legacy account's entire history in
        the scope is exactly one single-dimension administrator event and no
        bindings, timed safety or blacklist rows reference it; anything else is
        real history and is reported (counts only, never identities).
        """
        import re
        rows = conn.execute("SELECT identity,scope_kind,scope_id,revision FROM accounts").fetchall()
        existing = {(r["identity"], r["scope_kind"], r["scope_id"]) for r in rows}
        repairable: list[dict[str, Any]] = []
        diagnostics: dict[str, int] = {}
        for row in rows:
            identity = row["identity"]
            if ":" not in identity:
                continue
            platform, suffix = identity.split(":", 1)
            match = re.fullmatch(r".*\(([^()\s]+)\)", suffix)
            if not match:
                continue
            canonical = f"{platform}:{match.group(1)}"
            scope = (row["scope_kind"], row["scope_id"])
            if (canonical, *scope) not in existing:
                diagnostics["no_canonical_target"] = diagnostics.get("no_canonical_target", 0) + 1
                continue
            if conn.execute("SELECT 1 FROM relationship_bindings WHERE identity=? AND scope_kind=? AND scope_id=? LIMIT 1", (identity, *scope)).fetchone():
                diagnostics["binding_history_present"] = diagnostics.get("binding_history_present", 0) + 1
                continue
            if conn.execute("SELECT 1 FROM timed_safety WHERE identity=? AND scope_kind=? AND scope_id=? LIMIT 1", (identity, *scope)).fetchone():
                diagnostics["timed_safety_present"] = diagnostics.get("timed_safety_present", 0) + 1
                continue
            if conn.execute("SELECT 1 FROM settlement_blacklist WHERE identity=? AND scope_kind=? AND scope_id=? LIMIT 1", (identity, *scope)).fetchone():
                diagnostics["blacklist_present"] = diagnostics.get("blacklist_present", 0) + 1
                continue
            events = conn.execute("SELECT actor,requested_json,applied_json FROM events WHERE identity=? AND scope_kind=? AND scope_id=?", (identity, *scope)).fetchall()
            if len(events) != 1 or events[0]["actor"] != "administrator":
                diagnostics["llm_or_other_history_present"] = diagnostics.get("llm_or_other_history_present", 0) + 1
                continue
            requested = json.loads(events[0]["requested_json"])
            applied = json.loads(events[0]["applied_json"])
            keys = set(requested) | set(applied)
            if len(keys) != 1 or next(iter(keys)) not in DIMENSIONS:
                diagnostics["admin_event_shape_mismatch"] = diagnostics.get("admin_event_shape_mismatch", 0) + 1
                continue
            repairable.append({"legacy_identity": identity, "canonical_identity": canonical, "scope_kind": scope[0], "scope_id": scope[1], "revision": row["revision"], "dimension": next(iter(keys))})
        return repairable, diagnostics

    def legacy_identity_split_preview(self) -> list[dict[str, Any]]:
        """Read-only candidates shaped like ``platform:display(sender_id)``.

        A candidate is listed only when a provable repair would apply: the
        canonical row exists in exactly the same scope and the legacy account's
        whole history is one single-dimension administrator event. No mutation
        is performed here.
        """
        with self.lock, self._connection() as conn:
            repairable, _ = self._legacy_split_candidates(conn)
        return [{key: item[key] for key in ("legacy_identity", "canonical_identity", "scope_kind", "scope_id", "revision")} for item in repairable]

    def legacy_identity_split_diagnostic_counts(self) -> dict[str, int]:
        """Sanitised skip reasons for shape-matched but non-repairable splits.

        Values are counts per reason code; no identity, message, score or
        evidence is ever included.
        """
        with self.lock, self._connection() as conn:
            _, diagnostics = self._legacy_split_candidates(conn)
        return dict(sorted(diagnostics.items()))

    def repair_legacy_identity_splits(self) -> list[dict[str, Any]]:
        """Apply only provable admin-created display(ID) splits atomically.

        The old administrator event recorded a delta even though UI semantics are
        absolute set.  We therefore copy only the changed dimension's final value
        from the legacy row to its canonical row, audit that repair, then delete
        the legacy row and its administrator-only events. Accounts with LLM
        history, bindings, timed safety or blacklist rows are never touched.
        """
        import re
        repaired=[]
        with self.lock, self._connection() as conn:
            candidates, _ = self._legacy_split_candidates(conn)
            for item in candidates:
                identity=item["legacy_identity"]; canonical=item["canonical_identity"]
                scope=(item["scope_kind"], item["scope_id"]); dimension=item["dimension"]
                row=conn.execute("SELECT values_json FROM accounts WHERE identity=? AND scope_kind=? AND scope_id=?",(identity,*scope)).fetchone()
                target=conn.execute("SELECT values_json,revision FROM accounts WHERE identity=? AND scope_kind=? AND scope_id=?",(canonical,*scope)).fetchone()
                if row is None or target is None: continue
                legacy_values=json.loads(row["values_json"]); target_values=json.loads(target["values_json"])
                old=int(target_values.get(dimension,0)); final=int(legacy_values.get(dimension,0)); target_values[dimension]=final; now=time.time()
                conn.execute("UPDATE accounts SET values_json=?,revision=?,updated_at=? WHERE identity=? AND scope_kind=? AND scope_id=?",(json.dumps(target_values),int(target["revision"])+1,now,canonical,*scope))
                event_id=f"identity_repair:{hashlib.sha256(identity.encode()).hexdigest()[:16]}:{time.time_ns()}"
                conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(event_id,canonical,*scope,"migration","","identity normalization repair",json.dumps({dimension:final-old}),json.dumps({dimension:final-old}),json.dumps({"legacy_identity_removed":True}),"migration",now))
                conn.execute("DELETE FROM events WHERE identity=? AND scope_kind=? AND scope_id=? AND actor='administrator'",(identity,*scope))
                conn.execute("DELETE FROM accounts WHERE identity=? AND scope_kind=? AND scope_id=?",(identity,*scope))
                repaired.append({"dimension":dimension,"scope_kind":scope[0],"final_value":final})
        return repaired

    def existing_account(self, identity: str, scope_kind: str = "global", scope_id: str = "") -> dict[str, Any] | None:
        with self.lock, self._connection() as conn:
            row=conn.execute("SELECT * FROM accounts WHERE identity=? AND scope_kind=? AND scope_id=?",(identity,scope_kind,scope_id)).fetchone()
            return self._row_account(row) if row else None

    def account(self, identity: str, scope_kind: str = "global", scope_id: str = "") -> dict[str, Any]:
        with self.lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE identity=? AND scope_kind=? AND scope_id=?", (identity, scope_kind, scope_id)).fetchone()
            if row is None:
                values = self._default_values()
                state = self._default_state()
                conn.execute("INSERT INTO accounts(identity,scope_kind,scope_id,values_json,paused,revision,updated_at,state_json) VALUES(?,?,?,?,?,?,?,?)", (identity, scope_kind, scope_id, json.dumps(values), 0, 0, time.time(), json.dumps(state)))
                return {"values": values, "state": state, "paused": False, "revision": 0}
            return self._row_account(row)

    def count_accounts(self, scope_kind: str | None = None, scope_id: str | None = None) -> int:
        clauses=[]; params=[]
        if scope_kind in {"global","session"}: clauses.append("scope_kind=?");params.append(scope_kind)
        if scope_id is not None: clauses.append("scope_id=?");params.append(scope_id)
        where=(" WHERE "+" AND ".join(clauses)) if clauses else ""
        with self.lock, self._connection() as conn: return int(conn.execute("SELECT count(*) FROM accounts"+where,params).fetchone()[0])

    MAX_PAGE_SIZE = 200

    def _scope_admission(self, scope_allowed, table: str) -> tuple[int, str]:
        """MIS-98: bound-parameter scope admission for the universal paginated
        queries. Returns (global_flag, admitted_session_ids_json). Session ids
        are enumerated from live data and passed as one bound JSON array
        consumed by json_each; the table choice is a literal branch."""
        global_flag = 1 if scope_allowed("global", "") else 0
        with self._connection() as conn:
            if table == "accounts":
                ids = [r["scope_id"] for r in conn.execute("SELECT DISTINCT scope_id FROM accounts WHERE scope_kind='session'")]
            elif table == "events":
                ids = [r["scope_id"] for r in conn.execute("SELECT DISTINCT scope_id FROM events WHERE scope_kind='session'")]
            elif table == "relationship_bindings":
                ids = [r["scope_id"] for r in conn.execute("SELECT DISTINCT scope_id FROM relationship_bindings WHERE scope_kind='session'")]
            else:
                raise ValueError("unknown table")
        keep = [sid for sid in ids if scope_allowed("session", sid)]
        return global_flag, json.dumps(keep)

    def list_accounts_page(self, *, page: int, page_size: int, scope_kind: str | None = None, scope_id: str | None = None, scope_allowed=None) -> list[dict[str, Any]]:
        """MIS-98: server-side pagination behind one single-line literal query.
        Optional filters bind as (? IS NULL OR col=?) pairs; scope admission
        rides on a bound global flag plus a bound json_each array, so no SQL
        text is ever constructed from runtime values."""
        page=max(1,int(page)); page_size=max(1,min(int(page_size),self.MAX_PAGE_SIZE))
        scope_allowed = scope_allowed or (lambda kind, sid: True)
        global_flag, session_ids = self._scope_admission(scope_allowed, "accounts")
        params:list[Any]=[global_flag, session_ids, scope_kind, scope_kind, scope_id, scope_id, page_size, (page-1)*page_size]
        with self.lock, self._connection() as conn:
            rows=conn.execute("SELECT identity,scope_kind,scope_id,values_json,state_json,paused,revision,updated_at FROM accounts WHERE ((scope_kind='global' AND ?) OR (scope_kind='session' AND scope_id IN (SELECT value FROM json_each(?)))) AND (? IS NULL OR scope_kind=?) AND (? IS NULL OR scope_id=?) ORDER BY updated_at DESC, identity ASC, scope_kind ASC, scope_id ASC LIMIT ? OFFSET ?",params).fetchall()
            return [{**dict(row),**self._row_account(row)} for row in rows]

    def count_accounts_page(self, *, scope_kind: str | None = None, scope_id: str | None = None, scope_allowed=None) -> int:
        global_flag, session_ids = self._scope_admission(scope_allowed, "accounts")
        params:list[Any]=[global_flag, session_ids, scope_kind, scope_kind, scope_id, scope_id]
        with self.lock, self._connection() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM accounts WHERE ((scope_kind='global' AND ?) OR (scope_kind='session' AND scope_id IN (SELECT value FROM json_each(?)))) AND (? IS NULL OR scope_kind=?) AND (? IS NULL OR scope_id=?)",params).fetchone()[0])

    def list_bindings_page(self, *, page: int, page_size: int, scope_kind: str | None = None, scope_id: str | None = None, status: str | None = None, scope_allowed=None) -> list[dict[str, Any]]:
        """MIS-98: server-side binding pagination behind one single-line
        literal query; optional filters bind as (? IS NULL OR col=?) pairs."""
        page=max(1,int(page)); page_size=max(1,min(int(page_size),self.MAX_PAGE_SIZE))
        scope_allowed = scope_allowed or (lambda kind, sid: True)
        global_flag, session_ids = self._scope_admission(scope_allowed, "relationship_bindings")
        params:list[Any]=[global_flag, session_ids, scope_kind, scope_kind, scope_id, scope_id, status, status, page_size, (page-1)*page_size]
        with self.lock, self._connection() as conn:
            rows=conn.execute("SELECT binding_id,identity,scope_kind,scope_id,type_key,status,unique_scope,origin_event_id,state_json,created_at,updated_at,ended_at FROM relationship_bindings WHERE ((scope_kind='global' AND ?) OR (scope_kind='session' AND scope_id IN (SELECT value FROM json_each(?)))) AND (? IS NULL OR scope_kind=?) AND (? IS NULL OR scope_id=?) AND (? IS NULL OR status=?) ORDER BY updated_at DESC, binding_id ASC LIMIT ? OFFSET ?",params).fetchall()
            return [dict(row) for row in rows]

    def count_bindings_page(self, *, scope_kind: str | None = None, scope_id: str | None = None, status: str | None = None, scope_allowed=None) -> int:
        scope_allowed = scope_allowed or (lambda kind, sid: True)
        global_flag, session_ids = self._scope_admission(scope_allowed, "relationship_bindings")
        params:list[Any]=[global_flag, session_ids, scope_kind, scope_kind, scope_id, scope_id, status, status]
        with self.lock, self._connection() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM relationship_bindings WHERE ((scope_kind='global' AND ?) OR (scope_kind='session' AND scope_id IN (SELECT value FROM json_each(?)))) AND (? IS NULL OR scope_kind=?) AND (? IS NULL OR scope_id=?) AND (? IS NULL OR status=?)",params).fetchone()[0])

    def list_events_page(self, *, page: int, page_size: int, scope_kind: str | None = None, scope_id: str | None = None, scope_allowed=None) -> list[dict[str, Any]]:
        """MIS-98: audit event pagination behind one single-line literal query."""
        page=max(1,int(page)); page_size=max(1,min(int(page_size),self.MAX_PAGE_SIZE))
        scope_allowed = scope_allowed or (lambda kind, sid: True)
        global_flag, session_ids = self._scope_admission(scope_allowed, "events")
        params:list[Any]=[global_flag, session_ids, scope_kind, scope_kind, scope_id, scope_id, page_size, (page-1)*page_size]
        with self.lock, self._connection() as conn:
            rows=conn.execute("SELECT event_id,identity,scope_kind,scope_id,source_kind,reason,requested_json,applied_json,notes_json,actor,created_at FROM events WHERE ((scope_kind='global' AND ?) OR (scope_kind='session' AND scope_id IN (SELECT value FROM json_each(?)))) AND (? IS NULL OR scope_kind=?) AND (? IS NULL OR scope_id=?) ORDER BY created_at DESC, event_id ASC LIMIT ? OFFSET ?",params).fetchall()
            return [dict(row) for row in rows]

    def count_events_page(self, *, scope_kind: str | None = None, scope_id: str | None = None, scope_allowed=None) -> int:
        scope_allowed = scope_allowed or (lambda kind, sid: True)
        global_flag, session_ids = self._scope_admission(scope_allowed, "events")
        params:list[Any]=[global_flag, session_ids, scope_kind, scope_kind, scope_id, scope_id]
        with self.lock, self._connection() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM events WHERE ((scope_kind='global' AND ?) OR (scope_kind='session' AND scope_id IN (SELECT value FROM json_each(?)))) AND (? IS NULL OR scope_kind=?) AND (? IS NULL OR scope_id=?)",params).fetchone()[0])

    def list_accounts(self, limit: int = 200, scope_kind: str | None = None) -> list[dict[str, Any]]:
        with self.lock, self._connection() as conn:
            if scope_kind in {"global", "session"}:
                rows = conn.execute("SELECT identity,scope_kind,scope_id,values_json,state_json,paused,revision,updated_at FROM accounts WHERE scope_kind=? ORDER BY updated_at DESC LIMIT ?", (scope_kind, max(1, min(limit, 500)))).fetchall()
            else:
                rows = conn.execute("SELECT identity,scope_kind,scope_id,values_json,state_json,paused,revision,updated_at FROM accounts ORDER BY updated_at DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()
            return [{**dict(row), **self._row_account(row)} for row in rows]

    def apply(self, *, event_id: str, identity: str, scope_kind: str, scope_id: str, source_kind: str, evidence: str, reason: str, requested: dict[str, int], applied: dict[str, int], notes: dict[str, Any], actor: str = "llm") -> dict[str, int]:
        with self.lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE identity=? AND scope_kind=? AND scope_id=?", (identity, scope_kind, scope_id)).fetchone()
            if conn.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone():
                return json.loads(row["values_json"]) if row else self._default_values()
            values = self._row_account(row)["values"] if row else self._default_values()
            paused = bool(row["paused"]) if row else False
            state = self._row_account(row)["state"] if row else self._default_state()
            if not paused:
                for key, delta in applied.items():
                    if key in DIMENSIONS:
                        values[key] = clamp(values.get(key, 0) + int(delta))
            now = time.time()
            conn.execute("INSERT INTO accounts(identity,scope_kind,scope_id,values_json,paused,revision,updated_at,state_json,last_interaction) VALUES(?,?,?,?,?,?,?,?,0) ON CONFLICT(identity,scope_kind,scope_id) DO UPDATE SET values_json=excluded.values_json,revision=accounts.revision+1,updated_at=excluded.updated_at,state_json=excluded.state_json", (identity, scope_kind, scope_id, json.dumps(values), int(paused), 1, now, json.dumps(state)))
            conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (event_id, identity, scope_kind, scope_id, source_kind, evidence[:300], reason[:500], json.dumps(requested), json.dumps(applied), json.dumps(notes), actor, now))
            return values

    def apply_turn_with_binding(self, *, event_id: str, identity: str, scope_kind: str, scope_id: str, source_kind: str, evidence: str, reason: str, requested: dict[str, int], applied: dict[str, int], notes: dict[str, Any], binding: dict[str, Any] | None, state_changes: dict[str, str] | None = None, timed_safety: dict[str, Any] | None = None, expected_epoch: int | None = None) -> tuple[dict[str, int], str]:
        """Favour-style post-score validation write, upgraded to one SQLite transaction.

        `binding` must already be a backend-validated fixed type. This method
        never trusts model text and never creates bindings when it is None.
        MIS-125 R2: admission rides the same shared ladder as settlement —
        the authoritative policy, legacy conflicts, cross-user exclusion and
        same-user constraints are re-read inside this transaction.
        """
        with self.lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row=conn.execute("SELECT * FROM accounts WHERE identity=? AND scope_kind=? AND scope_id=?",(identity,scope_kind,scope_id)).fetchone()
            if conn.execute("SELECT 1 FROM events WHERE event_id=?",(event_id,)).fetchone():
                conn.execute("ROLLBACK")
                return (self._row_account(row)["values"] if row else self._default_values()), "duplicate_event"
            values=self._row_account(row)["values"] if row else self._default_values()
            paused=bool(row["paused"]) if row else False; state=self._row_account(row)["state"] if row else self._default_state()
            allowed_state={"interaction_safety"}
            state_changes=state_changes or {}
            if set(state_changes)-allowed_state or state_changes.get("interaction_safety",state.get("interaction_safety")) not in {"normal","slow_down","pause_intimacy"}: raise ValueError("invalid settlement state change")
            if timed_safety is not None and (set(timed_safety) != {"level","duration_minutes"} or timed_safety["level"] not in {"slow_down","pause_intimacy"} or not isinstance(timed_safety["duration_minutes"],int) or not 1 <= timed_safety["duration_minutes"] <= 10080): raise ValueError("invalid timed safety")
            state={**state,**state_changes}
            if not paused:
                for key,delta in applied.items():
                    if key in DIMENSIONS: values[key]=clamp(values.get(key,0)+int(delta))
            now=time.time()
            if binding:
                active_policy=self._active_policy_row(conn)
                binding,admission=self._admit_binding(conn,binding,identity,scope_kind,scope_id,active_policy,expected_epoch,now=now)
                binding_status="binding_created" if binding is not None else admission
                if binding is None:
                    notes={**notes,"binding":{"notes":[admission]}}
            else: binding_status="no_binding"
            conn.execute("INSERT INTO accounts(identity,scope_kind,scope_id,values_json,paused,revision,updated_at,state_json,last_interaction) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(identity,scope_kind,scope_id) DO UPDATE SET values_json=excluded.values_json,revision=accounts.revision+1,updated_at=excluded.updated_at,state_json=excluded.state_json,last_interaction=excluded.last_interaction",(identity,scope_kind,scope_id,json.dumps(values),int(paused),1,now,json.dumps(state),now))
            conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(event_id,identity,scope_kind,scope_id,source_kind,evidence[:300],reason[:500],json.dumps(requested),json.dumps(applied),json.dumps(notes),"llm",now))
            if timed_safety is not None:
                previous=conn.execute("SELECT generation FROM timed_safety WHERE identity=? AND scope_kind=? AND scope_id=?",(identity,scope_kind,scope_id)).fetchone()
                generation=int(previous["generation"])+1 if previous else 1
                conn.execute("INSERT INTO timed_safety(identity,scope_kind,scope_id,level,expires_at,generation,source,created_at,updated_at) VALUES(?,?,?,?,?,?, 'llm_auto',?,?) ON CONFLICT(identity,scope_kind,scope_id) DO UPDATE SET level=excluded.level,expires_at=excluded.expires_at,generation=excluded.generation,updated_at=excluded.updated_at",(identity,scope_kind,scope_id,timed_safety["level"],now+timed_safety["duration_minutes"]*60,generation,now,now))
            if binding:
                conn.execute("INSERT INTO relationship_bindings(binding_id,identity,scope_kind,scope_id,type_key,status,unique_scope,origin_event_id,state_json,created_at,updated_at,ended_at) VALUES(?,?,?,?,?,'active',?,?,?,?,?,NULL)",(binding["binding_id"],identity,scope_kind,scope_id,binding["type_key"],binding.get("unique_scope","") ,event_id,json.dumps({"origin":binding["origin"],"summary":binding.get("summary","")}),now,now))
            return values,binding_status

    def settle_turn(self, *, event_id: str, identity: str, scope_kind: str, scope_id: str, source_kind: str, evidence: str, reason: str, requested_all: dict[str, int], fact_signature: str, repeat_key: str = "", safety_proposal: dict[str, Any] | None, policy: dict[str, Any], romance_gate, binding_gate) -> tuple[dict[str, int], str, dict[str, Any]]:
        """MIS-92: one controlled transaction for a full turn settlement.

        Read state -> policy/window/eligibility recomputation -> writes all
        happen inside a single BEGIN IMMEDIATE transaction, so concurrent
        connections can never settle from stale state. Policy stays in the
        caller via the two pure callbacks (no I/O inside):
          romance_gate(values, state) -> bool
          binding_gate(projected_values, state) -> (binding | None, reason)

        ``evidence``/``reason`` are the audit summary for the event row;
        ``repeat_key`` (MIS-117, restored baseline semantics: the first fact's
        evidence) is what repeat-decay matching compares against window rows,
        falling back to ``fact_signature`` when it is empty. Joining the audit
        summary into the match key silently disabled multi-fact decay.

        Returns (values, status, info); status is one of
        committed / duplicate / noop. A duplicate returns before any write, so
        revision, bindings, timers and settlement counts are untouched.
        """
        now = time.time()
        safety_mode = policy.get("safety_mode", "administrator_only")
        with self.lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM accounts WHERE identity=? AND scope_kind=? AND scope_id=?", (identity, scope_kind, scope_id)).fetchone()
            if conn.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone():
                return ({}, "duplicate", {"reason": "duplicate_event"})
            values = self._row_account(row)["values"] if row else self._default_values()
            base_state = self._row_account(row)["state"] if row else self._default_state()
            paused = bool(row["paused"]) if row else False
            revision = int(row["revision"]) if row else 0
            # Effective safety inside this transaction, expiring only the exact
            # expired automatic row (C1 invariant).
            trow = conn.execute("SELECT level,expires_at,generation FROM timed_safety WHERE identity=? AND scope_kind=? AND scope_id=?", (identity, scope_kind, scope_id)).fetchone()
            existing_timed = dict(trow) if trow else None
            if existing_timed and float(existing_timed["expires_at"]) <= now:
                conn.execute("DELETE FROM timed_safety WHERE identity=? AND scope_kind=? AND scope_id=? AND generation=? AND source='llm_auto' AND expires_at<=?", (identity, scope_kind, scope_id, existing_timed["generation"], now))
                existing_timed = None
            base_safety = base_state.get("interaction_safety", "normal")
            current_safety = base_safety
            if existing_timed and SAFETY_RANK[existing_timed["level"]] > SAFETY_RANK[current_safety]:
                current_safety = existing_timed["level"]
            state = {**base_state, "interaction_safety": current_safety}
            timed_safety = None
            if safety_proposal and safety_mode == "llm_auto":
                proposed = safety_proposal["level"]
                if SAFETY_RANK[proposed] > SAFETY_RANK[current_safety] or (existing_timed and SAFETY_RANK[proposed] == SAFETY_RANK[current_safety] and SAFETY_RANK[proposed] > SAFETY_RANK[base_safety]):
                    timed_safety = {"level": proposed, "duration_minutes": int(policy["auto_duration_minutes"])}
            final_state = {**state, **({"interaction_safety": timed_safety["level"]} if timed_safety else {})}
            romance_allowed = bool(romance_gate(values, final_state))
            applied, notes = {}, {}
            anti_farm = policy.get("anti_farm", {})
            # MIS-97: one window fetch shared by all dimensions. The repeat
            # window (repeat_window_minutes) and the anti-farm window
            # (rolling_window_hours) have different horizons, so rows carry
            # created_at and each use re-filters by time — semantics identical
            # to the previous per-dimension queries (equivalence-tested).
            repeat_since = now - policy["repeat_window_minutes"] * 60
            farm_since = now - int(anti_farm.get("rolling_window_hours", 24)) * 3600
            window = self._window_rows(conn, identity, scope_kind, scope_id, min(repeat_since, farm_since))
            for dimension in DIMENSIONS:
                requested = int(requested_all.get(dimension, 0))
                if not requested:
                    continue
                if dimension == "romance_interest" and not romance_allowed:
                    # Locked romance never moves in either direction; anti-coercion is a hard backend gate.
                    notes[dimension] = {"requested": requested, "repeat_factor": 1.0, "notes": ("romance_locked",), "window_positive": 0}
                    applied[dimension] = 0
                    continue
                repeat_count = self._window_repeat_count(window, repeat_since, dimension, repeat_key, fact_signature)
                result = apply_delta(values.get(dimension, 0), requested, repeat_count, policy["repeat_factors"], False)
                ceiling = int(anti_farm.get("positive_change_ceiling", {}).get(dimension, 0))
                already_positive = self._window_positive_total(window, farm_since, dimension)
                applied_value = result.applied
                result_notes = result.notes
                if applied_value > 0 and ceiling > 0:
                    capped = max(0, min(applied_value, ceiling - already_positive))
                    if capped != applied_value:
                        result_notes = tuple((*result_notes, "rolling_window_cap"))
                    applied_value = capped
                applied[dimension] = applied_value
                notes[dimension] = {"requested": requested, "repeat_factor": result.repeat_factor, "notes": result_notes, "window_positive": already_positive}
            # MIS-134 C05: a paused account never writes the accepted deltas,
            # so binding/upgrade qualification uses the values that will
            # actually exist after this turn — unapplied scores can never
            # carry a proposal across a threshold.
            projected = {key: max(0, min(1000, values.get(key, 0) + (0 if paused else applied.get(key, 0)))) for key in DIMENSIONS}
            binding, binding_reason = binding_gate(projected, final_state)
            binding_status = "no_binding"
            if binding is None and binding_reason != "no_proposal":
                notes["binding"] = {"notes": (f"binding_rejected:{binding_reason}",)}
            if safety_proposal:
                if timed_safety:
                    notes["interaction_safety"] = {"notes": (f"timed_safety_applied:{timed_safety['level']}",)}
                else:
                    notes["interaction_safety"] = {"notes": (f"safety_suggestion_not_applied:{safety_mode}",)}
            if not any(applied.values()) and not binding and not timed_safety:
                return (values, "noop", {"reason": binding_reason, "notes": notes})
            # MIS-125 R2: the authoritative policy row is read inside this
            # transaction; a turn pinned to an older epoch keeps its legal
            # scoring and safety handling but may not bind. Exclusivity,
            # same-user constraints, legacy conflicts and the optional
            # rebind cooldown all follow the shared admission ladder.
            active_policy = self._active_policy_row(conn)
            binding_status = "no_binding"
            # MIS-126 R3: a natural, mutual bind/spouse proposal for an
            # identity that already holds exactly one active romantic_partner
            # is recognised by the backend as an upgrade candidate. No new
            # protocol action, no forced ladder, no manual confirmation.
            upgrade_of = None
            if binding is not None and binding["type_key"] == "spouse":
                own_partner = conn.execute("SELECT binding_id FROM relationship_bindings WHERE identity=? AND scope_kind=? AND scope_id=? AND type_key='romantic_partner' AND status='active' LIMIT 1", (identity, scope_kind, scope_id)).fetchone()
                upgrade_of = own_partner["binding_id"] if own_partner else None
            if binding:
                binding, admission = self._admit_binding(conn, binding, identity, scope_kind, scope_id, active_policy, policy.get("expected_epoch"), allow_romance_replace=upgrade_of is not None, now=now)
                if binding is None:
                    binding_status = admission
                    notes = {**notes, "binding": {"notes": [admission]}}
                elif upgrade_of:
                    # MIS-126 R3: atomic replacement — the partner row ends
                    # with the explicit upgraded marker (never a breakup,
                    # never a cooldown seed) and links forward to the new
                    # binding; the target type's real cooldown was already
                    # enforced inside the shared admission ladder.
                    conn.execute("UPDATE relationship_bindings SET status='ended',ended_at=?,updated_at=?,end_reason='upgraded',state_json=json_set(state_json,'$.upgraded_to',?) WHERE binding_id=?", (now, now, binding["binding_id"], upgrade_of))
                    binding_status = "binding_upgraded"
                    notes = {**notes, "binding": {"notes": ["binding_upgraded:romantic_partner->spouse"], "upgraded_from": upgrade_of}}
                else:
                    binding_status = "binding_created"
                    notes = {**notes, "binding": {"notes": ["binding_created:" + binding["type_key"]]}}
            if not paused:
                for key, delta in applied.items():
                    if key in DIMENSIONS:
                        values[key] = clamp(values.get(key, 0) + int(delta))
            conn.execute("INSERT INTO accounts(identity,scope_kind,scope_id,values_json,paused,revision,updated_at,state_json,last_interaction) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(identity,scope_kind,scope_id) DO UPDATE SET values_json=excluded.values_json,revision=excluded.revision,updated_at=excluded.updated_at,state_json=excluded.state_json,last_interaction=excluded.last_interaction", (identity, scope_kind, scope_id, json.dumps(values), int(paused), revision + 1, now, json.dumps(base_state), now))
            # MIS-134 C05: the event audit records the ACTUAL deltas — a
            # paused account wrote nothing, so the accepted-but-unapplied
            # deltas move into the notes with an explicit marker.
            event_actor = "llm"
            event_applied = applied
            if paused and any(applied.values()):
                notes = {**notes, "paused": {"accepted": dict(applied)}}
                event_applied = {}
            # MIS-134 C07: a round with no score, no safety action and a
            # rejected binding is a pure rejection receipt — kept for replay
            # idempotency and explanation, but never counted as a successful
            # settlement (blacklist counters and policy windows filter it).
            if not any((event_applied or {}).values()) and binding is None and timed_safety is None:
                event_actor = "llm_receipt"
            conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (event_id, identity, scope_kind, scope_id, source_kind, evidence[:300], reason[:500], json.dumps(requested_all), json.dumps(event_applied), json.dumps(notes), event_actor, now))
            if timed_safety is not None:
                previous = conn.execute("SELECT generation FROM timed_safety WHERE identity=? AND scope_kind=? AND scope_id=?", (identity, scope_kind, scope_id)).fetchone()
                generation = int(previous["generation"]) + 1 if previous else 1
                conn.execute("INSERT INTO timed_safety(identity,scope_kind,scope_id,level,expires_at,generation,source,created_at,updated_at) VALUES(?,?,?,?,?,?, 'llm_auto',?,?) ON CONFLICT(identity,scope_kind,scope_id) DO UPDATE SET level=excluded.level,expires_at=excluded.expires_at,generation=excluded.generation,updated_at=excluded.updated_at", (identity, scope_kind, scope_id, timed_safety["level"], now + timed_safety["duration_minutes"] * 60, generation, now, now))
            if binding:
                conn.execute("INSERT INTO relationship_bindings(binding_id,identity,scope_kind,scope_id,type_key,status,unique_scope,origin_event_id,state_json,created_at,updated_at,ended_at) VALUES(?,?,?,?,?,'active',?,?,?,?,?,NULL)", (binding["binding_id"], identity, scope_kind, scope_id, binding["type_key"], binding.get("unique_scope", ""), event_id, json.dumps({"origin": binding["origin"], "summary": binding.get("summary", "")}), now, now))
            # MIS-134 R03: the caller sees the ACTUAL deltas — unapplied
            # accepted values live only in notes.paused.
            info = {"status": binding_status, "binding_reason": binding_reason, "applied": event_applied, "requested": requested_all, "notes": notes, "timed_safety": timed_safety}
            return (values, "committed", info)

    def is_settlement_blacklisted(self,identity: str,scope_kind: str,scope_id: str) -> bool:
        with self.lock, self._connection() as conn:
            return bool(conn.execute("SELECT 1 FROM settlement_blacklist WHERE identity=? AND scope_kind=? AND scope_id=?",(identity,scope_kind,scope_id)).fetchone())

    def blacklist_settlement(self,identity: str,scope_kind: str,scope_id: str,reason_code: str) -> None:
        with self.lock, self._connection() as conn:
            conn.execute("INSERT INTO settlement_blacklist(identity,scope_kind,scope_id,reason_code,created_at) VALUES(?,?,?,?,?) ON CONFLICT(identity,scope_kind,scope_id) DO NOTHING",(identity,scope_kind,scope_id,reason_code[:64],time.time()))

    def clear_settlement_blacklist(self,identity: str,scope_kind: str,scope_id: str) -> bool:
        with self.lock, self._connection() as conn:
            return conn.execute("DELETE FROM settlement_blacklist WHERE identity=? AND scope_kind=? AND scope_id=?",(identity,scope_kind,scope_id)).rowcount>0

    def settlement_blacklist_entries(self,scope_kind: str | None = None) -> list[dict[str,Any]]:
        with self.lock, self._connection() as conn:
            rows=conn.execute("SELECT identity,scope_kind,scope_id,reason_code,created_at FROM settlement_blacklist"+(" WHERE scope_kind=?" if scope_kind in {'global','session'} else "")+" ORDER BY created_at DESC",((scope_kind,) if scope_kind in {'global','session'} else ())).fetchall()
            return [dict(row) for row in rows]

    def settlement_event_count(self,identity: str,scope_kind: str,scope_id: str) -> int:
        with self.lock, self._connection() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM events WHERE identity=? AND scope_kind=? AND scope_id=? AND actor='llm'",(identity,scope_kind,scope_id)).fetchone()[0])

    def active_timed_safety(self, identity: str, scope_kind: str, scope_id: str, now: float | None = None) -> dict[str, Any] | None:
        """Read an override and remove only the exact expired automatic row."""
        now=time.time() if now is None else float(now)
        with self.lock, self._connection() as conn:
            row=conn.execute("SELECT level,expires_at,generation,source FROM timed_safety WHERE identity=? AND scope_kind=? AND scope_id=?",(identity,scope_kind,scope_id)).fetchone()
            if not row: return None
            item=dict(row)
            if float(item["expires_at"]) > now: return item
            conn.execute("DELETE FROM timed_safety WHERE identity=? AND scope_kind=? AND scope_id=? AND generation=? AND source='llm_auto' AND expires_at<=?",(identity,scope_kind,scope_id,item["generation"],now))
            return None

    def effective_interaction_safety(self, identity: str, scope_kind: str, scope_id: str) -> str:
        account=self.existing_account(identity,scope_kind,scope_id)
        base=(account or {"state":self._default_state()})["state"].get("interaction_safety","normal")
        timed=self.active_timed_safety(identity,scope_kind,scope_id)
        rank={"normal":0,"slow_down":1,"pause_intimacy":2}
        return timed["level"] if timed and rank[timed["level"]] > rank[base] else base

    def set_interaction_safety_admin(self, identity: str, scope_kind: str, scope_id: str, value: str) -> dict[str, Any]:
        if value not in {"normal","slow_down","pause_intimacy"}: raise ValueError("invalid interaction safety")
        with self.lock, self._connection() as conn:
            row=conn.execute("SELECT * FROM accounts WHERE identity=? AND scope_kind=? AND scope_id=?",(identity,scope_kind,scope_id)).fetchone()
            account=self._row_account(row) if row else {"values":self._default_values(),"state":self._default_state(),"paused":False,"revision":0}
            if row is None:
                conn.execute("INSERT INTO accounts(identity,scope_kind,scope_id,values_json,paused,revision,updated_at,state_json) VALUES(?,?,?,?,?,?,?,?)",(identity,scope_kind,scope_id,json.dumps(account["values"]),0,0,time.time(),json.dumps(account["state"])))
            state={**account["state"],"interaction_safety":value}; now=time.time()
            conn.execute("DELETE FROM timed_safety WHERE identity=? AND scope_kind=? AND scope_id=?",(identity,scope_kind,scope_id))
            conn.execute("UPDATE accounts SET state_json=?,revision=revision+1,updated_at=? WHERE identity=? AND scope_kind=? AND scope_id=?",(json.dumps(state),now,identity,scope_kind,scope_id))
            event_id=hashlib.sha256(f"admin-safety:{identity}:{time.time_ns()}".encode()).hexdigest()[:32]
            conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(event_id,identity,scope_kind,scope_id,"admin","","",json.dumps({}),json.dumps({}),json.dumps({"interaction_safety":{"notes":["admin_set:"+value]}}),"administrator",now))
            return {**account,"state":state,"revision":account["revision"]+1}

    def record_admin_state_event(self, identity: str, scope_kind: str, scope_id: str, key: str, value: str) -> None:
        """Record a redacted administrator state change; no message/evidence is stored."""
        if key != "interaction_safety" or value not in {"normal","slow_down","pause_intimacy"}: raise ValueError("invalid admin state event")
        if scope_kind not in {"global","session"} or (scope_kind=="session" and not scope_id): raise ValueError("invalid scope")
        event_id=hashlib.sha256(f"admin-state:{identity}:{scope_kind}:{scope_id}:{key}:{value}:{time.time_ns()}".encode()).hexdigest()[:32]
        with self.lock, self._connection() as conn:
            conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(event_id,identity,scope_kind,scope_id,"admin","","",json.dumps({}),json.dumps({}),json.dumps({key:{"notes":["admin_set:"+value]}}),"administrator",time.time()))

    def recent(self, identity: str, scope_kind: str, scope_id: str, limit: int = 5) -> list[dict[str, Any]]:
        with self.lock, self._connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM events WHERE identity=? AND scope_kind=? AND scope_id=? ORDER BY created_at DESC LIMIT ?", (identity, scope_kind, scope_id, max(1, min(limit, 50))))]

    def list_events(self, limit: int = 200, scope_kind: str | None = None) -> list[dict[str, Any]]:
        with self.lock, self._connection() as conn:
            query = "SELECT event_id,identity,scope_kind,scope_id,source_kind,reason,requested_json,applied_json,notes_json,actor,created_at FROM events"
            args: tuple[Any, ...] = ()
            if scope_kind in {"global", "session"}:
                query += " WHERE scope_kind=?"; args = (scope_kind,)
            query += " ORDER BY created_at DESC LIMIT ?"
            return [dict(row) for row in conn.execute(query, (*args, max(1, min(limit, 500))))]

    @staticmethod
    def audit_cards_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """MIS-98: card projection for already-paginated event rows."""
        cards=[]
        for row in rows:
            try: requested, applied, notes = json.loads(row["requested_json"]), json.loads(row["applied_json"]), json.loads(row["notes_json"])
            except json.JSONDecodeError: requested, applied, notes = {}, {}, {}
            cards.append({"event_id":row["event_id"],"scope_kind":row["scope_kind"],"source_kind":row["source_kind"],"actor":row["actor"],"created_at":row["created_at"],"requested":{k:int(v) for k,v in requested.items() if v},"applied":{k:int(v) for k,v in applied.items() if v},"policy":{k:v.get("notes",[]) for k,v in notes.items() if isinstance(v,dict) and v.get("notes")}})
        return cards

    def audit_cards(self, limit: int = 200, scope_kind: str | None = None, scope_allowed=None) -> list[dict[str, Any]]:
        cards=[]
        for row in self.list_events(limit, scope_kind):
            if scope_allowed is not None and not scope_allowed(row["scope_kind"], row["scope_id"]):
                continue
            cards.extend(self.audit_cards_from_rows([row]))
        return cards

    def record_protocol_health(self, outcome: str, source: str, bare_recovery: bool, effect_count: int, retention_days: int) -> None:
        if outcome not in {"missing_protocol","invalid_protocol","model_no_effects","invalid_effects","zero_after_policy","applied","binding_rejected","duplicate_event","skipped_group_not_directed","skipped_no_stable_message_id"} or source not in {"completion_text","result_chain","none"}: return
        with self.lock, self._connection() as conn:
            conn.execute("INSERT INTO protocol_health(outcome,source,bare_recovery,effect_count,created_at) VALUES(?,?,?,?,?)", (outcome,source,int(bare_recovery),max(0,min(int(effect_count),100)),time.time()))
            conn.execute("DELETE FROM protocol_health WHERE created_at<?", (time.time()-max(1,retention_days)*86400,))

    def protocol_health_summary(self, since_days: int = 30) -> dict[str, Any]:
        window=max(1,min(since_days,3650)); since=time.time()-window*86400
        with self.lock, self._connection() as conn:
            rows=conn.execute("SELECT outcome,source,bare_recovery,COUNT(*) count FROM protocol_health WHERE created_at>=? GROUP BY outcome,source,bare_recovery",(since,)).fetchall()
        return {"window_days":window,"total":sum(int(r["count"]) for r in rows),"buckets":[dict(r) for r in rows]}

    def list_bindings(self, limit: int = 200, scope_kind: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        """B0 management query. No write method exists until B2 validation ships."""
        # Fully literal branch queries: every clause is a compile-time constant.
        if scope_kind in {"global", "session"} and status in {"active", "ended"}:
            query="SELECT binding_id,identity,scope_kind,scope_id,type_key,status,unique_scope,origin_event_id,state_json,created_at,updated_at,ended_at,end_reason FROM relationship_bindings WHERE scope_kind=? AND status=? ORDER BY updated_at DESC LIMIT ?"
            args: list[Any]=[scope_kind,status]
        elif scope_kind in {"global", "session"}:
            query="SELECT binding_id,identity,scope_kind,scope_id,type_key,status,unique_scope,origin_event_id,state_json,created_at,updated_at,ended_at,end_reason FROM relationship_bindings WHERE scope_kind=? ORDER BY updated_at DESC LIMIT ?"
            args=[scope_kind]
        elif status in {"active", "ended"}:
            query="SELECT binding_id,identity,scope_kind,scope_id,type_key,status,unique_scope,origin_event_id,state_json,created_at,updated_at,ended_at,end_reason FROM relationship_bindings WHERE status=? ORDER BY updated_at DESC LIMIT ?"
            args=[status]
        else:
            query="SELECT binding_id,identity,scope_kind,scope_id,type_key,status,unique_scope,origin_event_id,state_json,created_at,updated_at,ended_at,end_reason FROM relationship_bindings ORDER BY updated_at DESC LIMIT ?"
            args=[]
        args.append(max(1,min(limit,500)))
        with self.lock, self._connection() as conn:
            return [dict(row) for row in conn.execute(query,tuple(args)).fetchall()]

    def active_bindings_for(self, identity: str, scope_kind: str, scope_id: str) -> list[dict[str, Any]]:
        with self.lock, self._connection() as conn:
            rows=conn.execute("SELECT binding_id,type_key,unique_scope,created_at FROM relationship_bindings WHERE identity=? AND scope_kind=? AND scope_id=? AND status='active' ORDER BY created_at ASC",(identity,scope_kind,scope_id)).fetchall()
            return [dict(row) for row in rows]

    def exclusive_occupied(self, scope_kind: str, scope_id: str, unique_scope: str, except_identity: str) -> bool:
        if not unique_scope: return False
        with self.lock, self._connection() as conn:
            return bool(conn.execute("SELECT 1 FROM relationship_bindings WHERE scope_kind=? AND scope_id=? AND unique_scope=? AND status='active' AND identity<>? LIMIT 1",(scope_kind,scope_id,unique_scope,except_identity)).fetchone())

    def binding_scope(self, binding_id: str) -> tuple[str, str] | None:
        if not binding_id or len(binding_id)>128: return None
        with self.lock, self._connection() as conn:
            row=conn.execute("SELECT scope_kind,scope_id FROM relationship_bindings WHERE binding_id=? AND status='active'",(binding_id,)).fetchone()
            return (row["scope_kind"],row["scope_id"]) if row else None

    def end_binding(self, binding_id: str, actor: str, reason_code: str = "administrator") -> bool:
        """End only an existing active binding; retain its full structural history.

        MIS-125 R2: the same transaction re-counts the same-user conflicts for
        the affected identity/scope, so resolving a historical conflict by
        ending one specific binding re-opens the ledger writes for it (the
        uniqueness indexes return once the whole ledger is clean)."""
        if not binding_id or len(binding_id)>128: return False
        with self.lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row=conn.execute("SELECT binding_id,identity,scope_kind,scope_id FROM relationship_bindings WHERE binding_id=? AND status='active'",(binding_id,)).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                return False
            now=time.time()
            conn.execute("UPDATE relationship_bindings SET status='ended',ended_at=?,updated_at=?,end_reason=?,state_json=json_set(state_json,'$.end_actor',?,'$.end_reason',?) WHERE binding_id=?",(now,now,reason_code[:64],actor[:64],reason_code[:64],binding_id))
            # MIS-134 C02: one full reconciliation in the same transaction —
            # the recount covers same-user AND scope-level conflicts, the
            # diagnosis table is refreshed, and once the ledger is clean every
            # applicable constraint index comes back (writes resume).
            self._reconcile_binding_constraints(conn)
            conn.commit()
            return True

    def confirmed_admin_candidates(self, admin_ids: set[str]) -> list[dict[str, str]]:
        """Find only explicitly committed accounts whose platform-qualified identity
        ends in a configured administrator id. Used once for confirmed legacy migration.
        """
        if not admin_ids: return []
        with self.lock, self._connection() as conn:
            rows=conn.execute("SELECT identity,scope_kind,scope_id,state_json FROM accounts").fetchall()
        candidates=[]
        for row in rows:
            try: state=json.loads(row["state_json"] or "{}")
            except json.JSONDecodeError: continue
            sender=row["identity"].rsplit(":",1)[-1]
            if sender in admin_ids and state.get("romance_state") == "committed" and state.get("romance_policy") == "shown":
                candidates.append({"identity":row["identity"],"scope_kind":row["scope_kind"],"scope_id":row["scope_id"]})
        return candidates

    def migrate_confirmed_binding(self, *, identity: str, scope_kind: str, scope_id: str, type_key: str = "spouse") -> str:
        """Explicit legacy migration. Caller must nominate the identity; no inference.

        MIS-125 R2: the migration follows the same binding rules as every
        other write path - the active policy, legacy conflicts and occupancy
        are checked inside the transaction, never bypassed."""
        import hashlib
        binding_id=hashlib.sha256(f"legacy_confirmed_migration:{identity}:{scope_kind}:{scope_id}:{type_key}".encode()).hexdigest()[:32]
        with self.lock, self._connection() as conn:
            # Serialize the occupancy check and insertion, including other store instances.
            conn.execute("BEGIN IMMEDIATE")
            existing=conn.execute("SELECT status FROM relationship_bindings WHERE binding_id=?",(binding_id,)).fetchone()
            if existing:
                conn.execute("ROLLBACK")
                return "already_migrated"
            active_policy=self._active_policy_row(conn)
            now=time.time()
            candidate={"binding_id":binding_id,"type_key":type_key,"unique_scope":"romance","origin":"legacy_confirmed_migration","summary":""}
            candidate,status=self._admit_binding(conn,candidate,identity,scope_kind,scope_id,active_policy,None,now=now)
            if candidate is None:
                conn.execute("ROLLBACK")
                return status
            conn.execute("INSERT INTO relationship_bindings(binding_id,identity,scope_kind,scope_id,type_key,status,unique_scope,origin_event_id,state_json,created_at,updated_at,ended_at) VALUES(?,?,?,?,?,'active','romance','legacy_confirmed_migration',?,?,?,NULL)",(binding_id,identity,scope_kind,scope_id,type_key,json.dumps({"origin":"legacy_confirmed_migration","display":"此生挚爱"}),now,now))
            return "migrated"

    def binding_overview(self) -> dict[str, int]:
        with self.lock, self._connection() as conn:
            rows=conn.execute("SELECT scope_kind,status,COUNT(*) count FROM relationship_bindings GROUP BY scope_kind,status").fetchall()
        result={"total":0,"active":0,"ended":0,"global":0,"session":0}
        for row in rows:
            n=int(row["count"]); result["total"]+=n; result[row["status"]]+=n; result[row["scope_kind"]]+=n
        return result

    def list_migrations(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock, self._connection() as conn:
            return [dict(r) for r in conn.execute("SELECT component,from_version,to_version,backup_name,created_at FROM migration_log ORDER BY id DESC LIMIT ?",(max(1,min(limit,500)),))]

    @staticmethod
    def effect_signature(requested: dict[str, int]) -> str:
        """Stable shape key so repeated identical verdicts still decay when evidence is omitted."""
        return json.dumps({key: int(value) for key, value in sorted(requested.items()) if value}, sort_keys=True)

    def repeat_count(self, identity: str, scope_kind: str, scope_id: str, dimension: str, evidence: str, since: float, signature: str | None = None) -> int:
        with self.lock, self._connection() as conn:
            return self._repeat_count(conn, identity, scope_kind, scope_id, dimension, evidence, since, signature)

    def _window_rows(self, conn, identity: str, scope_kind: str, scope_id: str, since: float) -> list[tuple[int, dict[str, Any], dict[str, Any], list[str]]]:
        """MIS-97: one window fetch shared by every dimension in a turn.

        Returns (created_at, applied, requested, evidence_parts) tuples. Each
        llm event in the window is JSON-parsed exactly once here, instead of
        once per dimension per query (up to 12 full window scans before). The
        repeat window and the anti-farm window have different horizons, so the
        timestamp is carried and each use re-filters by time."""
        rows = conn.execute("SELECT applied_json,requested_json,evidence,created_at FROM events WHERE identity=? AND scope_kind=? AND scope_id=? AND created_at>=? AND actor='llm'", (identity, scope_kind, scope_id, since)).fetchall()
        parsed=[]
        for row in rows:
            try:
                applied=json.loads(row["applied_json"]); requested=json.loads(row["requested_json"])
            except (ValueError,RecursionError):
                continue
            parsed.append((float(row["created_at"]), applied, requested, row["evidence"].split(" | ")))
        return parsed

    def _window_repeat_count(self, window, repeat_since: float, dimension: str, evidence: str, signature: str | None) -> int:
        count=0
        for created_at,applied,requested,evidences in window:
            if created_at < repeat_since: continue
            if dimension not in applied: continue
            if evidence and evidence in evidences: count+=1
            elif not evidence and signature and signature == self.effect_signature(requested): count+=1
        return count

    def _window_positive_total(self, window, farm_since: float, dimension: str) -> int:
        return sum(max(0,int(applied.get(dimension,0))) for created_at,applied,_,_ in window if created_at >= farm_since)

    def _repeat_count(self, conn, identity: str, scope_kind: str, scope_id: str, dimension: str, evidence: str, since: float, signature: str | None = None) -> int:
        rows = conn.execute("SELECT applied_json,requested_json,evidence FROM events WHERE identity=? AND scope_kind=? AND scope_id=? AND created_at>=? AND actor='llm'", (identity, scope_kind, scope_id, since))
        count = 0
        for row in rows:
            if dimension not in json.loads(row["applied_json"]):
                continue
            if evidence and evidence in row["evidence"].split(" | "):
                count += 1
            elif not evidence and signature and signature == self.effect_signature(json.loads(row["requested_json"])):
                count += 1
        return count

    def positive_window_total(self, identity: str, scope_kind: str, scope_id: str, dimension: str, since: float) -> int:
        with self.lock, self._connection() as conn:
            return self._positive_window_total(conn, identity, scope_kind, scope_id, dimension, since)

    def _positive_window_total(self, conn, identity: str, scope_kind: str, scope_id: str, dimension: str, since: float) -> int:
        rows = conn.execute("SELECT applied_json FROM events WHERE identity=? AND scope_kind=? AND scope_id=? AND created_at>=? AND actor='llm'", (identity, scope_kind, scope_id, since))
        return sum(max(0, int(json.loads(row["applied_json"]).get(dimension, 0))) for row in rows)

    def update_account_admin(self, *, identity: str, scope_kind: str, scope_id: str, expected_revision: int, values: dict[str, int], state_changes: dict[str, str], actor: str = "page_administrator", cancel_timed: bool = False) -> dict[str, Any] | None:
        """Atomic optimistic-concurrency Pages update for an existing account.

        MIS-99: the automatic timed override is cancelled only when the
        administrator explicitly edits interaction_safety (a literal safety
        change) or passes cancel_timed=True; a dimensions-only edit keeps the
        running timer untouched."""
        if scope_kind not in {"global","session"} or (scope_kind=="global" and scope_id) or (scope_kind=="session" and not scope_id): raise ValueError("invalid scope")
        if set(values) != set(DIMENSIONS): raise ValueError("all dimensions required")
        if any(not isinstance(value,int) or value<0 or value>1000 for value in values.values()): raise ValueError("invalid values")
        allowed={k:v for k,v in state_changes.items() if k in {"romance_policy","romance_state","interaction_safety"}}
        with self.lock, self._connection() as conn:
            row=conn.execute("SELECT * FROM accounts WHERE identity=? AND scope_kind=? AND scope_id=?",(identity,scope_kind,scope_id)).fetchone()
            if not row: return None
            account=self._row_account(row)
            if account["revision"] != expected_revision: raise RuntimeError("stale_revision")
            state={**account["state"],**allowed}; now=time.time()
            # An explicit Pages safety edit is administrator authority and must
            # cancel the automatic override in the same transaction.
            if "interaction_safety" in allowed or cancel_timed:
                conn.execute("DELETE FROM timed_safety WHERE identity=? AND scope_kind=? AND scope_id=?",(identity,scope_kind,scope_id))
            conn.execute("UPDATE accounts SET values_json=?,state_json=?,revision=?,updated_at=? WHERE identity=? AND scope_kind=? AND scope_id=?",(json.dumps(values),json.dumps(state),expected_revision+1,now,identity,scope_kind,scope_id))
            requested={k:int(values[k])-int(account["values"].get(k,0)) for k in DIMENSIONS if int(values[k])!=int(account["values"].get(k,0))}
            event_id=f"{actor}:{hashlib.sha256((identity+scope_kind+scope_id).encode()).hexdigest()[:16]}:{time.time_ns()}"
            conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(event_id,identity,scope_kind,scope_id,"admin","","atomic page adjustment",json.dumps(requested),json.dumps(requested),json.dumps({"atomic":True, **{key:{"notes":["admin_set:"+value]} for key,value in allowed.items()}}),actor,now))
            return {"values":dict(values),"state":state,"revision":expected_revision+1}

    def decay_accounts(self, *, floors: dict[str,int], step: int, inactive_before: float, scope_allowed) -> int:
        """Floor-only decay. Administrative timestamps never affect this eligibility."""
        with self.lock, self._connection() as conn:
            return self._decay_accounts_conn(conn, floors=floors, step=step,
                                             inactive_before=inactive_before, scope_allowed=scope_allowed)

    def get_scheduler_state(self, key: str) -> float | None:
        with self.lock, self._connection() as conn:
            row = conn.execute("SELECT value FROM scheduler_state WHERE key=?", (key,)).fetchone()
            return float(row["value"]) if row else None

    def live_schema_version(self) -> int:
        """Actual SQLite user_version of the live database. Diagnostics must
        report what the database is, not what the code constant says (MIS-117:
        the migrations endpoint hardcoded a stale value and misled upgrade
        verification)."""
        with self.lock, self._connection() as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    def decay_if_due(self, *, floors: dict[str,int], step: int, inactive_before: float, scope_allowed, interval_seconds: int, now: float | None = None) -> bool:
        """MIS-96: apply decay only when the persisted period has elapsed.

        The due check, the decay and the period marker update share one
        transaction, so restarts or duplicate starts can never apply the same
        period twice. Returns True when a decay actually ran."""
        now = time.time() if now is None else float(now)
        with self.lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value FROM scheduler_state WHERE key='decay_last_run'").fetchone()
            if row is not None and now - float(row["value"]) < interval_seconds:
                conn.execute("ROLLBACK")
                return False
            self._decay_accounts_conn(conn, floors=floors, step=step,
                                      inactive_before=inactive_before, scope_allowed=scope_allowed)
            conn.execute("INSERT INTO scheduler_state(key,value) VALUES('decay_last_run',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now,))
            return True

    def _decay_accounts_conn(self, conn, *, floors: dict[str,int], step: int, inactive_before: float, scope_allowed) -> int:
        if set(floors)!=set(DIMENSIONS) or step<1: raise ValueError("invalid decay configuration")
        changed=0
        rows=conn.execute("SELECT * FROM accounts WHERE last_interaction>0 AND last_interaction<=?",(inactive_before,)).fetchall()
        for row in rows:
            if not scope_allowed(row["scope_kind"],row["scope_id"]): continue
            values={**DEFAULT_VALUES,**json.loads(row["values_json"])}
            next_values={key:max(int(floors[key]),int(value)-step) if int(value)>int(floors[key]) else int(value) for key,value in values.items()}
            if next_values==values: continue
            now=time.time()
            conn.execute("UPDATE accounts SET values_json=?,revision=revision+1,updated_at=? WHERE identity=? AND scope_kind=? AND scope_id=?",(json.dumps(next_values),now,row["identity"],row["scope_kind"],row["scope_id"]))
            event_id=hashlib.sha256(f"decay:{row['identity']}:{row['scope_kind']}:{row['scope_id']}:{time.time_ns()}".encode()).hexdigest()[:32]
            applied={key:next_values[key]-values[key] for key in DIMENSIONS if next_values[key]!=values[key]}
            conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(event_id,row["identity"],row["scope_kind"],row["scope_id"],"decay","","inactivity_decay",json.dumps(applied),json.dumps(applied),json.dumps({"decay":True}),"scheduler",now))
            changed+=1
        return changed

    def set_dimension(self, identity: str, scope_kind: str, scope_id: str, dimension: str, value: int, actor: str = "administrator") -> dict[str, int]:
        if dimension not in DIMENSIONS:
            raise ValueError("unknown relation dimension")
        # MIS-92: single-transaction read-modify-write (same shape as
        # adjust_dimension) so a concurrent settlement or edit cannot be
        # overwritten by a stale precomputed delta.
        with self.lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM accounts WHERE identity=? AND scope_kind=? AND scope_id=?", (identity, scope_kind, scope_id)).fetchone()
            account = self._row_account(row) if row else {"values": self._default_values(), "state": self._default_state(), "paused": False, "revision": 0}
            values = dict(account["values"])
            old = int(values.get(dimension, 0))
            delta = int(value) - old
            new_revision = int(account["revision"]) + 1
            values[dimension] = clamp(old + delta)
            now = time.time()
            if row is None:
                conn.execute("INSERT INTO accounts(identity,scope_kind,scope_id,values_json,paused,revision,updated_at,state_json) VALUES(?,?,?,?,?,?,?,?)", (identity, scope_kind, scope_id, json.dumps(values), 0, new_revision, now, json.dumps(account["state"])))
            else:
                conn.execute("UPDATE accounts SET values_json=?,revision=?,updated_at=? WHERE identity=? AND scope_kind=? AND scope_id=?", (json.dumps(values), new_revision, now, identity, scope_kind, scope_id))
            event_id = f"{actor}:{identity}:{dimension}:{time.time_ns()}"
            conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (event_id, identity, scope_kind, scope_id, "admin", "", "manual adjustment", json.dumps({dimension: delta}), json.dumps({dimension: delta}), json.dumps({"atomic": True, dimension: {"notes": ["admin_set:" + str(value)]}}), actor, now))
            return values

    def adjust_dimension(self, identity: str, scope_kind: str, scope_id: str, dimension: str, delta: int, actor: str = "administrator") -> dict[str, int]:
        """Read, clamp, update and audit under one SQLite writer transaction."""
        if dimension not in DIMENSIONS or type(delta) is not int: raise ValueError("invalid adjustment")
        with self.lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row=conn.execute("SELECT * FROM accounts WHERE identity=? AND scope_kind=? AND scope_id=?",(identity,scope_kind,scope_id)).fetchone()
            if row is None: raise ValueError("account not found")
            values=self._row_account(row)["values"]
            old=values[dimension]; values[dimension]=max(0,min(1000,old+delta)); now=time.time()
            conn.execute("UPDATE accounts SET values_json=?,revision=revision+1,updated_at=? WHERE identity=? AND scope_kind=? AND scope_id=?",(json.dumps(values),now,identity,scope_kind,scope_id))
            event_id=hashlib.sha256(f"admin-delta:{identity}:{scope_kind}:{scope_id}:{row['revision']+1}:{time.time_ns()}".encode()).hexdigest()[:32]
            conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(event_id,identity,scope_kind,scope_id,"admin","","manual adjustment",json.dumps({dimension:delta}),json.dumps({dimension:values[dimension]-old}),json.dumps({}),actor,now))
            return values

    def set_paused(self, identity: str, scope_kind: str, scope_id: str, paused: bool) -> None:
        account = self.account(identity, scope_kind, scope_id)
        with self.lock, self._connection() as conn:
            conn.execute("UPDATE accounts SET paused=?, revision=?, updated_at=? WHERE identity=? AND scope_kind=? AND scope_id=?", (int(paused), account["revision"] + 1, time.time(), identity, scope_kind, scope_id))

    def backup_now(self, kind: str = "manual") -> Path:
        return self._sqlite_backup(self._backup_path(kind))

    def list_backups(self) -> list[dict[str, Any]]:
        rows=[]
        for kind in sorted(BACKUP_KINDS):
            for path in (self.backup_dir / kind).glob("*.sqlite3"):
                rows.append({"name":path.name,"kind":kind,"size":path.stat().st_size,"mtime":path.stat().st_mtime})
        return sorted(rows,key=lambda r:(r["mtime"],r["name"]),reverse=True)

    def cleanup_auto_backups(self, retention_hours: int) -> int:
        """Rotate expired auto snapshots as whole groups: a full snapshot is a
        sqlite file plus its same-stem config copy and manifest, so deleting
        only the sqlite file would orphan the verification material. Only the
        auto kind rotates; manual/migration/pre_restore stay protected."""
        cutoff=time.time()-max(1,retention_hours)*3600; cleaned=0
        for path in (self.backup_dir / "auto").glob("*.sqlite3"):
            if path.stat().st_mtime < cutoff:
                path.unlink(); cleaned+=1
                for companion in (path.with_suffix(".config.json"), path.with_suffix(".manifest.json")):
                    companion.unlink(missing_ok=True)
        return cleaned

    def restore_backup(self, name: str, kind: str = "manual") -> dict[str, Any]:
        """MIS-95/MIS-117/MIS-134: prechecked restore via the SQLite backup API.

        The source must pass an integrity check and carry a supported schema
        version (1..SCHEMA_VERSION) or it is rejected before the live database
        is touched. An older-schema backup is first migrated on a PRIVATE
        staging copy (unique per-call file name — concurrent restores can no
        longer delete each other's staging). The final phase runs under a
        cross-instance restore lease: the lease is mutually exclusive with
        policy activation, so the policy captured inside the lease is stable;
        the staging copy is injected with that policy plus a brand-new epoch,
        verified, and only then copied into the live database through a
        read-only source handle (a missing source can never spawn an empty
        database). After the copy the live facts are verified against the
        return value; any failure — including a detected clobber — rolls the
        live database back to the pre_restore snapshot taken moments earlier
        and raises instead of faking success."""
        if kind not in BACKUP_KINDS: raise ValueError("unknown backup kind")
        source=(self.backup_dir/kind/Path(name).name).resolve(); parent=(self.backup_dir/kind).resolve()
        if source.parent != parent or not source.is_file(): raise ValueError("backup not found")
        probe=sqlite3.connect(source)
        try:
            try:
                integrity=probe.execute("PRAGMA integrity_check").fetchone()[0]
                version=int(probe.execute("PRAGMA user_version").fetchone()[0])
            except sqlite3.DatabaseError as exc:
                raise ValueError("backup file is not a readable SQLite database") from exc
        finally:
            probe.close()
        if integrity != "ok":
            raise ValueError(f"backup failed integrity check: {integrity}")
        if not 1 <= version <= SCHEMA_VERSION:
            raise ValueError(f"unsupported backup schema version {version}; this build supports 1..{SCHEMA_VERSION}")
        import uuid
        staging=source.with_name(source.stem + f".restoring-{uuid.uuid4().hex[:12]}.tmp")
        token=self._acquire_restore_lock(ttl_seconds=60)
        if token is None:
            raise ValueError("another restore is in progress; retry after it finishes")
        try:
            self._materialize_staging(source, staging)
            # MIS-134 R01: the mutual exclusion lives in a lock FILE outside
            # the database, so the final copy can never wipe it. Ownership is
            # re-verified (and the TTL heartbeated) before every phase that
            # touches live state: an expired or disowned restorer stops
            # before copying, before rolling back, and can never delete a
            # successor's lock.
            self._assert_restore_lock(token)
            self._migrate_staging(staging, from_version=version, backup_name=source.name)
            # While the lock is held, activation is refused, so this snapshot
            # of the live policy is authoritative for the whole final phase.
            live_policy=self.active_binding_policy()
            summary=self._inject_staging_policy(staging, live_policy)
            self._verify_staging(staging)
            self._assert_restore_lock(token)
            pre_restore_path=self.backup_now("pre_restore")
            try:
                self._assert_restore_lock(token)
                self._copy_into_live(staging)
                self._verify_restored_live(live_policy)
                # MIS-134 R01: ownership re-checked AFTER the copy too — a
                # restorer that lost the lock mid-copy (TTL expiry + takeover)
                # must not report success over a successor's head. The state
                # is left as copied and the error documents the boundary; the
                # successor's desired JSON survives for the next reload.
                self._assert_restore_lock(token)
            except Exception:
                # Defense in depth: if the live database was replaced but
                # does not match the verified staging, roll back to THIS
                # restore's own pre-restore snapshot — but only while still
                # the lock owner; a successor that legitimately took over
                # (activation included) is never rolled back.
                if self._restore_lock_owned(token):
                    self._copy_into_live(pre_restore_path)
                raise
            self.policy_activation_error=None
            return {"schema_version": SCHEMA_VERSION, "restored_from_version": version,
                    "integrity": "ok", "migrated": version < SCHEMA_VERSION,
                    "policy": {"exclusivity": live_policy["exclusivity"],
                               "rebind_cooldown": live_policy["rebind_cooldown"],
                               "epoch": int(live_policy["epoch"]) + 1,
                               "legacy_conflicts": summary["legacy_conflicts"]}}
        finally:
            self._release_restore_lock(token)
            staging.unlink(missing_ok=True)

    def _inject_staging_policy(self, staging: Path, live_policy: dict[str, Any]) -> dict[str, Any]:
        """MIS-134 C01/C03: adopt the CURRENT effective policy with a
        brand-new epoch on the staging copy and reconcile its constraints.
        The backup's own baked-in indexes and epoch are discarded — an old
        backup must never re-lock relationships that the active policy has
        released. All structural and policy writes stay on staging (MIS-121
        B02: the final live copy is followed by zero structural writes)."""
        staging_conn=sqlite3.connect(staging, timeout=10)
        staging_conn.row_factory=sqlite3.Row
        try:
            staging_conn.execute("BEGIN IMMEDIATE")
            staging_conn.execute("INSERT OR REPLACE INTO binding_policy(id,exclusivity,rebind_cooldown,epoch,updated_at) VALUES(1,?,?,?,?)",
                                 (live_policy["exclusivity"], live_policy["rebind_cooldown"], int(live_policy["epoch"]) + 1, time.time()))
            summary=self._reconcile_binding_constraints(staging_conn)
            if live_policy["exclusivity"] == "scope":
                clash_count=int(staging_conn.execute("SELECT COUNT(*) FROM (SELECT scope_kind,scope_id FROM relationship_bindings WHERE status='active' AND type_key IN ('romantic_partner','spouse') GROUP BY scope_kind,scope_id HAVING COUNT(DISTINCT identity)>1)").fetchone()[0])
                if clash_count:
                    raise ValueError(f"restored backup conflicts with the active exclusivity policy in {clash_count} scope(s); end the extra bindings from a backup-era instance or restore while exclusivity is off")
            staging_conn.commit()
        except Exception:
            staging_conn.rollback()
            raise
        finally:
            staging_conn.close()
        return summary

    def _restore_lock_path(self) -> Path:
        """MIS-134 R01: the restore mutual exclusion lives in a lock FILE
        next to the database — never inside it, so replacing the database
        cannot delete the exclusion, and a backup cannot carry it."""
        return self.data_dir / "relation_arc.restore.lock"

    _RESTORE_LOCK_GUARDS: dict = {}
    _RESTORE_LOCK_GUARDS_GUARD: threading.Lock = threading.Lock()

    def _restore_lock_guard(self) -> threading.Lock:
        path=str(self._restore_lock_path())
        with RelationStore._RESTORE_LOCK_GUARDS_GUARD:
            return RelationStore._RESTORE_LOCK_GUARDS.setdefault(path, threading.Lock())

    def _acquire_restore_lock(self, ttl_seconds: int) -> str | None:
        """Take the restore lock (in-process mutex + O_EXCL file). Returns
        the owner token, or None while another unexpired holder owns it."""
        token=uuid.uuid4().hex[:12]
        lock_path=self._restore_lock_path()
        guard=self._restore_lock_guard()
        with self.lock, guard:
            now=time.time()
            try:
                payload=json.loads(lock_path.read_text(encoding="utf-8"))
                if float(payload.get("until", 0)) > now:
                    return None
                lock_path.unlink()
            except FileNotFoundError:
                pass
            except (ValueError, OSError):
                lock_path.unlink(missing_ok=True)
            fd=os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, json.dumps({"owner": token, "until": now + max(1, ttl_seconds)}).encode("utf-8"))
            finally:
                os.close(fd)
        return token

    def _restore_lock_state(self) -> dict[str, Any] | None:
        try:
            return json.loads(self._restore_lock_path().read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return None

    def _restore_lock_owned(self, token: str) -> bool:
        state=self._restore_lock_state()
        # An expired lock is LOST even when no successor has taken over yet:
        # the stale holder must re-verify against the world, not revive it.
        return bool(state) and state.get("owner") == token and float(state.get("until", 0)) > time.time()

    def _assert_restore_lock(self, token: str) -> None:
        """Heartbeat + ownership check: a lost or expired lock aborts the
        restore before it touches live state again."""
        if not self._restore_lock_owned(token):
            raise RuntimeError("restore lock lost; aborting without touching the live database")
        lock_path=self._restore_lock_path()
        guard=self._restore_lock_guard()
        with self.lock, guard:
            state=self._restore_lock_state()
            if state and state.get("owner") == token:
                fd=os.open(lock_path, os.O_TRUNC | os.O_WRONLY)
                try:
                    os.write(fd, json.dumps({"owner": token, "until": time.time() + 60}).encode("utf-8"))
                finally:
                    os.close(fd)

    def _release_restore_lock(self, token: str) -> None:
        """Release only when still the owner — never a successor's lock."""
        lock_path=self._restore_lock_path()
        guard=self._restore_lock_guard()
        with self.lock, guard:
            state=self._restore_lock_state()
            if state and state.get("owner") == token:
                lock_path.unlink(missing_ok=True)

    def _restore_lock_held(self) -> bool:
        state=self._restore_lock_state()
        return bool(state) and float(state.get("until", 0)) > time.time()

    def _verify_restored_live(self, live_policy: dict[str, Any]) -> None:
        """MIS-134 C01/C03: the live database must match the verified staging
        facts after the copy; a mismatch means a concurrent writer clobbered
        the restore and is raised (the caller rolls back pre_restore)."""
        conn=sqlite3.connect(self.path, timeout=10)
        conn.row_factory=sqlite3.Row
        try:
            version=int(conn.execute("PRAGMA user_version").fetchone()[0])
            integrity=conn.execute("PRAGMA integrity_check").fetchone()[0]
            row=conn.execute("SELECT exclusivity,rebind_cooldown,epoch FROM binding_policy WHERE id=1").fetchone()
        finally:
            conn.close()
        if version != SCHEMA_VERSION or integrity != "ok" or row is None:
            raise RuntimeError("restored live database failed post-copy verification")
        if (row["exclusivity"], row["rebind_cooldown"], int(row["epoch"])) != (
                live_policy["exclusivity"], live_policy["rebind_cooldown"], int(live_policy["epoch"]) + 1):
            raise RuntimeError("restored live database was clobbered by a concurrent policy change")

    def _materialize_staging(self, source: Path, staging: Path) -> None:
        """Copy the verified backup file onto a staging path via the backup API."""
        with self.lock:
            source_conn=sqlite3.connect(source)
            staging_conn=sqlite3.connect(staging)
            try:
                source_conn.backup(staging_conn)
            finally:
                staging_conn.close(); source_conn.close()

    def _migrate_staging(self, staging: Path, *, from_version: int, backup_name: str) -> None:
        conn=sqlite3.connect(staging, timeout=10)
        conn.row_factory=sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._apply_schema_ddl(conn)
            self._stamp_schema_version(conn, from_version, backup_name)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _verify_staging(self, staging: Path) -> None:
        probe=sqlite3.connect(staging)
        try:
            integrity=probe.execute("PRAGMA integrity_check").fetchone()[0]
            version=int(probe.execute("PRAGMA user_version").fetchone()[0])
        finally:
            probe.close()
        if integrity != "ok":
            raise ValueError(f"migrated restore staging copy failed integrity check: {integrity}")
        if version != SCHEMA_VERSION:
            raise ValueError(f"migrated restore staging copy has unexpected schema version {version}")

    def _copy_into_live(self, source: Path) -> None:
        """Page-by-page copy from a backup file into the live database.

        MIS-134 C01: the source is opened read-only through a URI after an
        explicit existence check — a missing or deleted source can never
        silently spawn an empty database and get copied over the live data."""
        if not source.is_file():
            raise ValueError(f"restore source disappeared before the final copy: {source.name}")
        with self.lock:
            target=sqlite3.connect(self.path, timeout=10)
            source_conn=sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                source_conn.backup(target)
            finally:
                source_conn.close(); target.close()

    def set_state(self, identity: str, scope_kind: str, scope_id: str, **changes: str) -> dict[str, Any]:
        allowed={"romance_policy","romance_state","interaction_safety"}
        unknown=set(changes)-allowed
        if unknown: raise ValueError(f"unknown state fields: {sorted(unknown)}")
        enums={
            "romance_policy":{"hidden","observing","shown"},
            "romance_state":{"hidden","observing","shown","eligible","committed"},
            "interaction_safety":{"normal","slow_down","pause_intimacy"},
        }
        invalid={key:value for key,value in changes.items() if value not in enums[key]}
        if invalid: raise ValueError(f"invalid state values: {sorted(invalid)}")
        account = self.account(identity, scope_kind, scope_id)
        state = {**account["state"], **changes}
        with self.lock, self._connection() as conn:
            conn.execute("UPDATE accounts SET state_json=?,revision=?,updated_at=? WHERE identity=? AND scope_kind=? AND scope_id=?", (json.dumps(state), account["revision"]+1, time.time(), identity, scope_kind, scope_id))
        return {**account, "state": state}

    def migrate_v4_reset(self, special_identity: str, special_values: dict[str,int]) -> dict[str,int]:
        """Approved destructive migration: reset values/states, retain immutable audit events."""
        with self.lock, self._connection() as conn:
            rows=conn.execute("SELECT identity,scope_kind,scope_id FROM accounts").fetchall()
            for row in rows:
                special = row["identity"] == special_identity
                values = dict(special_values) if special else dict(DEFAULT_VALUES)
                state = {"romance_policy":"shown","romance_state":"committed","interaction_safety":"normal"} if special else self._legacy_default_state()
                conn.execute("UPDATE accounts SET values_json=?,state_json=?,paused=0,revision=revision+1,updated_at=? WHERE identity=? AND scope_kind=? AND scope_id=?", (json.dumps(values),json.dumps(state),time.time(),row["identity"],row["scope_kind"],row["scope_id"]))
            # create designated account if it was not present yet
            if not any(row["identity"]==special_identity for row in rows):
                conn.execute("INSERT INTO accounts(identity,scope_kind,scope_id,values_json,paused,revision,updated_at,state_json) VALUES(?,?,?,?,?,?,?,?)", (special_identity,"global","",json.dumps(special_values),0,1,time.time(),json.dumps({"romance_policy":"shown","romance_state":"committed","interaction_safety":"normal"})))
        return {"reset_accounts": len(rows), "special_created": int(not any(row["identity"]==special_identity for row in rows))}

    def close(self) -> None:
        pass
