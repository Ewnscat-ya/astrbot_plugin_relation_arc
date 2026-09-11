from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_relation_arc.relation_engine import DEFAULT_VALUES, aggregate_effects, apply_delta, behavior_projection
from astrbot_plugin_relation_arc.relation_protocol import parse_response
from astrbot_plugin_relation_arc.relation_store import RelationStore
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
                self.assertEqual(9, verify.execute("PRAGMA user_version").fetchone()[0])
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
                self.assertEqual(9, verify_conn.execute("PRAGMA user_version").fetchone()[0])
            finally:
                verify_conn.close()
            self.assertEqual(444, store.account("qq:legacy")["values"]["trust"])
            self.assertEqual("legacy-event", store.list_events()[0]["event_id"])
            migrations = store.list_migrations()
            self.assertTrue(any(item["from_version"] == 0 and item["to_version"] == 9 for item in migrations))
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


if __name__ == '__main__':
    unittest.main()
