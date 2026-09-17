"""MIS-161 real-model A/B comparison runner (TEMPLATE - not executable here).

This machine has no authorized DeepSeek provider credentials, so per the
goal this ships as a runnable script + protocol: the friend (or any
authorized operator) runs it in the isolated environment where the bot's
provider is configured. It never reads keys from source; it uses whatever
provider the isolated AstrBot instance already has configured.

What it measures, per scenario and per side (baseline commit vs candidate
commit of this plugin, SAME provider/model ID/params/preset history):

  - input tokens, output tokens (provider-reported usage)
  - server-side cache-hit / cache-miss tokens WHEN the provider reports
    them (DeepSeek prompt_cache_hit_tokens / prompt_cache_miss_tokens);
    a MISSING field is recorded as null and NEVER as 0%
  - protocol validity rate (exactly one first-line <relation_judgment>
    block, schema 3, parseable by relation_protocol.parse_response)
  - first-line compliance rate, omission rate, duplicate-block rate
  - latency per request; cost only when a reliable price source is given

Protocol rules (to keep the two sides comparable):
  - FIXED preset history: the conversation history is a constant synthetic
    script (same for every request); the model's own outputs are NOT fed
    back, so no divergent random accumulation between sides.
  - Cold-start = fresh request with no prior warm turns on this prefix;
    warm = the same prefix sent K times before the measured call.
  - >= 20 scenarios x 3 repetitions per side (adjust to authorized budget;
    report the actual numbers run - smaller samples are reported honestly).

Usage (in the isolated environment, plugin parent directory):
    # 1) checkout baseline plugin and run:
    git -C astrbot_plugin_relation_arc checkout 913ca59
    python tools/model_ab_test.py --side baseline --out ab_baseline.json
    # 2) checkout candidate and run:
    git -C astrbot_plugin_relation_arc checkout d4d26be
    python tools/model_ab_test.py --side candidate --out ab_candidate.json
    # 3) compare:
    python tools/model_ab_test.py --compare ab_baseline.json ab_candidate.json

The script drives the plugin's inject + a real provider call through the
host's provider layer when run inside the configured AstrBot environment.
If the host provider API is unreachable it exits with a clear error and
writes nothing - never fabricates numbers.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

SCENARIO_SCRIPT = [
    # 20+ synthetic one-shot scenarios: greeting, thanks, teasing, boundary
    # pressure, gift mention, mutual affection, explicit bind request,
    # neutral chat, group-style noise, repeated identical praise (repeat
    # decay surface), ... Each is a fixed user text; the preset history is
    # shared and constant. Extend to taste - keep BOTH sides identical.
    "你好呀",
    "谢谢你昨天帮我",
    "哼，才不是特意来找你",
    "你能不能别开这种玩笑",
    "我给你带了点特产",
    "我们在一起吧",
    "今天天气不错",
    "哈哈哈哈哈",
    "你再夸我一句试试",
    "我觉得你比以前温柔了",
    "帮我看看这个问题",
    "晚安",
    "早上好！",
    "你还记得我上次说的话吗",
    "我有点难过",
    "你最近在忙什么",
    "这个梗好笑吧",
    "别不理我嘛",
    "我们做个好朋友吧",
    "你决定就好",
]
REPEATS = 3


def _null_record(side: str, scenario: str, error: str) -> dict:
    return {"side": side, "scenario": scenario, "error": error,
            "input_tokens": None, "output_tokens": None,
            "cache_hit_tokens": None, "cache_miss_tokens": None,
            "protocol_valid": None, "first_line": None,
            "omitted": None, "duplicate_blocks": None,
            "latency_ms": None, "cost": None}


def run_side(side: str, out_path: Path) -> int:
    try:
        from astrbot.core.provider.provider import Provider  # host layer
    except Exception as exc:  # pragma: no cover - environment guard
        out_path.write_text(json.dumps(
            {"side": side, "ran": False,
             "reason": f"host provider layer unavailable: {exc}"}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"NOT RUN: {exc}. Run inside the configured isolated AstrBot environment.")
        return 2

    from astrbot_plugin_relation_arc import relation_protocol
    from astrbot_plugin_relation_arc.main import RelationArc
    import asyncio

    records = []
    for scenario in SCENARIO_SCRIPT:
        for repetition in range(REPEATS):
            started = time.perf_counter()
            try:
                # The operator wires the real provider + request objects in
                # the isolated environment; this template documents the exact
                # call shape so results stay comparable across sides.
                raise NotImplementedError(
                    "wire Provider.text/chat with the configured provider here; "
                    "inject via RelationArc.inject first (plugin parent on path)")
            except NotImplementedError as exc:
                records.append(_null_record(side, scenario, str(exc)))
                continue
            latency_ms = (time.perf_counter() - started) * 1000
            text = ""  # provider output
            parsed = relation_protocol.parse_response(text, scenario, 10)
            blocks = text.count("<relation_judgment>")
            records.append({
                "side": side, "scenario": scenario, "repetition": repetition,
                "input_tokens": None, "output_tokens": None,  # fill from usage
                "cache_hit_tokens": None, "cache_miss_tokens": None,  # null when absent
                "protocol_valid": parsed.error is None,
                "first_line": text.lstrip().startswith("<relation_judgment>"),
                "omitted": blocks == 0, "duplicate_blocks": blocks > 1,
                "latency_ms": latency_ms, "cost": None,
            })
    out_path.write_text(json.dumps({"side": side, "ran": False,
                                    "reason": "template: wire the provider in the isolated environment",
                                    "protocol": "fixed preset history; no output feedback; null = not reported"},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
    print("Template written. Wire the real provider in the isolated environment before use.")
    return 0


def compare(baseline_path: Path, candidate_path: Path) -> int:
    base = json.loads(baseline_path.read_text(encoding="utf-8"))
    cand = json.loads(candidate_path.read_text(encoding="utf-8"))
    if not base.get("ran") or not cand.get("ran"):
        print("Both sides must actually run before comparing; no fabricated numbers.")
        return 2

    def _summary(records):
        valid = [r for r in records if r.get("protocol_valid") is not None]
        n = len(valid) or 1
        return {
            "protocol_valid_rate": sum(1 for r in valid if r["protocol_valid"]) / n,
            "first_line_rate": sum(1 for r in valid if r["first_line"]) / n,
            "omission_rate": sum(1 for r in valid if r["omitted"]) / n,
            "duplicate_rate": sum(1 for r in valid if r["duplicate_blocks"]) / n,
            "median_latency_ms": statistics.median(r["latency_ms"] for r in valid if r["latency_ms"] is not None) if any(r["latency_ms"] is not None for r in valid) else None,
            "median_input_tokens": statistics.median(r["input_tokens"] for r in valid if r["input_tokens"] is not None) if any(r["input_tokens"] is not None for r in valid) else None,
            "cache_hit_reported_fraction": (sum(1 for r in valid if r["cache_hit_tokens"] is not None) / n),
        }

    report = {"baseline": _summary(base["records"]), "candidate": _summary(cand["records"]),
              "notes": "missing provider cache fields are reported as not-reported, never 0%; "
                       "cost omitted without a reliable price source"}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run")
    run_p.add_argument("--side", choices=["baseline", "candidate"], required=True)
    run_p.add_argument("--out", required=True)
    cmp_p = sub.add_parser("compare")
    cmp_p.add_argument("baseline")
    cmp_p.add_argument("candidate")
    args = parser.parse_args()
    if args.cmd == "run":
        return run_side(args.side, Path(args.out))
    return compare(Path(args.baseline), Path(args.candidate))


if __name__ == "__main__":
    raise SystemExit(main())
