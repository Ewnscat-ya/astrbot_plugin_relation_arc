from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_relation_arc.relation_engine import DEFAULT_VALUES, DIMENSIONS, aggregate_effects, apply_delta, behavior_projection
from astrbot_plugin_relation_arc.relation_protocol import parse_response
from astrbot_plugin_relation_arc.relation_store import SCHEMA_VERSION, RelationStore
from astrbot_plugin_relation_arc.config_manager import PluginConfigManager
from astrbot_plugin_relation_arc.relationship_types import get_type, public_directory, projected_eligibility


class RelationArcCoreTests(unittest.TestCase):
    def test_protocol_strips_and_validates_evidence(self):
        text = '正文回复。<relation_judgment>{"schema_version":1,"fact_effects":[{"basis":"user_and_assistant","evidence":"用户表达感谢，角色感到被尊重","reason":"守约","effects":{"trust":4}}]}</relation_judgment>'
        result = parse_response(text, '谢谢', 10)
        self.assertEqual('正文回复。', result.clean_text)
        self.assertEqual({'trust': 4}, result.effects[0]['effects'])

    def test_protocol_accepts_nonverbatim_audit_summary(self):
        text = '<relation_judgment>{"schema_version":1,"fact_effects":[{"basis":"invalid","evidence":"不存在","reason":"x","effects":{"trust":4}}]}</relation_judgment>'
        self.assertEqual({'trust': 4}, parse_response(text, 'hello', 10).effects[0]['effects'])


    def test_protocol_accepts_assistant_experience_summary(self):
        text = '<relation_judgment>{"schema_version":1,"fact_effects":[{"basis":"assistant","evidence":"角色因本轮互动感到安心","reason":"角色体验形成有效关系事实","effects":{"comfort":3}}]}</relation_judgment>'
        result = parse_response(text, '任意用户输入', 10)
        self.assertEqual({'comfort': 3}, result.effects[0]['effects'])
        self.assertEqual({'comfort': 3}, result.effects[0]['effects'])

    def test_calibrated_neutral_baseline_and_downward_projection(self):
        self.assertEqual({"trust":400,"respect":500,"comfort":450,"closeness":150,"resonance":100,"romance_interest":0}, DEFAULT_VALUES)
        neutral = behavior_projection(DEFAULT_VALUES, False, False, 'normal')
        self.assertIn('中性陌生/正常往来', neutral)
        self.assertIn('不预设欺骗', neutral)
        self.assertIn('不主动暴露脆弱', neutral)
        harmed = behavior_projection({**DEFAULT_VALUES, 'trust':100, 'comfort':100}, False, False, 'normal')
        self.assertIn('不采信', harmed)
        self.assertIn('明确不适', harmed)

    def test_aggregate_and_saturation(self):
        self.assertEqual(5, aggregate_effects([{'trust': 8}, {'trust': -3}], 'trust'))
        self.assertEqual(1, apply_delta(850, 8, 0, [1.0], False).applied)
        self.assertIn('恋爱意向', behavior_projection({**DEFAULT_VALUES, 'romance_interest':700}, True, True, 'normal'))

    def test_store_idempotency_backup_and_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            kwargs = dict(event_id='event-1', identity='qq:1', scope_kind='global', scope_id='', source_kind='private', evidence='谢谢', reason='测试', requested={'trust': 4}, applied={'trust': 4}, notes={})
            self.assertEqual(404, store.apply(**kwargs)['trust'])
            self.assertEqual(404, store.apply(**kwargs)['trust'])
            self.assertEqual(404, store.account('qq:1')['values']['trust'])
            self.assertEqual(1, store.repeat_count('qq:1', 'global', '', 'trust', '谢谢', time.time() - 60))
            self.assertTrue(store.backup_now().is_file())
            self.assertTrue(store.list_backups())

    def test_legacy_candidate_requires_exactly_one_admin_committed_account(self):
        with tempfile.TemporaryDirectory() as directory:
            store=RelationStore(Path(directory))
            store.account("qq:admin", "global", "")
            store.set_state("qq:admin", "global", "", romance_policy="shown", romance_state="committed")
            store.account("qq:other", "global", "")
            store.set_state("qq:other", "global", "", romance_policy="shown", romance_state="committed")
            candidates=store.confirmed_admin_candidates({"admin"})
            self.assertEqual([{"identity":"qq:admin","scope_kind":"global","scope_id":""}], candidates)
            self.assertEqual([], store.confirmed_admin_candidates({"missing"}))

    def test_identity_split_repair_copies_final_value_and_deletes_only_ghost(self):
        with tempfile.TemporaryDirectory() as directory:
            store=RelationStore(Path(directory)); store.account("qq:42","global",""); store.account("qq:display(42)","global","")
            store.set_dimension("qq:42","global","","trust",400)
            store.set_dimension("qq:display(42)","global","","trust",550)
            repaired=store.repair_legacy_identity_splits()
            self.assertEqual([{ "dimension":"trust", "scope_kind":"global", "final_value":550}],repaired)
            self.assertEqual(550,store.existing_account("qq:42","global","")["values"]["trust"])
            self.assertIsNone(store.existing_account("qq:display(42)","global", ""))
            events=store.recent("qq:42","global","",10)
            self.assertTrue(any(row["actor"]=="migration" for row in events))

    def test_legacy_identity_split_preview_is_read_only_and_scope_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            store=RelationStore(Path(directory))
            store.account("qq:display(42)","global","")
            store.account("qq:42","global","")
            store.account("qq:99","global","")
            # Without the provable single admin adjustment the split is not actionable.
            self.assertEqual([], store.legacy_identity_split_preview())
            store.set_dimension("qq:display(42)","global","","trust",550)
            preview=store.legacy_identity_split_preview()
            self.assertEqual(1,len(preview)); self.assertEqual("qq:42",preview[0]["canonical_identity"])
            self.assertEqual(3,len(store.list_accounts()))

    def test_romance_exclusive_group_rejects_second_type_for_same_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            store=RelationStore(Path(directory))
            first={"binding_id":"first","type_key":"romantic_partner","unique_scope":"romance","origin":"test","summary":""}
            second={"binding_id":"second","type_key":"spouse","unique_scope":"romance","origin":"test","summary":""}
            _,status=store.apply_turn_with_binding(event_id="e1",identity="qq:user",scope_kind="global",scope_id="",source_kind="private",evidence="",reason="",requested={},applied={},notes={},binding=first)
            self.assertEqual("binding_created",status)
            _,status=store.apply_turn_with_binding(event_id="e2",identity="qq:user",scope_kind="global",scope_id="",source_kind="private",evidence="",reason="",requested={},applied={},notes={},binding=second)
            self.assertEqual("binding_rejected:exclusive",status)
            self.assertEqual(1,len(store.list_bindings(status="active")))

    def test_ended_romance_binding_releases_exclusive_group_for_rebind(self):
        with tempfile.TemporaryDirectory() as directory:
            store=RelationStore(Path(directory)); first={"binding_id":"first","type_key":"romantic_partner","unique_scope":"romance","origin":"test","summary":""}; second={"binding_id":"second","type_key":"spouse","unique_scope":"romance","origin":"test","summary":""}
            store.apply_turn_with_binding(event_id="e1",identity="qq:a",scope_kind="global",scope_id="",source_kind="private",evidence="",reason="",requested={},applied={},notes={},binding=first)
            self.assertTrue(store.end_binding("first","test"))
            _,status=store.apply_turn_with_binding(event_id="e2",identity="qq:b",scope_kind="global",scope_id="",source_kind="private",evidence="",reason="",requested={},applied={},notes={},binding=second)
            self.assertEqual("binding_created",status)

    def test_legacy_migration_never_creates_second_romance_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            store=RelationStore(Path(directory)); store.migrate_confirmed_binding(identity="qq:a",scope_kind="global",scope_id="")
            self.assertEqual("already_migrated",store.migrate_confirmed_binding(identity="qq:a",scope_kind="global",scope_id=""))
            self.assertEqual(1,len(store.list_bindings(status="active")))

    def test_b3_confirmed_legacy_migration_is_explicit_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store=RelationStore(Path(directory))
            self.assertEqual("migrated", store.migrate_confirmed_binding(identity="qq:confirmed", scope_kind="global", scope_id=""))
            self.assertEqual("already_migrated", store.migrate_confirmed_binding(identity="qq:confirmed", scope_kind="global", scope_id=""))
            active=store.active_bindings_for("qq:confirmed","global","")
            self.assertEqual(1,len(active)); self.assertEqual("spouse",active[0]["type_key"])
            self.assertTrue(store.end_binding(active[0]["binding_id"],"test"))
            self.assertFalse(store.end_binding(active[0]["binding_id"],"test"))
            self.assertEqual([],store.active_bindings_for("qq:confirmed","global", ""))

    def test_schema_v2_binding_proposal_and_v1_compatibility(self):
        v1=parse_response('<relation_judgment>{"schema_version":1,"fact_effects":[]}</relation_judgment>reply', 'x')
        self.assertIsNone(v1.proposal)
        v2=parse_response('<relation_judgment>{"schema_version":2,"fact_effects":[],"relationship_proposal":{"action":"bind","type_id":"friend","origin":"mutual_dialogue","mutuality":"clear","summary":"ok"}}</relation_judgment>reply', 'x')
        self.assertEqual("friend", v2.proposal["type_id"])
        bad=parse_response('<relation_judgment>{"schema_version":2,"fact_effects":[],"relationship_proposal":{"action":"bind","type_id":"friend","origin":"bad","mutuality":"clear"}}</relation_judgment>reply', 'x')
        self.assertIsNone(bad.proposal)

    def test_b1_fixed_type_directory_and_spouse_display(self):
        self.assertEqual("此生挚爱", get_type("spouse").label)
        self.assertEqual("romance", get_type("spouse").exclusive_group)
        self.assertIsNone(get_type("任意自定义关系"))
        self.assertEqual(["friend", "close_friend", "partner", "romantic_partner", "spouse"], [item["key"] for item in public_directory()])
        values = {"trust": 900, "respect": 800, "comfort": 900, "closeness": 900, "resonance": 800, "romance_interest": 900}
        ok, reason = projected_eligibility(get_type("spouse"), values, {"romance_policy":"shown", "romance_state":"committed", "interaction_safety":"normal"}, True)
        self.assertTrue(ok); self.assertEqual("eligible", reason)
        denied, reason = projected_eligibility(get_type("spouse"), values, {"romance_policy":"observing", "romance_state":"eligible", "interaction_safety":"normal"}, True)
        self.assertFalse(denied); self.assertEqual("route", reason)

    def test_b0_binding_schema_migrates_empty_and_scope_queries_are_isolated(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "relation_arc.sqlite3"
            conn = sqlite3.connect(db)
            conn.execute("PRAGMA user_version=5")
            conn.execute("CREATE TABLE accounts (identity TEXT NOT NULL, scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL DEFAULT '', values_json TEXT NOT NULL, paused INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL, state_json TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(identity,scope_kind,scope_id))")
            conn.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY, identity TEXT NOT NULL, scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL, source_kind TEXT NOT NULL, evidence TEXT NOT NULL, reason TEXT NOT NULL, requested_json TEXT NOT NULL, applied_json TEXT NOT NULL, notes_json TEXT NOT NULL, actor TEXT NOT NULL, created_at REAL NOT NULL)")
            conn.execute("CREATE TABLE migration_log (id INTEGER PRIMARY KEY AUTOINCREMENT, component TEXT NOT NULL, from_version INTEGER NOT NULL, to_version INTEGER NOT NULL, backup_name TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL)")
            conn.execute("CREATE TABLE protocol_health (id INTEGER PRIMARY KEY AUTOINCREMENT, outcome TEXT NOT NULL, source TEXT NOT NULL, bare_recovery INTEGER NOT NULL DEFAULT 0, effect_count INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)")
            conn.commit(); conn.close()
            store = RelationStore(root)
            verify = sqlite3.connect(db)
            try:
                self.assertEqual(SCHEMA_VERSION, verify.execute("PRAGMA user_version").fetchone()[0])
                self.assertIn("relationship_bindings", {row[0] for row in verify.execute("SELECT name FROM sqlite_master WHERE type='table'")})
                self.assertEqual(0, verify.execute("SELECT count(*) FROM relationship_bindings").fetchone()[0])
            finally: verify.close()
            self.assertEqual([], store.list_bindings())
            self.assertEqual({"total":0,"active":0,"ended":0,"global":0,"session":0}, store.binding_overview())
            self.assertTrue(any(item["kind"] == "migration" for item in store.list_backups()))

    def test_config_rejects_invalid_query_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            manager=PluginConfigManager(ROOT,Path(directory)); manager.load_or_create()
            with self.assertRaises(ValueError): manager.update({"query_permission":{"group_normal_user":"yes"}})

    def test_config_rejects_runtime_breaking_nested_shapes(self):
        with tempfile.TemporaryDirectory() as directory:
            manager=PluginConfigManager(ROOT,Path(directory)); config=manager.load_or_create()
            for patch in ({"repeat_factors":"bad"},{"anti_farm":{"rolling_window_hours":0}},{"romance":{"eligibility_thresholds":{"trust":"bad"}}},{"allowed_sessions":[3]}):
                with self.assertRaises(ValueError): manager.update(patch)
            self.assertEqual(config["raw_delta_limit"],manager.config["raw_delta_limit"])

    def test_v6_config_migration_preserves_existing_values_and_creates_protected_backup(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "plugin_data" / "astrbot_plugin_relation_arc" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps({"config_version": 4, "is_global_relation": False, "initial_values": {"trust": 450}}), encoding="utf-8")
            mgr = PluginConfigManager(root, root)
            config = mgr.load_or_create()
            self.assertEqual(6, config["config_version"])
            self.assertFalse(config["is_global_relation"])
            self.assertEqual(450, config["initial_values"]["trust"])
            self.assertIn("protocol_health", config)
            self.assertEqual(1, len(list((config_path.parent / "backups" / "migration").glob("config_v4_to_v6_*.json"))))

    def test_v5_schema_migration_preserves_ledger_and_creates_protected_backup(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "relation_arc.sqlite3"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE accounts (identity TEXT NOT NULL, scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL DEFAULT '', values_json TEXT NOT NULL, paused INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL, PRIMARY KEY(identity,scope_kind,scope_id))")
            conn.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY, identity TEXT NOT NULL, scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL, source_kind TEXT NOT NULL, evidence TEXT NOT NULL, reason TEXT NOT NULL, requested_json TEXT NOT NULL, applied_json TEXT NOT NULL, notes_json TEXT NOT NULL, actor TEXT NOT NULL, created_at REAL NOT NULL)")
            conn.execute("INSERT INTO accounts VALUES(?,?,?,?,?,?,?)", ("qq:legacy", "global", "", '{"trust": 444}', 0, 3, time.time()))
            conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("legacy-event", "qq:legacy", "global", "", "private", "keep-private", "keep-private", '{"trust": 4}', '{"trust": 4}', '{}', "llm", time.time()))
            conn.commit(); conn.close()
            store = RelationStore(root)
            verify_conn = sqlite3.connect(db)
            try:
                self.assertEqual(SCHEMA_VERSION, verify_conn.execute("PRAGMA user_version").fetchone()[0])
            finally:
                verify_conn.close()
            self.assertEqual(444, store.account("qq:legacy")["values"]["trust"])
            self.assertEqual("legacy-event", store.list_events()[0]["event_id"])
            migrations = store.list_migrations()
            self.assertTrue(any(item["from_version"] == 0 and item["to_version"] == SCHEMA_VERSION for item in migrations))
            backups = store.list_backups()
            self.assertTrue(any(item["kind"] == "migration" for item in backups))
            self.assertFalse(store.cleanup_auto_backups(1))

    def test_protocol_health_and_safe_audit_cards(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            store.apply(event_id="card-event", identity="qq:private", scope_kind="global", scope_id="", source_kind="private", evidence="private evidence", reason="private reason", requested={"trust": 4}, applied={"trust": 4}, notes={"trust": {"notes": ("repeat_decay",)}}, actor="llm")
            store.record_protocol_health("applied", "result_chain", True, 1, 30)
            card = store.audit_cards()[0]
            self.assertEqual({"trust": 4}, card["applied"])
            self.assertNotIn("identity", card)
            self.assertNotIn("evidence", card)
            self.assertNotIn("reason", card)
            health = store.protocol_health_summary()
            self.assertEqual(1, health["total"])
            self.assertEqual("applied", health["buckets"][0]["outcome"])

    def test_window_positive_total(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            store.apply(event_id='event-a', identity='qq:2', scope_kind='global', scope_id='', source_kind='private', evidence='a', reason='r', requested={'trust': 4}, applied={'trust': 4}, notes={})
            store.apply(event_id='event-b', identity='qq:2', scope_kind='global', scope_id='', source_kind='private', evidence='b', reason='r', requested={'trust': -2}, applied={'trust': -2}, notes={})
            self.assertEqual(4, store.positive_window_total('qq:2', 'global', '', 'trust', time.time() - 60))

    def test_account_page_filters_exact_scope_and_counts_all_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            store=RelationStore(Path(directory))
            for i in range(23): store.account(f"qq:{i}","session","s1")
            store.account("qq:other","session","s2")
            self.assertEqual(23,store.count_accounts("session","s1"))
            self.assertEqual(3,len(store.list_accounts_page(page=2,page_size=20,scope_kind="session",scope_id="s1")))

    def test_v3_bare_verdict_is_stripped_and_safety_is_parsed(self):
        from astrbot_plugin_relation_arc.relation_protocol import parse_response
        raw='{"schema_version":3,"fact_effects":[],"interaction_safety_proposal":{"level":"slow_down","reason_code":"boundary_pressure"}}visible reply'
        parsed=parse_response(raw,"",10)
        self.assertEqual("visible reply",parsed.clean_text)
        self.assertEqual("slow_down",parsed.safety_proposal["level"])

    def test_c3_decay_uses_last_interaction_and_never_raises_to_floor(self):
        with tempfile.TemporaryDirectory() as directory:
            store=RelationStore(Path(directory)); store.account("qq:active","global","")
            store.apply_turn_with_binding(event_id="interaction",identity="qq:active",scope_kind="global",scope_id="",source_kind="private",evidence="",reason="",requested={"trust":10},applied={"trust":10},notes={},binding=None)
            row=store.existing_account("qq:active","global",""); self.assertGreater(row["last_interaction"],0)
            # Admin edit must not refresh interaction eligibility.
            old_last=row["last_interaction"]; store.set_dimension("qq:active","global","","trust",700)
            self.assertEqual(old_last,store.existing_account("qq:active","global","")["last_interaction"])
            changed=store.decay_accounts(floors={key:500 for key in DEFAULT_VALUES},step=20,inactive_before=time.time()+1,scope_allowed=lambda *_:True)
            values=store.existing_account("qq:active","global","")["values"]
            self.assertEqual(1,changed); self.assertEqual(680,values["trust"])
            # Values below floor are untouched, never lifted to it.
            self.assertEqual(450,values["comfort"])

    def test_c1_timed_safety_is_durable_expires_and_manual_state_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); store=RelationStore(root); store.account("qq:1","global","")
            store.apply_turn_with_binding(event_id="timer-1",identity="qq:1",scope_kind="global",scope_id="",source_kind="private",evidence="",reason="",requested={},applied={},notes={},binding=None,timed_safety={"level":"slow_down","duration_minutes":1})
            active=store.active_timed_safety("qq:1","global","")
            self.assertEqual("slow_down",store.effective_interaction_safety("qq:1","global",""))
            self.assertEqual("normal",store.existing_account("qq:1","global","")["state"]["interaction_safety"])
            reopened=RelationStore(root); self.assertEqual("slow_down",reopened.active_timed_safety("qq:1","global","")["level"])
            self.assertIsNone(reopened.active_timed_safety("qq:1","global","",now=active["expires_at"]+1))
            reopened.set_interaction_safety_admin("qq:1","global","","pause_intimacy")
            self.assertEqual("pause_intimacy",reopened.effective_interaction_safety("qq:1","global",""))

    def test_set_state_rejects_invalid_enum_values(self):
        with tempfile.TemporaryDirectory() as directory:
            store=RelationStore(Path(directory)); store.account("qq:1","global","")
            for changes in ({"interaction_safety":"bogus_value"},{"romance_policy":"not_a_policy"},{"romance_state":"???"}):
                with self.assertRaises(ValueError): store.set_state("qq:1","global","",**changes)

    def test_set_state_rejects_unknown_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            store=RelationStore(Path(directory)); store.account("qq:1","global","")
            with self.assertRaises(ValueError): store.set_state("qq:1","global","",state_changes="ignored")

    def test_atomic_admin_update_rejects_stale_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            store=RelationStore(Path(directory)); a=store.account("qq:1","global",""); values={**a["values"],"trust":500}
            done=store.update_account_admin(identity="qq:1",scope_kind="global",scope_id="",expected_revision=a["revision"],values=values,state_changes={"romance_policy":"hidden","romance_state":"hidden","interaction_safety":"normal"})
            self.assertEqual(500,done["values"]["trust"])
            with self.assertRaises(RuntimeError):store.update_account_admin(identity="qq:1",scope_kind="global",scope_id="",expected_revision=a["revision"],values=values,state_changes={})

    def test_manual_dimension_update_is_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            values = store.set_dimension('qq:admin-target', 'global', '', 'comfort', 327)
            self.assertEqual(327, values['comfort'])
            row = store.recent('qq:admin-target', 'global', '', 1)[0]
            self.assertEqual('administrator', row['actor'])
            self.assertEqual({'comfort': -123}, __import__('json').loads(row['applied_json']))

    def test_v4_migration_resets_and_preserves_special_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            store.account('qq:ordinary')
            special = {"trust":900,"respect":880,"comfort":920,"closeness":950,"resonance":860,"romance_interest":940}
            result = store.migrate_v4_reset('qq:special', special)
            self.assertEqual(1, result['reset_accounts'])
            self.assertEqual(DEFAULT_VALUES, store.account('qq:ordinary')['values'])
            profile = store.account('qq:special')
            self.assertEqual(special, profile['values'])
            self.assertEqual('committed', profile['state']['romance_state'])


class SchemaVersionGuardTests(unittest.TestCase):
    """MIS-89: never rewrite or open a database whose schema is newer."""

    def test_future_schema_version_refuses_and_preserves(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            store.account("qq:keep", "global", "")
            db_path = Path(directory) / "relation_arc.sqlite3"
            raw = sqlite3.connect(db_path)
            raw.execute("PRAGMA user_version=12")
            raw.commit()
            raw.close()
            with self.assertRaises(ValueError) as ctx:
                RelationStore(Path(directory))
            self.assertIn("future", str(ctx.exception))
            check = sqlite3.connect(db_path)
            version = check.execute("PRAGMA user_version").fetchone()[0]
            kept = check.execute("SELECT identity FROM accounts WHERE identity='qq:keep'").fetchone()
            check.close()
            self.assertEqual(12, version)
            self.assertIsNotNone(kept)

    def test_corrupt_database_refused_and_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "relation_arc.sqlite3"
            payload = b"this is definitely not a sqlite database" * 8
            db_path.write_bytes(payload)
            with self.assertRaises(RuntimeError):
                RelationStore(Path(directory))
            self.assertEqual(payload, db_path.read_bytes())


class LegacyIdentityRepairGuardTests(unittest.TestCase):
    """MIS-89: repair only provable admin display-name splits; keep ambiguous history."""

    def _store_with_split(self, directory):
        store = RelationStore(Path(directory))
        store.account("qq:42", "global", "")
        store.set_dimension("qq:42", "global", "", "trust", 400)
        store.account("qq:display(42)", "global", "")
        store.set_dimension("qq:display(42)", "global", "", "trust", 550)
        return store

    def test_repair_skips_when_llm_history_present(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_split(directory)
            store.apply(event_id="llm-hist", identity="qq:display(42)", scope_kind="global", scope_id="",
                        source_kind="private", evidence="real", reason="real",
                        requested={"closeness": 2}, applied={"closeness": 2}, notes={}, actor="llm")
            self.assertEqual([], store.legacy_identity_split_preview())
            self.assertEqual([], store.repair_legacy_identity_splits())
            self.assertIsNotNone(store.existing_account("qq:display(42)", "global", ""))
            self.assertEqual(550, store.existing_account("qq:display(42)", "global", "")["values"]["trust"])
            self.assertEqual(400, store.existing_account("qq:42", "global", "")["values"]["trust"])
            self.assertIn("llm_or_other_history_present", store.legacy_identity_split_diagnostic_counts())

    def test_repair_skips_when_binding_history_present(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_split(directory)
            first = {"binding_id": "ghostbind", "type_key": "friend", "unique_scope": "", "origin": "test", "summary": ""}
            _, status = store.apply_turn_with_binding(event_id="be1", identity="qq:display(42)", scope_kind="global",
                                                      scope_id="", source_kind="private", evidence="", reason="",
                                                      requested={}, applied={}, notes={}, binding=first)
            self.assertEqual("binding_created", status)
            self.assertEqual([], store.repair_legacy_identity_splits())
            self.assertIsNotNone(store.existing_account("qq:display(42)", "global", ""))
            self.assertEqual(1, len(store.list_bindings(status="active")))
            self.assertIn("binding_history_present", store.legacy_identity_split_diagnostic_counts())

    def test_repair_skips_with_timed_safety(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_split(directory)
            _, status = store.apply_turn_with_binding(event_id="ts1", identity="qq:display(42)", scope_kind="global",
                                                      scope_id="", source_kind="private", evidence="", reason="",
                                                      requested={}, applied={}, notes={}, binding=None,
                                                      timed_safety={"level": "slow_down", "duration_minutes": 30})
            self.assertEqual("no_binding", status)
            self.assertEqual([], store.repair_legacy_identity_splits())
            self.assertIsNotNone(store.existing_account("qq:display(42)", "global", ""))
            self.assertIn("timed_safety_present", store.legacy_identity_split_diagnostic_counts())

    def test_repair_skips_with_settlement_blacklist(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_split(directory)
            store.blacklist_settlement("qq:display(42)", "global", "", "settlement_limit")
            self.assertEqual([], store.repair_legacy_identity_splits())
            self.assertIsNotNone(store.existing_account("qq:display(42)", "global", ""))
            self.assertIn("blacklist_present", store.legacy_identity_split_diagnostic_counts())

    def test_repair_is_idempotent_and_atomic_on_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            for suffix in ("42", "77"):
                store.account(f"qq:{suffix}", "global", "")
                store.account(f"qq:display({suffix})", "global", "")
                store.set_dimension(f"qq:{suffix}", "global", "", "trust", 400)
                store.set_dimension(f"qq:display({suffix})", "global", "", "trust", 600)
            from unittest import mock
            with mock.patch("astrbot_plugin_relation_arc.relation_store.time.time_ns", side_effect=[1, RuntimeError("interrupted")]):
                with self.assertRaises(RuntimeError):
                    store.repair_legacy_identity_splits()
            for suffix in ("42", "77"):
                self.assertIsNotNone(store.existing_account(f"qq:display({suffix})", "global", ""))
                self.assertEqual(400, store.existing_account(f"qq:{suffix}", "global", "")["values"]["trust"])
            repaired = store.repair_legacy_identity_splits()
            self.assertEqual(2, len(repaired))
            self.assertEqual(600, store.existing_account("qq:42", "global", "")["values"]["trust"])
            self.assertEqual(600, store.existing_account("qq:77", "global", "")["values"]["trust"])
            self.assertIsNone(store.existing_account("qq:display(42)", "global", ""))
            self.assertIsNone(store.existing_account("qq:display(77)", "global", ""))
            self.assertEqual([], store.repair_legacy_identity_splits())
            self.assertEqual([], store.legacy_identity_split_preview())


class ProtocolHardeningTests(unittest.TestCase):
    """MIS-91: parse budgets and explicit truncated-block contract."""

    def _block(self, inner: str) -> str:
        return f"<relation_judgment>{inner}</relation_judgment>"

    def test_oversized_block_rejected_but_stripped(self):
        inner = '{"schema_version":1,"fact_effects":[],"note":"' + "x" * 70000 + '"}'
        result = parse_response("可见" + self._block(inner), "x", 10)
        self.assertEqual("invalid_json", result.error)
        self.assertEqual("可见", result.clean_text)
        self.assertEqual([], result.effects)

    def test_fact_items_beyond_budget_are_truncated(self):
        items = ",".join('{"effects":{"trust":1}}' for _ in range(100))
        result = parse_response(self._block('{"schema_version":1,"fact_effects":[%s]}' % items), "x", 10)
        self.assertIsNone(result.error)
        self.assertEqual(64, len(result.effects))
        self.assertEqual(36, result.stats["truncated_items"])

    def test_deep_nesting_does_not_crash(self):
        deep = '{"schema_version":1,"fact_effects":' + "[" * 3000 + "]" * 3000 + "}"
        result = parse_response(self._block(deep), "x", 10)
        self.assertIsNotNone(result.error)
        self.assertEqual([], result.effects)

    def test_multiple_blocks_last_payload_wins_and_all_stripped(self):
        text = ("开头" + self._block('{"schema_version":1,"fact_effects":[{"effects":{"trust":5}}]}')
                + "中段" + self._block('{"schema_version":1,"fact_effects":[{"effects":{"comfort":-3}}]}') + "结尾")
        result = parse_response(text, "x", 10)
        self.assertIsNone(result.error)
        self.assertEqual({"comfort": -3}, result.effects[0]["effects"])
        self.assertEqual("开头中段结尾", result.clean_text)
        self.assertEqual(2, result.stats["blocks"])

    def test_truncated_tail_opener_is_removed(self):
        text = '可见回复。<relation_judgment>{"schema_version":1,'
        result = parse_response(text, "x", 10)
        self.assertEqual("可见回复。", result.clean_text)
        self.assertIsNone(result.error)
        self.assertEqual([], result.effects)

    def test_mid_text_unclosed_opener_is_kept_and_not_settled(self):
        text = "前文 <relation_judgment>{\"broken\": true} 后续正文"
        result = parse_response(text, "x", 10)
        self.assertIn("<relation_judgment>", result.clean_text)
        self.assertIsNone(result.error)
        self.assertEqual([], result.effects)

    def test_ordinary_json_code_sample_is_not_swallowed(self):
        text = '看这个例子：\n{"schema_version": 9, "fact_effects": []}\n完毕'
        result = parse_response(text, "x", 10)
        self.assertIsNone(result.error)
        self.assertIn('"schema_version": 9', result.clean_text)
        self.assertEqual([], result.effects)


class SettlementAtomicityTests(unittest.TestCase):
    """MIS-92: one transaction per settlement; database-level exclusivity."""

    def _policy(self, trust_ceiling=5):
        return {
            "repeat_window_minutes": 180,
            "repeat_factors": [1.0],
            "anti_farm": {"rolling_window_hours": 24,
                          "positive_change_ceiling": {"trust": trust_ceiling, "respect": 50,
                                                      "comfort": 40, "closeness": 30,
                                                      "resonance": 30, "romance_interest": 15}},
            "safety_mode": "administrator_only",
            "auto_duration_minutes": 30,
        }

    @staticmethod
    def _no_binding(projected, state):
        return (None, "no_proposal")

    @staticmethod
    def _romance_open(values, state):
        return True

    def test_concurrent_settlements_respect_window_ceiling(self):
        import threading
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            store.account("qq:race", "global", "")
            policy = self._policy(trust_ceiling=5)
            barrier = threading.Barrier(2)
            outcomes = []

            def worker(index):
                barrier.wait()
                outcomes.append(store.settle_turn(
                    event_id=f"race-{index}", identity="qq:race", scope_kind="global", scope_id="",
                    source_kind="private", evidence="same fact", reason="r",
                    requested_all={"trust": 4}, fact_signature='{"trust": 4}',
                    safety_proposal=None, policy=policy,
                    romance_gate=self._romance_open, binding_gate=self._no_binding))

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
            for t in threads: t.start()
            for t in threads: t.join()
            self.assertEqual({"committed", "committed"}, {status for _, status, _ in outcomes})
            final = store.existing_account("qq:race", "global", "")["values"]["trust"]
            self.assertLessEqual(final, 405)
            self.assertEqual(final, 400 + sum(sum(info["applied"].values()) for _, _, info in outcomes))

    def test_duplicate_replay_leaves_no_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            policy = self._policy()
            kwargs = dict(identity="qq:dup", scope_kind="global", scope_id="", source_kind="private",
                          evidence="e", reason="r", requested_all={"trust": 4},
                          fact_signature='{"trust": 4}', safety_proposal=None, policy=policy,
                          romance_gate=self._romance_open, binding_gate=self._no_binding)
            values, status, info = store.settle_turn(event_id="same-1", **kwargs)
            self.assertEqual("committed", status)
            before_revision = store.existing_account("qq:dup", "global", "")["revision"]
            before_events = len(store.recent("qq:dup", "global", "", 50))
            values, status, info = store.settle_turn(event_id="same-1", **kwargs)
            self.assertEqual("duplicate", status)
            self.assertEqual({}, values)
            self.assertEqual(before_revision, store.existing_account("qq:dup", "global", "")["revision"])
            self.assertEqual(before_events, len(store.recent("qq:dup", "global", "", 50)))
            self.assertEqual(1, store.settlement_event_count("qq:dup", "global", ""))

    def test_concurrent_exclusive_bindings_have_single_winner(self):
        import threading
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            policy = self._policy()
            barrier = threading.Barrier(2)

            def worker(user, binding_id):
                gate = lambda projected, state: (
                    {"binding_id": binding_id, "type_key": "romantic_partner",
                     "unique_scope": "romance", "origin": "test", "summary": ""},
                    "eligible")
                barrier.wait()
                return store.settle_turn(
                    event_id=f"bind-{binding_id}", identity=f"qq:{user}", scope_kind="global",
                    scope_id="", source_kind="private", evidence="mutual", reason="mutual",
                    requested_all={"trust": 2}, fact_signature='{"trust": 2}',
                    safety_proposal=None, policy=policy,
                    romance_gate=self._romance_open, binding_gate=gate)

            threads = [threading.Thread(target=worker, args=("a", "ba")),
                       threading.Thread(target=worker, args=("b", "bb"))]
            for t in threads: t.start()
            for t in threads: t.join()
            active = store.list_bindings(status="active")
            self.assertEqual(1, len(active))
            self.assertEqual("romantic_partner", active[0]["type_key"])

    def test_unique_index_rejects_conflicting_active_row(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            binding_a = ("ida", "global", "", "romantic_partner", "romance")
            store.apply_turn_with_binding(
                event_id="e1", identity="qq:a", scope_kind="global", scope_id="",
                source_kind="private", evidence="", reason="", requested={}, applied={},
                notes={}, binding={"binding_id": "ida", "type_key": "romantic_partner",
                                   "unique_scope": "romance", "origin": "t", "summary": ""})
            raw = sqlite3.connect(Path(directory) / "relation_arc.sqlite3")
            with self.assertRaises(sqlite3.IntegrityError):
                raw.execute("INSERT INTO relationship_bindings(binding_id,identity,scope_kind,scope_id,type_key,status,unique_scope,origin_event_id,state_json,created_at,updated_at,ended_at) VALUES('idb','qq:b','global','','romantic_partner','active','romance','','{}',1,1,NULL)")
            raw.close()

    def test_admin_set_and_adjust_audits_actual_change(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            store.account("qq:admin", "global", "")
            store.set_dimension("qq:admin", "global", "", "trust", 550)
            store.adjust_dimension("qq:admin", "global", "", "trust", -20)
            final = store.existing_account("qq:admin", "global", "")["values"]["trust"]
            self.assertEqual(530, final)
            deltas = sum(int(json.loads(row["applied_json"]).get("trust", 0))
                         for row in store.recent("qq:admin", "global", "", 50))
            self.assertEqual(final - 400, deltas)

    def test_failure_injection_rolls_back_whole_settlement(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            policy = self._policy()
            with mock.patch.object(store, "_window_positive_total",
                                   side_effect=[0, RuntimeError("injected failure")]):
                with self.assertRaises(RuntimeError):
                    store.settle_turn(
                        event_id="boom", identity="qq:x", scope_kind="global", scope_id="",
                        source_kind="private", evidence="e", reason="r",
                        requested_all={"trust": 4, "comfort": 4},
                        fact_signature='{"comfort": 4, "trust": 4}',
                        safety_proposal=None, policy=policy,
                        romance_gate=self._romance_open, binding_gate=self._no_binding)
            self.assertIsNone(store.existing_account("qq:x", "global", ""))
            self.assertEqual([], store.recent("qq:x", "global", "", 10))


class BindingPolicyTests(unittest.TestCase):
    """MIS-94: cooldown is auditable; inject lists the single type directory."""

    def _policy(self, cooldowns=None):
        return {
            "repeat_window_minutes": 180,
            "repeat_factors": [1.0],
            "anti_farm": {"rolling_window_hours": 24,
                          "positive_change_ceiling": {"trust": 50, "respect": 50,
                                                      "comfort": 40, "closeness": 30,
                                                      "resonance": 30, "romance_interest": 15}},
            "safety_mode": "administrator_only",
            "auto_duration_minutes": 30,
            "type_cooldown_hours": cooldowns or {},
        }

    @staticmethod
    def _friend_binding(binding_id):
        return {"binding_id": binding_id, "type_key": "friend", "unique_scope": "",
                "origin": "mutual_dialogue", "summary": ""}

    def _settle(self, store, event_id, policy, binding):
        return store.settle_turn(
            event_id=event_id, identity="qq:cd", scope_kind="global", scope_id="",
            source_kind="private", evidence="mutual", reason="mutual",
            requested_all={"trust": 2}, fact_signature='{"trust": 2}',
            safety_proposal=None, policy=policy,
            romance_gate=lambda values, state: True,
            binding_gate=lambda projected, state: (binding, "eligible"))

    def test_cooldown_blocks_rebind_until_elapsed(self):
        from unittest import mock
        import astrbot_plugin_relation_arc.relation_store as store_mod
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            policy = self._policy(cooldowns={"friend": 24})
            _, status, _ = self._settle(store, "c1", policy, self._friend_binding("b1"))
            self.assertEqual("committed", status)
            self.assertTrue(store.end_binding("b1", "test"))
            _, status, info = self._settle(store, "c2", policy, self._friend_binding("b2"))
            self.assertEqual("committed", status)
            self.assertEqual("binding_rejected:cooldown", info["status"])
            active_ids = {b["binding_id"] for b in store.active_bindings_for("qq:cd", "global", "")}
            self.assertNotIn("b2", active_ids)
            # A different type has no cooldown entry and binds immediately.
            _, status, info = self._settle(store, "c3", policy, {**self._friend_binding("b3"), "type_key": "partner"})
            self.assertEqual("binding_created", info["status"])
            # After the cooldown elapses the same type binds again.
            real_time = store_mod.time
            class Shifted:
                def __init__(self, offset): self.offset = offset
                def time(self): return real_time.time() + self.offset
            with mock.patch.object(store_mod, "time", Shifted(25 * 3600)):
                _, status, info = self._settle(store, "c4", policy, self._friend_binding("b4"))
            self.assertEqual("binding_created", info["status"])

    def test_cooldown_reason_is_structured_in_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            policy = self._policy(cooldowns={"friend": 24})
            self._settle(store, "k1", policy, self._friend_binding("kb1"))
            store.end_binding("kb1", "test")
            _, status, info = self._settle(store, "k2", policy, self._friend_binding("kb2"))
            notes = info["notes"]
            self.assertEqual(["binding_rejected:cooldown"], notes["binding"]["notes"])


class BackupRestoreTests(unittest.TestCase):
    """MIS-95: prechecked restore, full-state recovery, failure rollback."""

    def _seed(self, store):
        store.account("qq:a", "global", "")
        store.set_dimension("qq:a", "global", "", "trust", 600)
        store.apply(event_id="seed-ev", identity="qq:a", scope_kind="global", scope_id="",
                    source_kind="private", evidence="e", reason="r",
                    requested={"trust": 2}, applied={"trust": 2}, notes={}, actor="llm")
        store.apply_turn_with_binding(
            event_id="seed-b", identity="qq:a", scope_kind="global", scope_id="",
            source_kind="private", evidence="", reason="", requested={}, applied={},
            notes={}, binding={"binding_id": "seed-bid", "type_key": "friend",
                               "unique_scope": "", "origin": "t", "summary": ""})
        store.apply_turn_with_binding(
            event_id="seed-ts", identity="qq:a", scope_kind="global", scope_id="",
            source_kind="private", evidence="", reason="", requested={}, applied={},
            notes={}, binding=None,
            timed_safety={"level": "slow_down", "duration_minutes": 60})
        store.blacklist_settlement("qq:a", "global", "", "settlement_limit")

    def test_restore_rejects_corrupt_future_and_out_of_bounds(self):
        for case in ("corrupt", "future", "zero"):
            with tempfile.TemporaryDirectory() as directory:
                store = RelationStore(Path(directory))
                self._seed(store)
                store.backup_now("manual")
                expected_revision = store.existing_account("qq:a", "global", "")["revision"]
                backup_file = next((Path(directory) / "backups" / "manual").glob("*.sqlite3"))
                import sqlite3
                raw = sqlite3.connect(backup_file)
                if case == "corrupt":
                    raw.close()
                    backup_file.write_bytes(b"junk" * 512)
                elif case == "future":
                    raw.execute("PRAGMA user_version=12")
                    raw.commit(); raw.close()
                else:
                    raw.execute("PRAGMA user_version=0")
                    raw.commit(); raw.close()
                with self.assertRaises(ValueError, msg=case):
                    store.restore_backup(backup_file.name, kind="manual")
                # Current database untouched by the rejected restore.
                account = store.existing_account("qq:a", "global", "")
                self.assertEqual(602, account["values"]["trust"])
                self.assertEqual(expected_revision, account["revision"])

    def test_restore_recovers_full_synthetic_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            self._seed(store)
            backup = store.backup_now("manual")
            # Perturb after the backup: new account, event, ended binding, cleared safety/blacklist.
            store.account("qq:b", "global", "")
            store.set_dimension("qq:a", "global", "", "trust", 100)
            store.end_binding("seed-bid", "test")
            store.set_interaction_safety_admin("qq:a", "global", "", "normal")
            store.clear_settlement_blacklist("qq:a", "global", "")
            store.restore_backup(backup.name, kind="manual")
            account = store.existing_account("qq:a", "global", "")
            self.assertEqual(602, account["values"]["trust"])
            self.assertIsNone(store.existing_account("qq:b", "global", ""))
            self.assertEqual(1, len(store.active_bindings_for("qq:a", "global", "")))
            self.assertEqual("slow_down", store.active_timed_safety("qq:a", "global", "")["level"])
            self.assertTrue(store.is_settlement_blacklisted("qq:a", "global", ""))
            self.assertIn("seed-ev", [row["event_id"] for row in store.recent("qq:a", "global", "", 50)])

    def test_restore_failure_leaves_current_database_intact(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            self._seed(store)
            backup = store.backup_now("manual")
            store.set_dimension("qq:a", "global", "", "trust", 100)
            before = store.existing_account("qq:a", "global", "")["values"]["trust"]
            before_revision = store.existing_account("qq:a", "global", "")["revision"]
            with mock.patch.object(store, "_copy_into_live", side_effect=RuntimeError("injected io failure")):
                with self.assertRaises(RuntimeError):
                    store.restore_backup(backup.name, kind="manual")
            after = store.existing_account("qq:a", "global", "")
            self.assertEqual(before, after["values"]["trust"])
            self.assertEqual(before_revision, after["revision"])
            # The pre-restore snapshot exists for manual recovery.
            self.assertTrue(any(item["kind"] == "pre_restore" for item in store.list_backups()))


class DecayPeriodTests(unittest.TestCase):
    """MIS-96: one decay per persisted period; floors and timestamps intact."""

    def _decay_kwargs(self, floors=None):
        return {"floors": floors or {key: 0 for key in DEFAULT_VALUES}, "step": 5,
                "inactive_before": time.time() + 1,
                "scope_allowed": lambda *_: True}

    def _seed_account(self, store, identity="qq:decay", trust=400, last_interaction=None):
        store.account(identity, "global", "")
        store.set_dimension(identity, "global", "", "trust", trust)
        if last_interaction is not None:
            import sqlite3
            raw = sqlite3.connect(store.path)
            raw.execute("UPDATE accounts SET last_interaction=? WHERE identity=?", (last_interaction, identity))
            raw.commit(); raw.close()

    def test_decay_if_due_runs_once_per_period(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            self._seed_account(store, last_interaction=time.time() - 10 * 3600)
            kwargs = self._decay_kwargs()
            self.assertTrue(store.decay_if_due(interval_seconds=1800, **kwargs))
            self.assertEqual(395, store.existing_account("qq:decay", "global", "")["values"]["trust"])
            self.assertFalse(store.decay_if_due(interval_seconds=1800, **kwargs))
            self.assertFalse(store.decay_if_due(interval_seconds=1800, **kwargs))
            self.assertEqual(395, store.existing_account("qq:decay", "global", "")["values"]["trust"])
            self.assertEqual(1, store.get_scheduler_state("decay_last_run") is not None and
                             sum(1 for row in store.recent("qq:decay", "global", "", 50)
                                 if row["actor"] == "scheduler"))

    def test_below_floor_never_raised_and_timestamps_not_forged(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            self._seed_account(store, trust=3, last_interaction=12345.0)
            floors = {key: 0 for key in DEFAULT_VALUES}
            floors["trust"] = 10
            store.decay_accounts(**self._decay_kwargs(floors=floors))
            account = store.existing_account("qq:decay", "global", "")
            self.assertEqual(3, account["values"]["trust"])
            self.assertEqual(12345.0, account["last_interaction"])
            store.set_dimension("qq:decay", "global", "", "trust", 300)
            store.adjust_dimension("qq:decay", "global", "", "trust", 5)
            self.assertEqual(12345.0, store.existing_account("qq:decay", "global", "")["last_interaction"])


class WindowMergeEquivalenceTests(unittest.TestCase):
    """MIS-97: merged window fetch is value-identical to per-dimension scans."""

    def _seed(self, store):
        import random
        rng = random.Random(7)
        now = time.time()
        import sqlite3
        conn = sqlite3.connect(store.path)
        events = []
        for index in range(200):
            identity = f"qq:w-{rng.randrange(3)}"
            trust = rng.randrange(-8, 9) or 1
            comfort = rng.randrange(-8, 9) or 1
            evidence = f"fact-{rng.randrange(6)}"
            events.append((f"wev-{index}", identity, "global", "", "private",
                           f"{evidence} | extra {index}", "r",
                           json.dumps({"trust": trust, "comfort": comfort}),
                           json.dumps({"trust": trust, "comfort": comfort}),
                           "{}", "llm", now - rng.randrange(0, 30 * 3600)))
        conn.executemany("INSERT INTO events(event_id,identity,scope_kind,scope_id,source_kind,evidence,reason,requested_json,applied_json,notes_json,actor,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", events)
        conn.commit(); conn.close()
        return now

    def test_merged_window_matches_per_dimension_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            now = self._seed(store)
            policy_window_minutes = 180
            farm_window_hours = 24
            repeat_since = now - policy_window_minutes * 60
            farm_since = now - farm_window_hours * 3600
            with store.lock, store._connection() as conn:
                window = store._window_rows(conn, "qq:w-1", "global", "", min(repeat_since, farm_since))
            for dimension in DIMENSIONS:
                expected_repeat = store.repeat_count("qq:w-1", "global", "", dimension, "", repeat_since)
                expected_positive = store.positive_window_total("qq:w-1", "global", "", dimension, farm_since)
                self.assertEqual(expected_repeat,
                                 store._window_repeat_count(window, repeat_since, dimension, "", None),
                                 f"repeat mismatch {dimension}")
                self.assertEqual(expected_positive,
                                 store._window_positive_total(window, farm_since, dimension),
                                 f"positive mismatch {dimension}")

    def test_window_rows_with_evidence_split_and_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            now = time.time()
            store.apply(event_id="w-1", identity="qq:s", scope_kind="global", scope_id="",
                        source_kind="private", evidence="共同事实 | 其他", reason="r",
                        requested={"trust": 2}, applied={"trust": 2}, notes={}, actor="llm")
            store.apply(event_id="w-2", identity="qq:s", scope_kind="global", scope_id="",
                        source_kind="private", evidence="共同事实", reason="r",
                        requested={"trust": 2}, applied={"trust": 2}, notes={}, actor="llm")
            signature = store.effect_signature({"trust": 2})
            with store.lock, store._connection() as conn:
                window = store._window_rows(conn, "qq:s", "global", "", now - 60)
            # Evidence-substring matching counts both rows.
            self.assertEqual(2, store._window_repeat_count(window, now - 60, "trust", "共同事实", None))
            # Signature matching with empty evidence also counts both.
            self.assertEqual(2, store._window_repeat_count(window, now - 60, "trust", "", signature))
            self.assertEqual(4, store._window_positive_total(window, now - 60, "trust"))


class ServerPaginationTests(unittest.TestCase):
    """MIS-98: bounded server-side pagination with exact totals and stable order."""

    def _seed_accounts(self, store, count, scope_id=""):
        scope_kind = "global" if scope_id == "" else "session"
        for index in range(count):
            store.account(f"qq:p-{index:05d}", scope_kind, scope_id)
            store.set_dimension(f"qq:p-{index:05d}", scope_kind, scope_id, "trust", 400 + (index % 3))

    def test_thousand_accounts_all_pages_reachable_and_total_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            self._seed_accounts(store, 1001)
            self.assertEqual(1001, store.count_accounts_page(scope_allowed=lambda *a: True))
            seen = []
            page = 1
            while True:
                rows = store.list_accounts_page(page=page, page_size=100, scope_allowed=lambda *a: True)
                seen.extend(row["identity"] for row in rows)
                if not rows: break
                if len(rows) < 100: break
                page += 1
            self.assertEqual(1001, len(seen))
            self.assertEqual(len(set(seen)), len(seen))

    def test_stable_order_for_equal_updated_at(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            self._seed_accounts(store, 50)
            first = [row["identity"] for row in store.list_accounts_page(page=1, page_size=100, scope_allowed=lambda *a: True)]
            second = [row["identity"] for row in store.list_accounts_page(page=1, page_size=100, scope_allowed=lambda *a: True)]
            self.assertEqual(first, second)

    def test_page_size_clamped(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            self._seed_accounts(store, 10)
            self.assertLessEqual(len(store.list_accounts_page(page=1, page_size=500, scope_allowed=lambda *a: True)), store.MAX_PAGE_SIZE)
            # page=0 clamps to page 1 (bounded parameter semantics).
            self.assertEqual(10, len(store.list_accounts_page(page=0, page_size=20, scope_allowed=lambda *a: True)))

    def test_bindings_and_events_paged(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            for index in range(60):
                identity = f"qq:b-{index:03d}"
                store.apply_turn_with_binding(
                    event_id=f"bp-{index}", identity=identity, scope_kind="global", scope_id="",
                    source_kind="private", evidence="", reason="", requested={}, applied={},
                    notes={}, binding={"binding_id": f"bid-{index}", "type_key": "friend",
                                       "unique_scope": "", "origin": "t", "summary": ""})
            store.apply(event_id=f"bp-ev", identity="qq:b-000", scope_kind="global", scope_id="",
                        source_kind="private", evidence="e", reason="r",
                        requested={"trust": 1}, applied={"trust": 1}, notes={}, actor="llm")
            self.assertEqual(60, store.count_bindings_page(scope_allowed=lambda *a: True))
            rows = store.list_bindings_page(page=2, page_size=50, scope_allowed=lambda *a: True)
            self.assertEqual(10, len(rows))
            # 60 binding-created events + 1 explicit llm event exist globally.
            self.assertGreaterEqual(store.count_events_page(scope_allowed=lambda *a: True), 61)
            self.assertEqual(50, len(store.list_events_page(page=1, page_size=50, scope_allowed=lambda *a: True)))
            # 60 binding settlements + 1 explicit llm event = 61; page 2 holds the remainder.
            self.assertEqual(11, len(store.list_events_page(page=2, page_size=50, scope_allowed=lambda *a: True)))

    def test_binding_counts_reflect_scope_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RelationStore(Path(directory))
            policy = self._friend_policy() if hasattr(self, "_friend_policy") else None
            del policy
            for user in ("a", "b"):
                store.account(f"qq:{user}", "session", "friend:1")
                store.account(f"qq:{user}-x", "session", "blocked:9")
            store.apply_turn_with_binding(
                event_id="v1", identity="qq:a", scope_kind="session", scope_id="friend:1",
                source_kind="private", evidence="", reason="", requested={}, applied={},
                notes={}, binding={"binding_id": "vb1", "type_key": "friend", "unique_scope": "",
                                   "origin": "t", "summary": ""})
            store.apply_turn_with_binding(
                event_id="v2", identity="qq:b-x", scope_kind="session", scope_id="blocked:9",
                source_kind="group", evidence="", reason="", requested={}, applied={},
                notes={}, binding={"binding_id": "vb2", "type_key": "friend", "unique_scope": "",
                                   "origin": "t", "summary": ""})
            allowed = lambda kind, sid: sid != "blocked:9"
            self.assertEqual(1, store.count_bindings_page(scope_allowed=allowed))
            self.assertEqual(1, len(store.list_bindings_page(page=1, page_size=50, scope_allowed=allowed)))


if __name__ == '__main__':
    unittest.main()