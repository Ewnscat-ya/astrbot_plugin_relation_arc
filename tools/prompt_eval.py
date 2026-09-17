"""MIS-158/159 offline prompt evaluation: baseline vs candidate layout.

Compares the two prompt layouts on identical synthetic inputs:
- stable prefix length (system prompt after the persona; chars, NOT tokens),
- fixed-block hash stability across turns and personas,
- where the dynamic content starts (the reusable-prefix boundary),
- total prompt overhead per turn (all plugin-owned text, chars).

The candidate layout is read from the live prompts module; the baseline
layout is reconstructed from the git commit given via --baseline (default:
913ca59) using its own prompts module loaded from a git-show snapshot via
importlib (no exec). Char counts are a proxy only — no tokenizer, no cache
hit-rate claims.

Usage (from the plugin parent directory, host venv python):
    python tools/prompt_eval.py --out tools/prompt_samples/eval_report.json
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_relation_arc import prompts as candidate_prompts  # noqa: E402
from astrbot_plugin_relation_arc.relation_engine import behavior_projection  # noqa: E402


def _load_baseline_prompts(repo: Path, commit: str):
    source = subprocess.check_output(
        ["git", "-C", str(repo), "show", f"{commit}:prompts.py"], encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="prompt_eval_") as tmp:
        module_file = Path(tmp) / "baseline_prompts.py"
        module_file.write_text(source, encoding="utf-8")
        package = types.ModuleType("prompt_eval_baseline")
        package.__path__ = [str(ROOT)]
        sys.modules.setdefault(package.__name__, package)
        spec = importlib.util.spec_from_file_location(
            package.__name__ + ".baseline_prompts", module_file)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


def _scenario_inputs():
    """Synthetic per-turn inputs spanning the matrix the goal requires."""
    public_values = json.dumps({"trust": 400, "respect": 500, "comfort": 450,
                                "closeness": 150, "resonance": 100}, ensure_ascii=False)
    public_values_shown = json.dumps({"trust": 850, "respect": 750, "comfort": 850,
                                      "closeness": 850, "resonance": 750}, ensure_ascii=False)
    hidden_values = {"trust": 400, "respect": 500, "comfort": 450,
                     "closeness": 150, "resonance": 100, "romance_interest": 0}
    shown_values = {"trust": 850, "respect": 750, "comfort": 850,
                    "closeness": 850, "resonance": 750, "romance_interest": 300}
    return {
        "hidden_turn": dict(values_json=public_values, binding="当前正式关系：无。",
                            types_hidden=True, visible=False, eligible=False,
                            safety="normal", values=hidden_values),
        "shown_turn": dict(values_json=public_values_shown,
                           binding="当前正式关系：恋人。",
                           types_hidden=False, visible=True, eligible=True,
                           safety="normal", values=shown_values),
    }


def _baseline_turn(module, inputs) -> dict:
    """Rebuild the baseline (913ca59) two-part per-turn layout."""
    types_line = module.types_directory_line()
    romance = module.romance_context(inputs["visible"], inputs["eligible"],
                                      inputs["values"].get("romance_interest", 0))
    dynamic = module.dynamic_prompt(inputs["values_json"], inputs["binding"], types_line,
                                    romance, inputs["safety"], inputs["values"],
                                    inputs["visible"], inputs["eligible"])
    effect_keys = ("trust,respect,comfort,closeness,resonance,romance_interest"
                   if inputs["visible"] else "trust,respect,comfort,closeness,resonance")
    contract = module.output_contract(effect_keys)
    return {"system_add": "", "parts": [dynamic, contract]}


def _candidate_turn(inputs) -> dict:
    """The MIS-158 layout: fixed block in system + dynamic + reminder temp."""
    fixed = candidate_prompts.fixed_rules_block()
    binding = inputs["binding"] + ("同 scope 恋爱位置已被他人占用：false。" if inputs["visible"] else "")
    romance = candidate_prompts.romance_context(inputs["visible"], inputs["eligible"],
                                                 inputs["values"].get("romance_interest", 0))
    dynamic = candidate_prompts.dynamic_prompt(inputs["values_json"], binding, romance,
                                               inputs["safety"], inputs["values"],
                                               inputs["visible"], inputs["eligible"])
    reminder = candidate_prompts.short_turn_reminder(inputs["visible"])
    return {"system_add": fixed, "parts": [dynamic, reminder]}


def evaluate(repo: Path, baseline_commit: str) -> dict:
    baseline = _load_baseline_prompts(repo, baseline_commit)
    personas = {"empty": "", "persona": "你是一位温柔的助手。"}
    scenarios = _scenario_inputs()
    report = {"baseline_commit": baseline_commit, "note_chars_not_tokens": True,
              "personas": {}, "scenarios": {}}
    for pname, persona in personas.items():
        entry = {}
        for sname, inputs in scenarios.items():
            b = _baseline_turn(baseline, inputs)
            c = _candidate_turn(inputs)
            b_total = len(b["parts"][0]) + len(b["parts"][1])
            c_total = len(c["parts"][0]) + len(c["parts"][1]) + len(c["system_add"])
            entry[sname] = {
                "baseline_per_turn_chars": b_total,
                "baseline_stable_prefix_chars": len(persona),  # everything was per-turn
                "candidate_system_fixed_chars": len(c["system_add"]),
                "candidate_per_turn_chars": c_total - len(c["system_add"]),
                "candidate_total_chars": c_total,
                "candidate_stable_prefix_chars": len(persona) + 2 + len(c["system_add"]) if persona else len(c["system_add"]),
                "candidate_fixed_sha256": hashlib.sha256(c["system_add"].encode("utf-8")).hexdigest(),
            }
        report["personas"][pname] = entry
    report["fixed_block_stable_across_scenarios"] = (
        report["personas"]["persona"]["hidden_turn"]["candidate_fixed_sha256"]
        == report["personas"]["persona"]["shown_turn"]["candidate_fixed_sha256"])
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="913ca59")
    parser.add_argument("--out", default=str(ROOT / "tools" / "prompt_samples" / "eval_report.json"))
    args = parser.parse_args()
    report = evaluate(ROOT, args.baseline)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
