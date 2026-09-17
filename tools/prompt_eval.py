"""MIS-158/159 offline prompt evaluation: baseline vs candidate layout.

Compares the two prompt layouts on IDENTICAL shared inputs — including the
effective exclusivity policy and occupancy boolean (U04: both sides receive
the same binding inputs and produce their occupancy text through their own
binding-context builder, so no side gets an extra clause the other lacks).

Measured per scenario (visibility × exclusivity policy):
- candidate fixed-block hash and stable system-prefix length,
- per-turn temporary chars (dynamic + reminder for candidate; both parts for
  baseline),
- total plugin-owned chars per turn (fixed + dynamic + reminder vs both
  baseline parts) — both blocks counted on both sides,
- chars, NOT tokens; no tokenizer, no cache hit-rate or cost claims.

The candidate layout is read from the live prompts module; the baseline
layout is loaded from the git commit given via --baseline (default 913ca59)
via importlib from a git-show snapshot (no exec).

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


# Shared synthetic inputs (U04: exclusivity_on is a SHARED scenario input).
def _scenarios() -> dict:
    hidden_values = {"trust": 400, "respect": 500, "comfort": 450,
                     "closeness": 150, "resonance": 100, "romance_interest": 0}
    shown_values = {"trust": 850, "respect": 750, "comfort": 850,
                    "closeness": 850, "resonance": 750, "romance_interest": 300}
    hidden_json = json.dumps({"trust": 400, "respect": 500, "comfort": 450,
                              "closeness": 150, "resonance": 100}, ensure_ascii=False)
    shown_json = json.dumps({"trust": 850, "respect": 750, "comfort": 850,
                             "closeness": 850, "resonance": 750}, ensure_ascii=False)
    base = dict(labels=["恋人"], occupied=False, eligible=True, safety="normal")
    return {
        "hidden_exclusivity_off": {**base, "visible": False, "exclusivity_on": False,
                                   "values": hidden_values, "values_json": hidden_json},
        "hidden_exclusivity_on": {**base, "visible": False, "exclusivity_on": True,
                                  "values": hidden_values, "values_json": hidden_json},
        "shown_exclusivity_off": {**base, "visible": True, "exclusivity_on": False,
                                  "values": shown_values, "values_json": shown_json},
        "shown_exclusivity_on": {**base, "visible": True, "exclusivity_on": True,
                                 "values": shown_values, "values_json": shown_json},
    }


def _baseline_turn(module, s) -> dict:
    binding = module.binding_context(s["labels"], s["exclusivity_on"], s["occupied"])
    types_line = module.types_directory_line()
    romance = module.romance_context(s["visible"], s["eligible"],
                                     s["values"].get("romance_interest", 0))
    dynamic = module.dynamic_prompt(s["values_json"], binding, types_line, romance,
                                    s["safety"], s["values"], s["visible"], s["eligible"])
    effect_keys = ("trust,respect,comfort,closeness,resonance,romance_interest"
                   if s["visible"] else "trust,respect,comfort,closeness,resonance")
    contract = module.output_contract(effect_keys)
    return {"system_add": "", "parts": [dynamic, contract]}


def _candidate_turn(s) -> dict:
    binding = candidate_prompts.binding_context(s["labels"], s["exclusivity_on"], s["occupied"])
    romance = candidate_prompts.romance_context(s["visible"], s["eligible"],
                                                s["values"].get("romance_interest", 0))
    dynamic = candidate_prompts.dynamic_prompt(s["values_json"], binding, romance,
                                               s["safety"], s["values"],
                                               s["visible"], s["eligible"])
    reminder = candidate_prompts.short_turn_reminder(s["visible"])
    return {"system_add": candidate_prompts.fixed_rules_block(),
            "parts": [dynamic, reminder]}


def evaluate(repo: Path, baseline_commit: str) -> dict:
    baseline = _load_baseline_prompts(repo, baseline_commit)
    personas = {"empty": "", "persona": "你是一位温柔的助手。"}
    report = {"baseline_commit": baseline_commit, "note_chars_not_tokens": True,
              "shared_inputs": "visibility x exclusivity policy; occupancy=False; "
                               "binding text produced by each side's own builder",
              "personas": {}}
    for pname, persona in personas.items():
        entry = {}
        for sname, s in _scenarios().items():
            b = _baseline_turn(baseline, s)
            c = _candidate_turn(s)
            b_total = sum(len(part) for part in b["parts"])
            c_per_turn = sum(len(part) for part in c["parts"])
            c_total = len(c["system_add"]) + c_per_turn
            entry[sname] = {
                "baseline_per_turn_chars": b_total,
                "baseline_total_chars": b_total,
                "baseline_stable_prefix_chars": len(persona),
                "candidate_system_fixed_chars": len(c["system_add"]),
                "candidate_per_turn_chars": c_per_turn,
                "candidate_total_chars": c_total,
                "candidate_stable_prefix_chars": (len(persona) + 2 + len(c["system_add"])) if persona else len(c["system_add"]),
                "candidate_fixed_sha256": hashlib.sha256(c["system_add"].encode("utf-8")).hexdigest(),
            }
        report["personas"][pname] = entry
    fixed_hashes = {v["candidate_fixed_sha256"]
                    for entry in report["personas"].values() for v in entry.values()}
    report["fixed_block_stable_across_scenarios"] = len(fixed_hashes) == 1
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
