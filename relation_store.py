from __future__ import annotations

import json
import hashlib
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .relation_engine import DIMENSIONS, DEFAULT_VALUES, apply_delta, clamp

SCHEMA_VERSION = 10
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_binding_scope_status ON relationship_bindings(identity,scope_kind,scope_id,status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_binding_unique_active ON relationship_bindings(scope_kind,scope_id,unique_scope,status)")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
            if "state_json" not in columns:
                conn.execute("ALTER TABLE accounts ADD COLUMN state_json TEXT NOT NULL DEFAULT '{}'")
            if "last_interaction" not in columns:
                # C3 deliberately distinguishes accepted interaction time from any edit/read timestamp.
                conn.execute("ALTER TABLE accounts ADD COLUMN last_interaction REAL NOT NULL DEFAULT 0")
            if old_version < SCHEMA_VERSION:
                conn.execute("INSERT INTO migration_log(component,from_version,to_version,backup_name,created_at) VALUES(?,?,?,?,?)", ("sqlite", old_version, SCHEMA_VERSION, migration_backup.name if migration_backup else "", time.time()))
                self.migration_events.append({"component":"sqlite", "from_version":old_version, "to_version":SCHEMA_VERSION, "backup":migration_backup.name if migration_backup else ""})
            # SQLite PRAGMA cannot bind parameters, so the version literal is
            # written directly and verified against SCHEMA_VERSION on read-back;
            # any drift fails loudly instead of silently mislabelling a database.
            conn.execute("PRAGMA user_version=10")
            written = conn.execute("PRAGMA user_version").fetchone()[0]
            if written != SCHEMA_VERSION:
                raise RuntimeError(f"schema version drift: user_version={written} != SCHEMA_VERSION={SCHEMA_VERSION}")
        # MIS-92: exclusivity gets a database-level guarantee. Audit first: the
        # partial unique index below is constant literal DDL (SQLite DDL cannot
        # bind parameters) and is only created when no conflicting active rows
        # exist; conflicts are counted (sanitised, never identities) and never
        # deleted; every later boot retries.
        self.unique_index_conflicts = 0
        with self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_binding_unique_active_uq ON relationship_bindings(scope_kind,scope_id,unique_scope,status) WHERE status='active' AND unique_scope<>''")
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                self.unique_index_conflicts = int(conn.execute("SELECT COUNT(*) FROM (SELECT 1 FROM relationship_bindings WHERE status='active' AND unique_scope<>'' GROUP BY scope_kind,scope_id,unique_scope HAVING COUNT(*)>1)").fetchone()[0])

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

    def list_accounts_page(self, *, page: int, page_size: int, scope_kind: str | None = None, scope_id: str | None = None) -> list[dict[str, Any]]:
        page=max(1,int(page)); page_size=max(1,min(int(page_size),100)); clauses=[];params=[]
        if scope_kind in {"global","session"}: clauses.append("scope_kind=?");params.append(scope_kind)
        if scope_id is not None: clauses.append("scope_id=?");params.append(scope_id)
        where=(" WHERE "+" AND ".join(clauses)) if clauses else ""
        params.extend((page_size,(page-1)*page_size))
        with self.lock, self._connection() as conn:
            rows=conn.execute("SELECT identity,scope_kind,scope_id,values_json,state_json,paused,revision,updated_at FROM accounts"+where+" ORDER BY updated_at DESC LIMIT ? OFFSET ?",params).fetchall()
            return [{**dict(row),**self._row_account(row)} for row in rows]

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

    def apply_turn_with_binding(self, *, event_id: str, identity: str, scope_kind: str, scope_id: str, source_kind: str, evidence: str, reason: str, requested: dict[str, int], applied: dict[str, int], notes: dict[str, Any], binding: dict[str, Any] | None, state_changes: dict[str, str] | None = None, timed_safety: dict[str, Any] | None = None) -> tuple[dict[str, int], str]:
        """Favour-style post-score validation write, upgraded to one SQLite transaction.

        `binding` must already be a backend-validated fixed type. This method
        never trusts model text and never creates bindings when it is None.
        """
        with self.lock, self._connection() as conn:
            row=conn.execute("SELECT * FROM accounts WHERE identity=? AND scope_kind=? AND scope_id=?",(identity,scope_kind,scope_id)).fetchone()
            if conn.execute("SELECT 1 FROM events WHERE event_id=?",(event_id,)).fetchone():
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
                # Recheck exclusivity inside this write transaction. Other users
                # are never returned to the caller, only an opaque rejection code.
                group=binding.get("unique_scope","")
                if group and conn.execute("SELECT 1 FROM relationship_bindings WHERE scope_kind=? AND scope_id=? AND unique_scope=? AND status='active' LIMIT 1",(scope_kind,scope_id,group)).fetchone():
                    binding=None; notes={**notes,"binding":{"notes":["binding_rejected:exclusive"]}}
                    binding_status="binding_rejected:exclusive"
                elif conn.execute("SELECT 1 FROM relationship_bindings WHERE identity=? AND scope_kind=? AND scope_id=? AND type_key=? AND status='active' LIMIT 1",(identity,scope_kind,scope_id,binding["type_key"])).fetchone():
                    binding=None; notes={**notes,"binding":{"notes":["binding_rejected:duplicate"]}}
                    binding_status="binding_rejected:duplicate"
                else:
                    binding_status="binding_created"
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

    def settle_turn(self, *, event_id: str, identity: str, scope_kind: str, scope_id: str, source_kind: str, evidence: str, reason: str, requested_all: dict[str, int], fact_signature: str, safety_proposal: dict[str, Any] | None, policy: dict[str, Any], romance_gate, binding_gate) -> tuple[dict[str, int], str, dict[str, Any]]:
        """MIS-92: one controlled transaction for a full turn settlement.

        Read state -> policy/window/eligibility recomputation -> writes all
        happen inside a single BEGIN IMMEDIATE transaction, so concurrent
        connections can never settle from stale state. Policy stays in the
        caller via the two pure callbacks (no I/O inside):
          romance_gate(values, state) -> bool
          binding_gate(projected_values, state) -> (binding | None, reason)

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
            for dimension in DIMENSIONS:
                requested = int(requested_all.get(dimension, 0))
                if not requested:
                    continue
                if dimension == "romance_interest" and not romance_allowed:
                    # Locked romance never moves in either direction; anti-coercion is a hard backend gate.
                    notes[dimension] = {"requested": requested, "repeat_factor": 1.0, "notes": ("romance_locked",), "window_positive": 0}
                    applied[dimension] = 0
                    continue
                repeat_count = self._repeat_count(conn, identity, scope_kind, scope_id, dimension, evidence, now - policy["repeat_window_minutes"] * 60, signature=fact_signature)
                result = apply_delta(values.get(dimension, 0), requested, repeat_count, policy["repeat_factors"], False)
                ceiling = int(anti_farm.get("positive_change_ceiling", {}).get(dimension, 0))
                since = now - int(anti_farm.get("rolling_window_hours", 24)) * 3600
                already_positive = self._positive_window_total(conn, identity, scope_kind, scope_id, dimension, since)
                applied_value = result.applied
                result_notes = result.notes
                if applied_value > 0 and ceiling > 0:
                    capped = max(0, min(applied_value, ceiling - already_positive))
                    if capped != applied_value:
                        result_notes = tuple((*result_notes, "rolling_window_cap"))
                    applied_value = capped
                applied[dimension] = applied_value
                notes[dimension] = {"requested": requested, "repeat_factor": result.repeat_factor, "notes": result_notes, "window_positive": already_positive}
            projected = {key: max(0, min(1000, values.get(key, 0) + applied.get(key, 0))) for key in DIMENSIONS}
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
            # Binding exclusivity/duplicate recheck inside this transaction.
            if binding:
                group = binding.get("unique_scope", "")
                if group and conn.execute("SELECT 1 FROM relationship_bindings WHERE scope_kind=? AND scope_id=? AND unique_scope=? AND status='active' LIMIT 1", (scope_kind, scope_id, group)).fetchone():
                    binding = None
                    notes = {**notes, "binding": {"notes": ["binding_rejected:exclusive"]}}
                    binding_status = "binding_rejected:exclusive"
                elif conn.execute("SELECT 1 FROM relationship_bindings WHERE identity=? AND scope_kind=? AND scope_id=? AND type_key=? AND status='active' LIMIT 1", (identity, scope_kind, scope_id, binding["type_key"])).fetchone():
                    binding = None
                    notes = {**notes, "binding": {"notes": ["binding_rejected:duplicate"]}}
                    binding_status = "binding_rejected:duplicate"
                else:
                    binding_status = "binding_created"
            if not paused:
                for key, delta in applied.items():
                    if key in DIMENSIONS:
                        values[key] = clamp(values.get(key, 0) + int(delta))
            conn.execute("INSERT INTO accounts(identity,scope_kind,scope_id,values_json,paused,revision,updated_at,state_json,last_interaction) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(identity,scope_kind,scope_id) DO UPDATE SET values_json=excluded.values_json,revision=excluded.revision,updated_at=excluded.updated_at,state_json=excluded.state_json,last_interaction=excluded.last_interaction", (identity, scope_kind, scope_id, json.dumps(values), int(paused), revision + 1, now, json.dumps(base_state), now))
            conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (event_id, identity, scope_kind, scope_id, source_kind, evidence[:300], reason[:500], json.dumps(requested_all), json.dumps(applied), json.dumps(notes), "llm", now))
            if timed_safety is not None:
                previous = conn.execute("SELECT generation FROM timed_safety WHERE identity=? AND scope_kind=? AND scope_id=?", (identity, scope_kind, scope_id)).fetchone()
                generation = int(previous["generation"]) + 1 if previous else 1
                conn.execute("INSERT INTO timed_safety(identity,scope_kind,scope_id,level,expires_at,generation,source,created_at,updated_at) VALUES(?,?,?,?,?,?, 'llm_auto',?,?) ON CONFLICT(identity,scope_kind,scope_id) DO UPDATE SET level=excluded.level,expires_at=excluded.expires_at,generation=excluded.generation,updated_at=excluded.updated_at", (identity, scope_kind, scope_id, timed_safety["level"], now + timed_safety["duration_minutes"] * 60, generation, now, now))
            if binding:
                conn.execute("INSERT INTO relationship_bindings(binding_id,identity,scope_kind,scope_id,type_key,status,unique_scope,origin_event_id,state_json,created_at,updated_at,ended_at) VALUES(?,?,?,?,?,'active',?,?,?,?,?,NULL)", (binding["binding_id"], identity, scope_kind, scope_id, binding["type_key"], binding.get("unique_scope", ""), event_id, json.dumps({"origin": binding["origin"], "summary": binding.get("summary", "")}), now, now))
            info = {"status": binding_status, "binding_reason": binding_reason, "applied": applied, "requested": requested_all, "notes": notes}
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

    def audit_cards(self, limit: int = 200, scope_kind: str | None = None) -> list[dict[str, Any]]:
        cards=[]
        for row in self.list_events(limit, scope_kind):
            try: requested, applied, notes = json.loads(row["requested_json"]), json.loads(row["applied_json"]), json.loads(row["notes_json"])
            except json.JSONDecodeError: requested, applied, notes = {}, {}, {}
            cards.append({"event_id":row["event_id"],"scope_kind":row["scope_kind"],"source_kind":row["source_kind"],"actor":row["actor"],"created_at":row["created_at"],"requested":{k:int(v) for k,v in requested.items() if v},"applied":{k:int(v) for k,v in applied.items() if v},"policy":{k:v.get("notes",[]) for k,v in notes.items() if isinstance(v,dict) and v.get("notes")}})
        return cards

    def record_protocol_health(self, outcome: str, source: str, bare_recovery: bool, effect_count: int, retention_days: int) -> None:
        if outcome not in {"missing_protocol","invalid_protocol","model_no_effects","invalid_effects","zero_after_policy","applied","duplicate_event","skipped_group_not_directed","skipped_no_stable_message_id"} or source not in {"completion_text","result_chain","none"}: return
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
        clauses=[]; args: list[Any]=[]
        if scope_kind in {"global", "session"}: clauses.append("scope_kind=?"); args.append(scope_kind)
        if status in {"active", "ended"}: clauses.append("status=?"); args.append(status)
        query="SELECT binding_id,identity,scope_kind,scope_id,type_key,status,unique_scope,origin_event_id,created_at,updated_at,ended_at FROM relationship_bindings"
        if clauses: query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC LIMIT ?"; args.append(max(1,min(limit,500)))
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
        """End only an existing active binding; retain its full structural history."""
        if not binding_id or len(binding_id)>128: return False
        with self.lock, self._connection() as conn:
            row=conn.execute("SELECT binding_id FROM relationship_bindings WHERE binding_id=? AND status='active'",(binding_id,)).fetchone()
            if not row: return False
            now=time.time()
            conn.execute("UPDATE relationship_bindings SET status='ended',ended_at=?,updated_at=?,state_json=json_set(state_json,'$.end_actor',?,'$.end_reason',?) WHERE binding_id=?",(now,now,actor[:64],reason_code[:64],binding_id))
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
        """Explicit legacy migration. Caller must nominate the identity; no inference."""
        import hashlib
        binding_id=hashlib.sha256(f"legacy_confirmed_migration:{identity}:{scope_kind}:{scope_id}:{type_key}".encode()).hexdigest()[:32]
        with self.lock, self._connection() as conn:
            # Serialize the occupancy check and insertion, including other store instances.
            conn.execute("BEGIN IMMEDIATE")
            existing=conn.execute("SELECT status FROM relationship_bindings WHERE binding_id=?",(binding_id,)).fetchone()
            if existing: return "already_migrated"
            if conn.execute("SELECT 1 FROM relationship_bindings WHERE scope_kind=? AND scope_id=? AND unique_scope='romance' AND status='active' LIMIT 1", (scope_kind, scope_id)).fetchone():
                return "binding_rejected:exclusive"
            now=time.time()
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

    def update_account_admin(self, *, identity: str, scope_kind: str, scope_id: str, expected_revision: int, values: dict[str, int], state_changes: dict[str, str], actor: str = "page_administrator") -> dict[str, Any] | None:
        """Atomic optimistic-concurrency Pages update for an existing account."""
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
            if "interaction_safety" in allowed:
                conn.execute("DELETE FROM timed_safety WHERE identity=? AND scope_kind=? AND scope_id=?",(identity,scope_kind,scope_id))
            conn.execute("UPDATE accounts SET values_json=?,state_json=?,revision=?,updated_at=? WHERE identity=? AND scope_kind=? AND scope_id=?",(json.dumps(values),json.dumps(state),expected_revision+1,now,identity,scope_kind,scope_id))
            requested={k:int(values[k])-int(account["values"].get(k,0)) for k in DIMENSIONS if int(values[k])!=int(account["values"].get(k,0))}
            event_id=f"{actor}:{hashlib.sha256((identity+scope_kind+scope_id).encode()).hexdigest()[:16]}:{time.time_ns()}"
            conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(event_id,identity,scope_kind,scope_id,"admin","","atomic page adjustment",json.dumps(requested),json.dumps(requested),json.dumps({"atomic":True, **{key:{"notes":["admin_set:"+value]} for key,value in allowed.items()}}),actor,now))
            return {"values":dict(values),"state":state,"revision":expected_revision+1}

    def decay_accounts(self, *, floors: dict[str,int], step: int, inactive_before: float, scope_allowed) -> int:
        """Floor-only decay. Administrative timestamps never affect this eligibility."""
        if set(floors)!=set(DIMENSIONS) or step<1: raise ValueError("invalid decay configuration")
        changed=0
        with self.lock, self._connection() as conn:
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
        cutoff=time.time()-max(1,retention_hours)*3600; cleaned=0
        for path in (self.backup_dir / "auto").glob("*.sqlite3"):
            if path.stat().st_mtime < cutoff: path.unlink(); cleaned+=1
        return cleaned

    def restore_backup(self, name: str, kind: str = "manual") -> None:
        if kind not in BACKUP_KINDS: raise ValueError("unknown backup kind")
        source=(self.backup_dir/kind/Path(name).name).resolve(); parent=(self.backup_dir/kind).resolve()
        if source.parent != parent or not source.is_file(): raise ValueError("backup not found")
        self.backup_now("pre_restore")
        with self.lock: shutil.copy2(source,self.path)

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
