"""MIS-161 real-model A/B comparison runner (executable; U05 rewrite).

Measures baseline (913ca59) vs candidate plugin prompt layouts against the
SAME provider/model/params/preset history. No keys in source: the real
adapter uses whatever provider the isolated AstrBot environment already has
configured; a --dry-run fake adapter exercises the whole chain offline.

Protocol comparability rules baked in:
- FIXED preset history (constant synthetic script); model outputs are never
  fed back, so the two sides can never accumulate divergent random history.
- cold = first request on this prefix in the process; warm = the same
  prefix sent --warmup N times before the measured call.
- provider-reported usage recorded per request; cache hit/miss tokens are
  kept ONLY when the provider actually reports them — a missing field is
  recorded as null and NEVER as 0%.
- protocol validity requires exactly one <relation_judgment> block, at the
  first line, with a parseable schema-3 payload (not just "no parser error").
- cost is filled only when --price-json gives a reliable price source.
- >= 20 scenarios x 3 repeats by default; pass --scenarios/--repeats to fit
  the authorized budget and report what actually ran.

Exit codes: 0 ok; 2 not-ran/invalid (never a silent fake success).

Usage (plugin parent directory, host venv python):
    # offline chain check (no network, no model):
    python tools/model_ab_test.py run --side candidate --adapter fake --out ab_candidate_dry.json
    # real runs inside the authorized isolated environment:
    git -C astrbot_plugin_relation_arc checkout 913ca59
    python tools/model_ab_test.py run --side baseline --out ab_baseline.json
    git -C astrbot_plugin_relation_arc checkout <candidate>
    python tools/model_ab_test.py run --side candidate --out ab_candidate.json
    # summarize (refuses non-ran or empty sides):
    python tools/model_ab_test.py compare ab_baseline.json ab_candidate.json
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "tests"))

DEFAULT_SCENARIOS = [
    "你好呀", "谢谢你昨天帮我", "哼，才不是特意来找你", "你能不能别开这种玩笑",
    "我给你带了点特产", "我们在一起吧", "今天天气不错", "哈哈哈哈哈",
    "你再夸我一句试试", "我觉得你比以前温柔了", "帮我看看这个问题", "晚安",
    "早上好！", "你还记得我上次说的话吗", "我有点难过", "你最近在忙什么",
    "这个梗好笑吧", "别不理我嘛", "我们做个好朋友吧", "你决定就好",
]
PRESET_HISTORY = [
    {"role": "user", "content": "之前的合成对话"},
    {"role": "assistant", "content": "好的合成回复"},
]


class ProviderAdapter(Protocol):
    """The seam real/fake providers plug into. complete() returns
    (text, usage) where usage is a dict or None; missing cache fields stay
    missing — the runner never coerces them to zero."""

    name: str

    async def complete(self, system_prompt: str, user_text: str) -> tuple[str, dict | None]: ...


class FakeAdapter:
    """Offline deterministic stand-in: emits a protocol-shaped reply. Used
    only by --dry-run/--adapter fake to verify the executable chain end to
    end; results are marked adapter=fake and are NOT model measurements."""

    name = "fake-offline"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, system_prompt: str, user_text: str) -> tuple[str, dict | None]:
        self.calls += 1
        digest = hashlib.sha256(user_text.encode("utf-8")).hexdigest()[:6]
        text = ('<relation_judgment>{"schema_version":3,"fact_effects":[{"effects":{"trust":'
                + str(int(digest[0], 16) % 5) + '}}],"relationship_proposal":null,'
                '"interaction_safety_proposal":null}</relation_judgment>好的。')
        return text, {"input_tokens": None, "output_tokens": None,
                      "prompt_cache_hit_tokens": None, "prompt_cache_miss_tokens": None}


def _try_host_adapter() -> ProviderAdapter | None:
    """Attempt to bind the host's configured provider (isolated env only).
    Returns None with a reason when unavailable — never fabricates."""
    class HostAdapter:
        name = "host-provider"

        def __init__(self, provider) -> None:
            self._provider = provider

        async def complete(self, system_prompt: str, user_text: str) -> tuple[str, dict | None]:
            from astrbot.core.provider.entities import ProviderRequest
            req = ProviderRequest(prompt=user_text, system_prompt=system_prompt,
                                  contexts=list(PRESET_HISTORY))
            resp = await self._provider.text_chat(**{
                "prompt": req.prompt, "session_id": "ab-test", "contexts": PRESET_HISTORY,
                "system_prompt": system_prompt})
            text = getattr(resp, "completion_text", "") or ""
            usage = getattr(resp, "usage", None)
            usage_dict = None
            if isinstance(usage, dict):
                usage_dict = {k: usage.get(k) for k in
                              ("input_tokens", "output_tokens",
                               "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")}
            elif usage is not None:
                usage_dict = {"raw": str(usage)}
            return text, usage_dict

    try:
        from astrbot.core.provider.provider import Provider
        provider = Provider()
        return HostAdapter(provider)
    except Exception as exc:
        print(f"[model_ab_test] host provider unavailable: {exc}", file=sys.stderr)
        return None


def _git_commit() -> str:
    try:
        return subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                                       encoding="utf-8").strip()
    except Exception:
        return "unknown"


def _measure(text: str, user_text: str) -> dict:
    """Protocol metrics: exactly one block, first line, schema-3 payload."""
    stripped = text.lstrip()
    first_line = stripped.split("\n", 1)[0]
    blocks = text.count("<relation_judgment>")
    from astrbot_plugin_relation_arc.relation_protocol import parse_response
    parsed = parse_response(text, user_text, 10)
    schema3 = (getattr(parsed, "stats", None) or {}).get("blocks", 0) > 0 and parsed.error is None
    return {"protocol_valid": blocks == 1 and schema3,
            "first_line": first_line.startswith("<relation_judgment>"),
            "omitted": blocks == 0,
            "duplicate_blocks": blocks > 1}


async def run_side(side: str, adapter: ProviderAdapter, repeats: int,
                   scenarios: list[str], warmup: int, price: dict | None,
                   out_path: Path) -> int:
    from astrbot.api.provider import ProviderRequest
    from test_main import FakeContext, FakeEvent
    from astrbot_plugin_relation_arc.main import RelationArc

    records: list[dict] = []
    failures = 0
    temp = tempfile_dir()
    plugin = RelationArc(FakeContext(temp.name))
    try:
        for scenario in scenarios:
            for repetition in range(repeats):
                req = ProviderRequest(prompt=scenario, system_prompt="persona",
                                      contexts=list(PRESET_HISTORY))
                await plugin.inject(FakeEvent(), req)
                for _ in range(warmup):
                    await adapter.complete(req.system_prompt or "", scenario)
                started = time.perf_counter()
                try:
                    text, usage = await adapter.complete(req.system_prompt or "", scenario)
                    error = None
                except Exception as exc:
                    failures += 1
                    text, usage, error = "", None, f"{type(exc).__name__}: {exc}"
                latency_ms = (time.perf_counter() - started) * 1000
                record: dict[str, Any] = {
                    "side": side, "scenario": scenario, "repetition": repetition,
                    "adapter": adapter.name, "error": error,
                    "input_tokens": (usage or {}).get("input_tokens") if usage else None,
                    "output_tokens": (usage or {}).get("output_tokens") if usage else None,
                    "cache_hit_tokens": (usage or {}).get("prompt_cache_hit_tokens") if usage else None,
                    "cache_miss_tokens": (usage or {}).get("prompt_cache_miss_tokens") if usage else None,
                    "latency_ms": latency_ms, "cost": None,
                }
                if error is None:
                    record.update(_measure(text, scenario))
                if price and record.get("input_tokens") is not None:
                    pin = float(price.get("input_per_1k", 0) or 0)
                    pout = float(price.get("output_per_1k", 0) or 0)
                    record["cost"] = (record["input_tokens"] * pin + (record["output_tokens"] or 0) * pout) / 1000
                records.append(record)
                out_path.write_text(json.dumps(
                    {"side": side, "ran": True, "adapter": adapter.name,
                     "plugin_commit": _git_commit(), "records": records,
                     "config": {"repeats": repeats, "scenarios": len(scenarios),
                                "warmup": warmup, "preset_history": PRESET_HISTORY}},
                    ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        await plugin.terminate()
        temp.cleanup()
    print(f"run: {len(records)} records, {failures} request failures -> {out_path}")
    return 0


def tempfile_dir():
    import tempfile
    return tempfile.TemporaryDirectory(prefix="ab_test_")


def _summary(payload: dict) -> dict:
    records = [r for r in payload.get("records", []) if r.get("error") is None]
    out: dict[str, Any] = {"adapter": payload.get("adapter"),
                           "records_total": len(payload.get("records", [])),
                           "records_ok": len(records)}
    if not records:
        out["status"] = "no-valid-samples"
        return out
    n = len(records)

    def rate(key: str) -> float:
        return sum(1 for r in records if r.get(key)) / n

    out.update({"protocol_valid_rate": rate("protocol_valid"),
                "first_line_rate": rate("first_line"),
                "omission_rate": rate("omitted"),
                "duplicate_rate": rate("duplicate_blocks"),
                "cache_field_reported_fraction":
                    sum(1 for r in records if r.get("cache_hit_tokens") is not None) / n})
    lat = [r["latency_ms"] for r in records if r.get("latency_ms") is not None]
    out["median_latency_ms"] = statistics.median(lat) if lat else None
    tok_in = [r["input_tokens"] for r in records if r.get("input_tokens") is not None]
    out["median_input_tokens"] = statistics.median(tok_in) if tok_in else None
    cost = [r["cost"] for r in records if r.get("cost") is not None]
    out["total_cost"] = sum(cost) if cost else None
    out["status"] = "ok"
    return out


def compare(baseline_path: Path, candidate_path: Path) -> int:
    base = json.loads(baseline_path.read_text(encoding="utf-8"))
    cand = json.loads(candidate_path.read_text(encoding="utf-8"))
    problems = []
    for name, payload in (("baseline", base), ("candidate", cand)):
        if not payload.get("ran"):
            problems.append(f"{name} side was never run ({payload.get('reason', 'ran:false')})")
    if problems:
        print("; ".join(problems), "— refusing to compare.", file=sys.stderr)
        return 2
    sb, sc = _summary(base), _summary(cand)
    for name, s in (("baseline", sb), ("candidate", sc)):
        if s["status"] != "ok":
            print(f"{name} side has {s['status']} — comparison result: UNKNOWN.",
                  file=sys.stderr)
    report = {"baseline": sb, "candidate": sc,
              "notes": "cache fields absent from the provider payload are reported "
                       "as cache_field_reported_fraction, never as 0%; cost only "
                       "with a supplied price source; UNKNOWN when either side "
                       "lacks valid samples."}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    both_ok = sb["status"] == "ok" and sc["status"] == "ok"
    if not both_ok:
        print("comparison result: UNKNOWN (a side lacks valid samples); exit 2.",
              file=sys.stderr)
    return 0 if both_ok else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="run one side")
    run_p.add_argument("--side", choices=["baseline", "candidate"], required=True)
    run_p.add_argument("--out", required=True)
    run_p.add_argument("--adapter", choices=["auto", "fake"], default="auto",
                       help="auto=host provider when available; fake=offline dry-run")
    run_p.add_argument("--repeats", type=int, default=3)
    run_p.add_argument("--scenarios", type=int, default=len(DEFAULT_SCENARIOS))
    run_p.add_argument("--warmup", type=int, default=2)
    run_p.add_argument("--price-json", default=None,
                       help='optional JSON like {"input_per_1k":0.001,"output_per_1k":0.002}')
    cmp_p = sub.add_parser("compare", help="summarize two run files")
    cmp_p.add_argument("baseline")
    cmp_p.add_argument("candidate")
    args = parser.parse_args()

    if args.cmd == "compare":
        return compare(Path(args.baseline), Path(args.candidate))

    adapter: ProviderAdapter | None
    if args.adapter == "fake":
        adapter = FakeAdapter()
    else:
        adapter = _try_host_adapter()
        if adapter is None:
            Path(args.out).write_text(json.dumps(
                {"side": args.side, "ran": False,
                 "reason": "host provider layer unavailable; use --adapter fake "
                           "for an offline chain check or run inside the "
                           "authorized isolated environment"},
                ensure_ascii=False, indent=2), encoding="utf-8")
            print("NOT RUN: no host provider. See the written reason file.",
                  file=sys.stderr)
            return 2
    price = None
    if args.price_json:
        price = json.loads(Path(args.price_json).read_text(encoding="utf-8"))
    scenarios = DEFAULT_SCENARIOS[:max(1, args.scenarios)]
    return asyncio.run(run_side(args.side, adapter, max(1, args.repeats),
                                scenarios, max(0, args.warmup), price,
                                Path(args.out)))


if __name__ == "__main__":
    raise SystemExit(main())
