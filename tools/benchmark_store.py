"""MIS-97 storage benchmark: fixed-seed synthetic data, per-variant timings.

Variants:
  old  - per-dimension window scans (the pre-MIS-97 code path, inlined here
         so both variants run against the same data set)
  new  - single window fetch shared across dimensions (the merged path)

The benchmark never touches the settlement policy; it exercises exactly the
window read/aggregate work that settle_turn performs per turn.

Usage: python tools/benchmark_store.py [accounts] [events]
Defaults: 1000 accounts / 100000 events. 10000/1000000 is the documented
large run; pass the sizes explicitly on a machine that can afford it.
"""
from __future__ import annotations

import json
import random
import statistics
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_relation_arc.relation_store import RelationStore
from astrbot_plugin_relation_arc.relation_engine import DEFAULT_VALUES, DIMENSIONS

ACCOUNTS = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
EVENTS = int(sys.argv[2]) if len(sys.argv) > 2 else 100000
WINDOW = 180 * 60
REPEAT_FACTORS = [1.0, 0.6, 0.3, 0.0]
SEED = 20260912


def build_dataset(store: RelationStore) -> None:
    rng = random.Random(SEED)
    conn = sqlite3.connect(store.path)
    now = time.time()
    accounts = []
    for index in range(ACCOUNTS):
        identity = f"qq:bench-{index}"
        values = dict(DEFAULT_VALUES)
        values["trust"] = rng.randrange(0, 1000)
        accounts.append((identity, "global", "", json.dumps(values), 0, 1, now,
                         json.dumps({"romance_policy": "hidden", "romance_state": "hidden",
                                     "interaction_safety": "normal"})))
    conn.executemany("INSERT INTO accounts(identity,scope_kind,scope_id,values_json,paused,revision,updated_at,state_json) VALUES(?,?,?,?,?,?,?,?)", accounts)
    events = []
    for index in range(EVENTS):
        identity = f"qq:bench-{rng.randrange(ACCOUNTS)}"
        applied = {"trust": rng.randrange(-8, 9) or 1}
        requested = dict(applied)
        events.append((f"bench:{index}", identity, "global", "", "private",
                       f"evidence {rng.randrange(100)}", "bench",
                       json.dumps(requested), json.dumps(applied), "{}", "llm",
                       now - rng.randrange(0, WINDOW + 1)))
    conn.executemany("INSERT INTO events(event_id,identity,scope_kind,scope_id,source_kind,evidence,reason,requested_json,applied_json,notes_json,actor,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", events)
    conn.commit()
    conn.close()


def old_variant(conn, identity: str, evidence: str, signature: str, since: float):
    """Per-dimension scans: the pre-MIS-97 shape (2 queries per dimension)."""
    results = {}
    for dimension in DIMENSIONS:
        rows = conn.execute(
            "SELECT applied_json,requested_json,evidence FROM events "
            "WHERE identity=? AND scope_kind=? AND scope_id=? AND created_at>=? AND actor='llm'",
            (identity, "global", "", since)).fetchall()
        count = 0
        for row in rows:
            if dimension not in json.loads(row["applied_json"]):
                continue
            if evidence and evidence in row["evidence"].split(" | "):
                count += 1
            elif not evidence and signature and signature == json.dumps(
                    {k: int(v) for k, v in sorted(json.loads(row["requested_json"]).items()) if v},
                    sort_keys=True):
                count += 1
        results[dimension] = count
    return results


def new_variant(conn, identity: str, evidence: str, signature: str, since: float):
    """Single window fetch shared across dimensions (MIS-97 merged path)."""
    rows = conn.execute(
        "SELECT applied_json,requested_json,evidence FROM events "
        "WHERE identity=? AND scope_kind=? AND scope_id=? AND created_at>=? AND actor='llm'",
        (identity, "global", "", since)).fetchall()
    parsed = [(json.loads(row["applied_json"]), json.loads(row["requested_json"]),
               row["evidence"].split(" | ")) for row in rows]
    results = {}
    for dimension in DIMENSIONS:
        count = 0
        for applied, requested, evidences in parsed:
            if dimension not in applied:
                continue
            if evidence and evidence in evidences:
                count += 1
            elif not evidence and signature and signature == json.dumps(
                    {k: int(v) for k, v in sorted(requested.items()) if v}, sort_keys=True):
                count += 1
        results[dimension] = count
    return results


def equivalence_check(conn, samples: list[tuple[str, str, str]], since: float) -> bool:
    for identity, evidence, signature in samples:
        if old_variant(conn, identity, evidence, signature, since) != \
                new_variant(conn, identity, evidence, signature, since):
            return False
    return True


def benchmark(variant, conn, targets: list[tuple[str, str, str]], since: float, rounds: int = 40) -> dict:
    timings = []
    for index in range(rounds):
        identity, evidence, signature = targets[index % len(targets)]
        start = time.perf_counter()
        variant(conn, identity, evidence, signature, since)
        timings.append((time.perf_counter() - start) * 1000)
    timings.sort()
    return {"p50": round(statistics.median(timings), 2),
            "p95": round(timings[int(len(timings) * 0.95) - 1], 2),
            "max": round(timings[-1], 2)}


def query_plan(conn, identity: str) -> list[str]:
    return [row[-1] for row in conn.execute(
        "EXPLAIN QUERY PLAN SELECT applied_json,requested_json,evidence FROM events "
        "WHERE identity=? AND scope_kind=? AND scope_id=? AND created_at>=? AND actor='llm'",
        (identity, "global", "", time.time() - WINDOW))]


def main() -> None:
    temp = tempfile.TemporaryDirectory()
    store = RelationStore(Path(temp.name) / "data")
    try:
        run_benchmark(store)
    finally:
        store.close()
        temp.cleanup()


def run_benchmark(store: RelationStore) -> None:
    build_start = time.perf_counter()
    build_dataset(store)
    build_seconds = round(time.perf_counter() - build_start, 1)

    rng = random.Random(SEED + 1)
    targets = [(f"qq:bench-{rng.randrange(ACCOUNTS)}", f"evidence {rng.randrange(100)}",
                '{"trust": 2}') for _ in range(8)]
    since = time.time() - WINDOW
    conn = sqlite3.connect(store.path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    same = equivalence_check(conn, targets, since)
    old = benchmark(old_variant, conn, targets, since)
    new = benchmark(new_variant, conn, targets, since)
    plan = query_plan(conn, identity=targets[0][0])
    conn.close()

    print(json.dumps({
        "dataset": {"accounts": ACCOUNTS, "events": EVENTS, "build_seconds": build_seconds},
        "equivalence_old_equals_new": same,
        "window_scan_ms": {"old": old, "new": new},
        "query_plan_without_index": plan,
        "hardware_note": "single run on the development machine, fixed seed",
    }, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
