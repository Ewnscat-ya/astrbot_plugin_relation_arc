"""Package-level host compatibility verification (MIS-88).

Drives the plugin's real Pages handlers through AstrBot's real dashboard
compatibility layer (``FastAPIAppAdapter`` + ``call_request_view`` +
``_match_registered_web_api``), the same code path the host uses for
``/api/plug/...`` and ``/plugins/extensions/...``. A running AstrBot instance
is NOT required and NOT simulated: real-browser and end-to-end host
verification stay with the follow-up acceptance task.

Skipped when the ``astrbot`` package is absent (recorded as missing
dependency in docs/BASELINE_VALIDATION.md, never as test failure).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "tests"))

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from starlette.testclient import TestClient

    import astrbot.dashboard.api.plugins as dash_plugins
    from astrbot.core.star.context import Context
    from astrbot.dashboard.asgi_runtime import FastAPIAppAdapter, call_request_view
    from astrbot.dashboard.services import plugin_page_service as page_service

    HAS_HOST = True
except ImportError:  # pragma: no cover - depends on environment
    HAS_HOST = False

from astrbot_plugin_relation_arc.main import PLUGIN_NAME, RelationArc  # noqa: E402
from test_main import FakeContext  # noqa: E402


def _make_matcher(registered):
    def match(plugin_path: str, method: str):
        return dash_plugins._match_registered_web_api(registered, plugin_path, method)
    return match


@unittest.skipUnless(HAS_HOST, "astrbot host package not installed (missing dependency, not a failure)")
class HostCompatTests(unittest.TestCase):
    """Plugin Pages handlers behind the host's real Quart compatibility bridge."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.context = FakeContext(cls._tmp.name)
        cls.plugin = RelationArc(cls.context)
        # FakeContext recorded every register_web_api(*args) call; rebuild the
        # same (route, handler, methods, desc) tuples the host would keep.
        cls.registered = [tuple(args) for args in cls.context.apis]
        assert len(cls.registered) == 8, "plugin must register exactly the 8 Pages APIs"

        app = FastAPI()
        cls.adapter = FastAPIAppAdapter(app)

        match = _make_matcher(cls.registered)

        @app.api_route("/ext/{plugin_path:path}", methods=["GET", "POST"])
        async def ext(plugin_path: str, request: Request):  # mirrors _call_plugin_extension glue
            matched = match(plugin_path, request.method)
            if not matched:
                return JSONResponse({"status": "error", "message": "未找到该路由", "data": {}}, status_code=404)
            view_handler, path_values = matched
            return await call_request_view(request, cls.adapter, view_handler, path_values)

        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(cls.plugin.terminate())
        except RuntimeError:
            asyncio.new_event_loop().run_until_complete(cls.plugin.terminate())
        cls._tmp.cleanup()

    def prefix(self, name: str) -> str:
        return f"{PLUGIN_NAME}/{name}"

    def test_all_apis_registered_with_expected_methods(self):
        expected = {
            "config": {"GET", "POST"}, "accounts": {"GET", "POST"}, "audit": {"GET"},
            "backups": {"GET", "POST"}, "overview": {"GET"}, "health": {"GET"},
            "migrations": {"GET"}, "bindings": {"GET", "POST"},
        }
        actual = {route.rsplit("/", 1)[-1]: set(methods) for route, _, methods, _ in self.registered}
        self.assertEqual(expected, actual)

    def test_register_web_api_replaces_same_route_and_methods(self):
        ctx = Context.__new__(Context)
        ctx.registered_web_apis = []

        def handler():
            return None

        ctx.register_web_api("/route", handler, ["GET"], "first")
        ctx.register_web_api("/route", handler, ["GET"], "second")
        self.assertEqual(1, len(ctx.registered_web_apis))
        self.assertEqual("second", ctx.registered_web_apis[0][3])

    def test_host_plugin_routes_require_authentication(self):
        # 4.26 guards extensions with the require_plugin_scope function; 4.28
        # switched to ScopeDependency(scope='plugin') instances (whose __name__
        # lives on the class, not the instance). Legacy /api/plug keeps
        # require_dashboard_user in both versions.
        def dep_names(routes):
            names = set()
            for route in routes:
                for dep in route.dependant.dependencies:
                    names.add(getattr(dep.call, "__name__", ""))
                    names.add(type(dep.call).__name__)
            return names

        scoped = dep_names(dash_plugins.router.routes)
        plugin_guard = {"require_plugin_scope", "ScopeDependency"}
        self.assertTrue(plugin_guard & scoped, f"no plugin-scope auth guard in {sorted(scoped)}")
        legacy = dep_names(dash_plugins.legacy_router.routes)
        self.assertIn("require_dashboard_user", legacy)

    def test_pages_layout_matches_host_discovery_contract(self):
        root = ROOT / page_service.PLUGIN_PAGE_ROOT_DIR_NAME
        declared = []
        meta = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        for line in meta.splitlines():
            line = line.strip()
            if line.startswith("- name:"):
                declared.append(line.split(":", 1)[1].strip())
        self.assertEqual(["settings"], declared)
        for name in declared:
            entry = root / name / page_service.PLUGIN_PAGE_ENTRY_FILE_NAME
            self.assertTrue(entry.is_file(), f"missing page entry for {name}")

    def test_config_get_returns_full_config(self):
        response = self.client.get(f"/ext/{self.prefix('config')}")
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual(6, body["config_version"])

    def test_config_post_rejects_non_object_with_400(self):
        response = self.client.post(f"/ext/{self.prefix('config')}", json=[1, 2])
        self.assertEqual(400, response.status_code)
        self.assertIn("error", response.json())

    def test_config_post_rejects_invalid_field_with_400(self):
        response = self.client.post(f"/ext/{self.prefix('config')}", json={"raw_delta_limit": 99})
        self.assertEqual(400, response.status_code)

    def test_accounts_wrong_action_is_400(self):
        response = self.client.post(f"/ext/{self.prefix('accounts')}", json={"action": "delete_everything"})
        self.assertEqual(400, response.status_code)
        self.assertIn("update_account", response.json()["error"])

    # Pages API takes human display units (0-100.0, stored x10 internally).
    _SIX_VALUES = {"trust": 40, "respect": 50, "comfort": 45,
                   "closeness": 15, "resonance": 10, "romance_interest": 0}

    def _update_account_payload(self, **overrides):
        payload = {"action": "update_account", "identity": "qq-missing", "scope_kind": "global",
                   "scope_id": "", "revision": 0, "romance_policy": "hidden",
                   "interaction_safety": "normal", "values": dict(self._SIX_VALUES)}
        payload.update(overrides)
        return payload

    def test_accounts_error_ladder_400_403_404_409(self):
        base = "/ext/" + self.prefix("accounts")
        # 400: partial dimension set is rejected before any lookup.
        partial = self._update_account_payload(values={"trust": 400})
        self.assertEqual(400, self.client.post(base, json=partial).status_code)
        # 403: disallowed scope kind is rejected before account lookup.
        response = self.client.post(base, json=self._update_account_payload(scope_kind="nonsense"))
        self.assertEqual(403, response.status_code)
        # 404: existing scope, missing account, no implicit creation.
        response = self.client.post(base, json=self._update_account_payload())
        self.assertEqual(404, response.status_code)
        # 409: stale revision against an existing account.
        account = self.plugin.store.account("qq-conflict", "global", "")
        response = self.client.post(base, json=self._update_account_payload(
            identity="qq-conflict", revision=account["revision"] + 5))
        self.assertEqual(409, response.status_code)
        # And the matching revision succeeds.
        response = self.client.post(base, json=self._update_account_payload(identity="qq-conflict"))
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["success"])

    def test_bindings_wrong_action_400_and_unknown_binding_403(self):
        # 400: only "end" is accepted.
        response = self.client.post(f"/ext/{self.prefix('bindings')}", json={"action": "resurrect"})
        self.assertEqual(400, response.status_code)
        # 403: an unknown binding's scope cannot be proven open, so the handler
        # refuses without revealing existence.
        response = self.client.post(f"/ext/{self.prefix('bindings')}", json={
            "action": "end", "binding_id": "no-such-binding"})
        self.assertEqual(403, response.status_code)

    def test_read_only_gets_all_reachable(self):
        for name in ("audit", "backups", "overview", "health", "migrations", "accounts"):
            response = self.client.get(f"/ext/{self.prefix(name)}")
            self.assertEqual(200, response.status_code, f"{name} -> {response.status_code}")

    def test_unknown_route_follows_host_miss_semantics(self):
        response = self.client.get(f"/ext/{self.prefix('no-such-api')}")
        self.assertIn(response.status_code, (404, 200))
        if response.status_code == 200:
            # Host legacy miss returns its own error envelope with 200 in some
            # versions; either way the plugin handler must not have run.
            self.assertIn(response.json().get("status"), ("error", None))

    def test_backups_post_manual_backup_roundtrip(self):
        response = self.client.post(f"/ext/{self.prefix('backups')}", json={"action": "backup_now"})
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["success"])
        listing = self.client.get(f"/ext/{self.prefix('backups')}")
        self.assertEqual(200, listing.status_code)


if __name__ == "__main__":
    unittest.main()
