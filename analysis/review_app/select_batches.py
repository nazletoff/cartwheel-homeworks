"""Pick review batches for Homework 4 and write them to sample_manifest.json.

Batch 1 (the handout's first batch): 15 cluster representatives plus 15
uniform random picks over the 250 final-run conversations. Clusters come from
the course's tiny k-means (``analysis/helpers/selection``) on structural
features of each conversation, so the representatives cover different shapes
of run (short lookups, long tool loops, escalations, refusals); the random
picks cover whatever the features miss. Deterministic: fixed seeds.

Later batches (a product dimension, depth searches, the final 15) get their
own subcommands as the review reaches them.

    uv run python -m analysis.review_app.select_batches batch1 [--dry-run]
"""

from __future__ import annotations

import argparse
import math
import random
from collections import Counter
from typing import Any

from analysis.helpers import _state, selection
from analysis.review_app import loader
from analysis.review_app.state import StateStore

RETRIEVAL_TOOLS = {"search_help_center", "get_policy"}
WRITE_TOOLS = {"issue_refund", "cancel_order"}


def features(c: dict[str, Any]) -> list[float]:
    calls = [call for t in c["turns"] for s in t["steps"] for call in s["tool_calls"]]
    names = [call["name"] for call in calls]
    reply_chars = sum(len((t["reply"]["text"] or "")) for t in c["turns"])
    return [
        float(c["turn_count"]),
        float(len(calls)),
        float(len(set(names))),
        1.0 if RETRIEVAL_TOOLS & set(names) else 0.0,
        float(c.get("total_duration_s") or 0.0),
        float(reply_chars),
        1.0 if "escalate_to_human" in names else 0.0,
        1.0 if WRITE_TOOLS & set(names) else 0.0,
        1.0 if any(call["ok"] is False for call in calls) else 0.0,
        1.0 if c["role"] == "shopper" else 0.0,
        1.0 if c["role"] == "merchant" else 0.0,
        1.0 if c["role"] == "support" else 0.0,
    ]


def describe(c: dict[str, Any]) -> str:
    calls = [call for t in c["turns"] for s in t["steps"] for call in s["tool_calls"]]
    names = [call["name"] for call in calls]
    bits = [f"{c['turn_count']} turn" + ("s" if c["turn_count"] != 1 else ""), f"{len(calls)} tool calls"]
    if "escalate_to_human" in names:
        bits.append("escalated")
    if WRITE_TOOLS & set(names):
        bits.append("wrote")
    if any(call["ok"] is False for call in calls):
        bits.append("tool error")
    return ", ".join(bits)


def batch1(convs: list[dict[str, Any]], k: int = 8, n_reps: int = 15, n_random: int = 15, seed: int = 20260920):
    final = [c for c in convs if c["source"] == "final"]
    vecs = selection._standardize([features(c) for c in final])
    clusters = selection._kmeans(vecs, k=k, seed=7)
    members: dict[int, list[tuple[float, int]]] = {}
    dims = len(vecs[0])
    for cid in set(clusters):
        idx = [i for i, cl in enumerate(clusters) if cl == cid]
        centroid = [sum(vecs[i][d] for i in idx) / len(idx) for d in range(dims)]
        ranked = sorted(
            ((math.sqrt(sum((vecs[i][d] - centroid[d]) ** 2 for d in range(dims))), i) for i in idx),
            key=lambda pair: pair[0],
        )
        members[cid] = ranked
    sizes = Counter(clusters)
    order = sorted(members, key=lambda cid: -sizes[cid])
    reps: list[tuple[int, int]] = []
    depth = 0
    while len(reps) < n_reps and depth < max(len(m) for m in members.values()):
        for cid in order:
            if len(reps) >= n_reps:
                break
            if depth < len(members[cid]):
                reps.append((cid, members[cid][depth][1]))
        depth += 1
    taken = {i for _, i in reps}
    rng = random.Random(seed)
    pool = [i for i in range(len(final)) if i not in taken]
    randoms = rng.sample(pool, n_random)
    items = []
    for cid, i in reps:
        c = final[i]
        items.append(
            {
                "session_id": c["session_id"],
                "reason": f"cluster {cid} representative (cluster of {sizes[cid]}): {describe(c)}",
            }
        )
    for i in randoms:
        c = final[i]
        items.append({"session_id": c["session_id"], "reason": f"uniform random pick: {describe(c)}"})
    return final, items, clusters


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("which", choices=["batch1"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    convs = loader.load_conversations("cache")
    by_sid = {c["session_id"]: c for c in convs}
    if args.which == "batch1":
        final, items, clusters = batch1(convs)
        print(f"final conversations {len(final)}, clusters {dict(sorted(Counter(clusters).items()))}")
        picked = [by_sid[i["session_id"]] for i in items]
        print(f"picked {len(items)}: roles {dict(Counter(c['role'] for c in picked))}, "
              f"groups {dict(Counter(c['scenario_group'] for c in picked))}, "
              f"intents {dict(Counter(((c.get('answer_key') or {}).get('tuple') or {}).get('intent') for c in picked))}, "
              f"traces {sum(c['turn_count'] for c in picked)}")
        for it in items:
            c = by_sid[it["session_id"]]
            print(f"  {c['scenario_id']:13} {c['role']:8} {c['scenario_group'] or '-':9} {it['reason']}")
        if not args.dry_run:
            store = StateStore(_state.state_root(), convs)
            man = store.add_batch("batch1", "uniform+cluster", items)
            print(f"written {_state.state_root() / 'sample_manifest.json'}: batches {[b['name'] for b in man['batches']]}, picks {man['k']}")


if __name__ == "__main__":
    main()
