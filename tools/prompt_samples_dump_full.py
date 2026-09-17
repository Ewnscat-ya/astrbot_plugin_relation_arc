"""MIS-158/159/U03 full-request evidence: complete request snapshots with the
REAL host assembly and history-persistence chain (not just prompt fragments).

Per scenario this records, for BOTH sides (baseline 913ca59 vs candidate):
- inputs: user prompt, preset history contexts, data-URI image, pre-existing
  other-plugin parts, persona/system, scenario + effective-policy state,
  plugin commit and host version;
- post-inject: final system_prompt and extra_user_content_parts;
- REAL host assembly: ``await req.assemble_context()`` — the same method the
  agent runner uses to build the provider user message;
- history persistence: ``dump_messages_with_checkpoints([Message.model_validate(
  assembled)])`` — the same filter the host applies before saving, proving the
  temporary dynamic block and reminder never persist while the user text,
  other-plugin part and image block survive.

Scenarios match the fragment matrix: empty system / persona + repeat inject /
other-plugin parts / disable+reenable / session scope / route shown / policy
exclusivity on. Run with the host venv python; the image is a synthetic 1x1
data URI (no network).

Usage: python tools/prompt_samples_dump_full.py [--out tools/prompt_samples]
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "tests"))

from astrbot.api.provider import ProviderRequest  # noqa: E402
from astrbot.core.agent.message import Message, TextPart  # noqa: E402
from astrbot.core.agent import message as message_mod  # noqa: E402

from test_main import FakeContext, FakeEvent  # noqa: E402
from astrbot_plugin_relation_arc.main import RelationArc  # noqa: E402

SYNTH_IMAGE = ("data:image/png;base64,"
               "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg"
               "YGBgAAAABQABh6FO1AAAAABJRU5ErkJggg==")  # 1x1 synthetic PNG
PRESET_HISTORY = [
    {"role": "user", "content": "之前的合成对话"},
    {"role": "assistant", "content": "好的合成回复"},
]


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], encoding="utf-8").strip()
    except Exception:
        return "unknown"


def _host_version() -> str:
    try:
        return importlib.metadata.version("astrbot")
    except Exception:
        return "unknown"


def _policy_state(plugin) -> dict:
    p = plugin.store.active_binding_policy()
    return {"exclusivity": p["exclusivity"], "rebind_cooldown": p["rebind_cooldown"],
            "epoch": p["epoch"]}


async def _snapshot(plugin, req, scenario: str) -> dict:
    assembled = await req.assemble_context()
    message = Message.model_validate(assembled)
    persisted = message_mod.dump_messages_with_checkpoints([message])
    return {
        "scenario": scenario,
        "system_prompt": req.system_prompt,
        "extra_user_content_parts": [
            {"text": part.text, "temp": bool(getattr(part, "_no_save", False))}
            for part in req.extra_user_content_parts],
        "assembled_user_message": assembled,
        "history_persisted": persisted,
    }


async def _run_scenario(name: str, build) -> dict:
    temp = tempfile.TemporaryDirectory()
    plugin = RelationArc(FakeContext(temp.name))
    try:
        record = await build(plugin)
        record["plugin_commit"] = _git_commit()
        record["host_version"] = _host_version()
        record["effective_policy"] = _policy_state(plugin)
        return record
    finally:
        await plugin.terminate()
        temp.cleanup()


def _new_req(system_prompt: str = "persona") -> ProviderRequest:
    req = ProviderRequest(prompt="用户原文", system_prompt=system_prompt,
                          contexts=list(PRESET_HISTORY))
    req.image_urls.append(SYNTH_IMAGE)
    return req


async def scenario_empty_system(plugin):
    req = _new_req(system_prompt="")
    await plugin.inject(FakeEvent(), req)
    return await _snapshot(plugin, req, "empty_system")


async def scenario_persona_repeat(plugin):
    req = _new_req()
    await plugin.inject(FakeEvent(), req)
    first = await _snapshot(plugin, req, "persona_repeat_inject:first")
    plugin.store.set_dimension("qq-adapter:user-1", "global", "", "trust", 432)
    await plugin.inject(FakeEvent(), req)
    second = await _snapshot(plugin, req, "persona_repeat_inject:second")
    return {"first": first, "second": second}


async def scenario_other_plugin(plugin):
    req = _new_req(system_prompt="persona\n<OtherPlugin>x</OtherPlugin>")
    req.extra_user_content_parts.append(TextPart(text="<Other>keep</Other>"))
    await plugin.inject(FakeEvent(), req)
    await plugin.inject(FakeEvent(), req)
    return await _snapshot(plugin, req, "other_plugin_parts")


async def scenario_disable_reenable(plugin):
    req = _new_req()
    await plugin.inject(FakeEvent(), req)
    plugin.config["llm_judgment_enabled"] = False
    await plugin.inject(FakeEvent(), req)
    disabled = await _snapshot(plugin, req, "disable_reenable:disabled")
    plugin.config["llm_judgment_enabled"] = True
    await plugin.inject(FakeEvent(), req)
    reenabled = await _snapshot(plugin, req, "disable_reenable:reenabled")
    return {"disabled": disabled, "reenabled": reenabled}


async def scenario_session(plugin):
    plugin.config["is_global_relation"] = False
    req = _new_req()
    event = FakeEvent()
    await plugin.inject(event, req)
    return await _snapshot(plugin, req, "session_scope")


async def scenario_route_shown(plugin):
    plugin.config["is_global_relation"] = False
    req = _new_req()
    event = FakeEvent()
    await plugin.inject(event, req)
    scope = ("session", event.unified_msg_origin)
    plugin.store.set_state("qq-adapter:user-1", *scope,
                           romance_policy="shown", romance_state="eligible")
    for key, value in {"trust": 600, "comfort": 600, "closeness": 500,
                       "resonance": 500}.items():
        plugin.store.set_dimension("qq-adapter:user-1", *scope, key, value)
    await plugin.inject(event, req)
    return await _snapshot(plugin, req, "route_shown_eligible")


async def scenario_policy_exclusivity(plugin):
    plugin.config["is_global_relation"] = False
    req = _new_req()
    event = FakeEvent()
    await plugin.inject(event, req)
    plugin.store.activate_binding_policy({"exclusivity": "scope", "rebind_cooldown": "off"})
    await plugin.inject(event, req)
    return await _snapshot(plugin, req, "policy_exclusivity_on")


SCENARIOS = {
    "full_request_empty_system": scenario_empty_system,
    "full_request_persona_repeat_inject": scenario_persona_repeat,
    "full_request_other_plugin_parts": scenario_other_plugin,
    "full_request_disable_reenable": scenario_disable_reenable,
    "full_request_session_scope": scenario_session,
    "full_request_route_shown_eligible": scenario_route_shown,
    "full_request_policy_exclusivity_on": scenario_policy_exclusivity,
}


async def _run_all(out_dir: Path) -> None:
    for name, build in SCENARIOS.items():
        record = await _run_scenario(name, build)
        (out_dir / f"{name}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print("saved", name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "tools" / "prompt_samples"))
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(_run_all(out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
