"""MIS-158/159 request-sample fixtures: save full request snapshots covering
the goal's matrix so baseline and candidate layouts stay auditable.

Scenarios: empty system / persona / other-plugin parts / repeated inject /
disabled-then-re-enabled / account & score changes / scope switch / route
(hidden->shown) / effective-policy switch (exclusivity on).

Output: one JSON per scenario under tools/prompt_samples/, each containing
the final system_prompt and the extra_user_content_parts (text + temp flag).
Run with the host venv python from the plugin parent directory.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "tests"))

from astrbot.api.provider import ProviderRequest  # noqa: E402
from astrbot.core.agent.message import TextPart  # noqa: E402

from test_main import FakeContext, FakeEvent  # noqa: E402
from astrbot_plugin_relation_arc.main import RelationArc  # noqa: E402


def _dump(req: ProviderRequest) -> dict:
    return {
        "system_prompt": req.system_prompt,
        "extra_user_content_parts": [
            {"text": part.text, "temp": bool(getattr(part, "_no_save", False))}
            for part in req.extra_user_content_parts],
    }


def main() -> int:
    out_dir = ROOT / "tools" / "prompt_samples"
    out_dir.mkdir(parents=True, exist_ok=True)
    scenarios = {}

    def fresh(plugin=None):
        temp = tempfile.TemporaryDirectory()
        plugin = RelationArc(FakeContext(temp.name))
        return temp, plugin

    # 1) empty system
    temp, plugin = fresh()
    req = ProviderRequest(prompt="hi")
    await_ = plugin.inject(FakeEvent(), req)
    import asyncio
    asyncio.get_event_loop().run_until_complete(await_) if False else asyncio.run(_run(plugin, FakeEvent(), req))
    scenarios["empty_system"] = _dump(req)
    asyncio.run(plugin.terminate()); temp.cleanup()

    # 2) persona + repeated inject (idempotency) + account change refresh
    temp, plugin = fresh()
    req = ProviderRequest(prompt="hi", system_prompt="persona")
    asyncio.run(_run(plugin, FakeEvent(), req))
    first = _dump(req)
    plugin.store.set_dimension("qq-adapter:user-1", "global", "", "trust", 432)
    asyncio.run(_run(plugin, FakeEvent(), req))
    scenarios["persona_repeat_inject"] = {"first": first, "second": _dump(req)}
    asyncio.run(plugin.terminate()); temp.cleanup()

    # 3) other-plugin parts survive
    temp, plugin = fresh()
    req = ProviderRequest(prompt="hi", system_prompt="persona\n<OtherPlugin>x</OtherPlugin>")
    req.extra_user_content_parts.append(TextPart(text="<Other>keep</Other>"))
    asyncio.run(_run(plugin, FakeEvent(), req))
    asyncio.run(_run(plugin, FakeEvent(), req))
    scenarios["other_plugin_parts"] = _dump(req)
    asyncio.run(plugin.terminate()); temp.cleanup()

    # 4) disabled cleans previous injection; re-enable restores
    temp, plugin = fresh()
    req = ProviderRequest(prompt="hi", system_prompt="persona")
    asyncio.run(_run(plugin, FakeEvent(), req))
    plugin.config["llm_judgment_enabled"] = False
    asyncio.run(_run(plugin, FakeEvent(), req))
    disabled = _dump(req)
    plugin.config["llm_judgment_enabled"] = True
    asyncio.run(_run(plugin, FakeEvent(), req))
    scenarios["disable_reenable"] = {"disabled": disabled, "reenabled": _dump(req)}
    asyncio.run(plugin.terminate()); temp.cleanup()

    # 5) scope switch (session mode) and route/policy changes
    temp, plugin = fresh()
    plugin.config["is_global_relation"] = False
    req = ProviderRequest(prompt="hi", system_prompt="persona")
    session_event = FakeEvent()
    asyncio.run(_run(plugin, session_event, req))
    scenarios["session_scope"] = _dump(req)
    # route hidden -> shown with eligibility
    plugin.store.set_state("qq-adapter:user-1", "session", session_event.unified_msg_origin,
                           romance_policy="shown", romance_state="eligible")
    for key, value in {"trust": 600, "comfort": 600, "closeness": 500, "resonance": 500}.items():
        plugin.store.set_dimension("qq-adapter:user-1", "session", session_event.unified_msg_origin, key, value)
    asyncio.run(_run(plugin, session_event, req))
    scenarios["route_shown_eligible"] = _dump(req)
    # effective policy: exclusivity on
    plugin.store.activate_binding_policy({"exclusivity": "scope", "rebind_cooldown": "off"})
    asyncio.run(_run(plugin, session_event, req))
    scenarios["policy_exclusivity_on"] = _dump(req)
    asyncio.run(plugin.terminate()); temp.cleanup()

    for name, payload in scenarios.items():
        (out_dir / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("saved", name)
    return 0


async def _run(plugin, event, req):
    await plugin.inject(event, req)


if __name__ == "__main__":
    raise SystemExit(main())
