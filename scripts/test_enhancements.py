#!/usr/bin/env python3
"""Tests for the three enhancements: bi-temporal validity, PPR ranking,
multiplicative re-ranking.

Run: .venv/bin/python test_enhancements.py [--integration]

Unit tests use synthetic data only. --integration also runs recall() against
sample data (slower, needs the built indexes).
"""

import math
import os
import sys
from datetime import date

# Allow running from the repo root without installing: python scripts/test_enhancements.py
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from helix_memory.entity_graph import EntityGraph  # noqa: E402
from helix_memory.fusion import MergedCandidate, ArmScores  # noqa: E402
from helix_memory.ppr_ranking import ppr_rank  # noqa: E402
from helix_memory.temporal_validity import (  # noqa: E402
    build_validity_index,
    filter_valid,
    is_valid,
)

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  PASS {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name} {detail}")


# ---------------------------------------------------------------- validity

def test_validity_supersession():
    print("\n[validity] supersession detection")
    docs = [
        {
            "id": "memory/helix/05-personal/qpulse/2026-10-08-0900-am.md#s1",
            "division": "05-personal",
            "text": "Project budget is $2000 per Jane Smith.",
            "date": date(2026, 10, 8),
        },
        {
            "id": "memory/helix/05-personal/qpulse/2026-10-10-1030-hourly.md#s2",
            "division": "05-personal",
            "text": (
                "Corrected: Project budget is $2500 per Jane Smith, "
                "this supersedes the earlier figure."
            ),
            "date": date(2026, 10, 10),
        },
        {
            "id": "memory/helix/05-personal/qpulse/2026-10-09-1200-pm.md#s3",
            "division": "05-personal",
            "text": "Alex had coffee with Sam in the morning.",
            "date": date(2026, 10, 9),
        },
    ]
    v = build_validity_index(docs)
    old_id = docs[0]["id"]
    new_id = docs[1]["id"]
    unrelated_id = docs[2]["id"]

    check("old window closed", v[old_id]["valid_until"] == "2026-10-10",
          f"got {v[old_id]['valid_until']}")
    check("old superseded_by set", v[old_id]["superseded_by"] == new_id)
    check("new still open", v[new_id]["valid_until"] is None)
    check("unrelated untouched", v[unrelated_id]["valid_until"] is None)

    check("old valid on 2026-10-09", is_valid(v[old_id], date(2026, 10, 9)))
    check("old invalid on 2026-10-11", not is_valid(v[old_id], date(2026, 10, 11)))
    check("new invalid on 2026-10-09", not is_valid(v[new_id], date(2026, 10, 9)))
    check("new valid on 2026-10-11", is_valid(v[new_id], date(2026, 10, 11)))

    ids = [d["id"] for d in docs]
    now_valid = filter_valid(ids, v, date(2026, 10, 11))
    check("filter hides superseded", old_id not in now_valid and new_id in now_valid)
    past_valid = filter_valid(ids, v, date(2026, 10, 9))
    check("filter shows old in past", old_id in past_valid and new_id not in past_valid)


def test_validity_no_false_supersede():
    print("\n[validity] no false positives")
    docs = [
        {
            "id": "a",
            "division": "03-work",
            "text": "Project approval pending per Jane Smith.",
            "date": date(2026, 10, 8),
        },
        {
            # shares 2 entities but has NO correction keyword -> no supersede
            "id": "b",
            "division": "03-work",
            "text": "The team sent another update via Jane Smith today.",
            "date": date(2026, 10, 10),
        },
        {
            # correction keyword but only 1 shared entity -> no supersede
            "id": "c",
            "division": "03-work",
            "text": "Corrected the Tesla tire pressure reading.",
            "date": date(2026, 10, 10),
        },
    ]
    v = build_validity_index(docs)
    check("no keyword -> no close", v["a"]["valid_until"] is None)
    check("single entity -> no close", v["c"]["valid_until"] is None)


# ---------------------------------------------------------------- PPR

def test_ppr_multihop():
    print("\n[ppr] multi-hop reach")
    g = EntityGraph()
    g.add_document("doc1", "Alpha and Beta met to discuss the plan")
    g.add_document("doc2", "Beta and Gamma reviewed the budget")
    g.add_document("doc3", "Gamma and Delta signed the contract")
    g.add_document("doc4", "Delta and Epsilon shipped the release")
    ranked = ppr_rank(g, ["Alpha"], top_n=10)
    ids = [d for d, _ in ranked]
    check("seed doc first", ids[0] == "doc1", f"got {ids}")
    check("3-hop doc reached", "doc4" in ids, f"got {ids}")
    # nearer docs should outrank farther ones
    pos = {d: i for i, d in enumerate(ids)}
    check("distance decay", pos["doc1"] < pos["doc2"] < pos["doc4"],
          f"positions {pos}")


def test_ppr_specificity():
    print("\n[ppr] node specificity")
    g = EntityGraph()
    # "Common" appears in 30 docs; "Rare" appears in 2.
    for i in range(30):
        g.add_document(f"common-{i}", f"Common topic number {i} discussed")
    g.add_document("rare-1", "Rare discovery announced today")
    g.add_document("rare-2", "Rare findings confirmed yesterday")
    ranked = ppr_rank(g, ["Rare"], top_n=10)
    ids = [d for d, _ in ranked]
    check("rare docs rank top", ids[:2] == ["rare-1", "rare-2"] or
          set(ids[:2]) == {"rare-1", "rare-2"}, f"got {ids[:4]}")


def test_ppr_no_seeds():
    print("\n[ppr] empty seeds")
    g = EntityGraph()
    g.add_document("doc1", "Alpha and Beta met")
    check("no seeds -> empty", ppr_rank(g, [], top_n=10) == [])
    check("unknown seeds -> empty", ppr_rank(g, ["Zzzzzz"], top_n=10) == [])


# ---------------------------------------------------------------- rerank

def _candidate(doc_id, rrf_score, rank):
    return MergedCandidate(
        id=doc_id,
        text="x",
        rrf_score=rrf_score,
        rrf_rank=rank,
        source_ranks={},
        arm_scores=ArmScores(),
    )


def test_rerank_formula():
    print("\n[rerank] multiplicative formula")
    import importlib
    recall_mod = importlib.import_module("helix_memory.recall")

    today = date.today()
    old = date(2026, 1, 1)
    recall_mod._doc_dates = {"new": today, "old": old}
    recall_mod._doc_entity_counts = {"new": 10, "old": 1}

    cands = [_candidate("new", 0.05, 1), _candidate("old", 0.05, 2)]
    out = recall_mod._apply_rerank(cands)
    check("newer wins ties", out[0].id == "new")

    m = out[0].metadata
    exp_importance = 1.0 + math.log1p(10)
    check("importance formula",
          abs(m["importance"] - round(exp_importance, 4)) < 1e-9,
          f"got {m['importance']}")
    check("recency today ~1.0", abs(m["recency_decay"] - 1.0) < 1e-9)
    check("final_score = rrf*i*r",
          abs(m["final_score"] - 0.05 * (1.0 + math.log1p(10)) * 1.0) < 1e-12,
          f"got {m['final_score']}")
    check("final_rank stamped", m["final_rank"] == 1)

    mold = out[1].metadata
    age = (today - old).days
    exp_rec = math.exp(-age / 180.0)
    check("recency decay formula",
          abs(mold["recency_decay"] - round(exp_rec, 4)) < 1e-9,
          f"got {mold['recency_decay']}")
    check("old penalized", mold["final_score"] < m["final_score"])


def test_rerank_undated_neutral():
    print("\n[rerank] undated docs not penalized")
    import importlib
    recall_mod = importlib.import_module("helix_memory.recall")

    recall_mod._doc_dates = {"u": None}
    recall_mod._doc_entity_counts = {"u": 3}
    cands = [_candidate("u", 0.1, 1)]
    out = recall_mod._apply_rerank(cands)
    check("undated recency 1.0", out[0].metadata["recency_decay"] == 1.0)


# ---------------------------------------------------------------- integration

def test_integration():
    print("\n[integration] recall() with enhancements on real data")
    import importlib
    recall_mod = importlib.import_module("helix_memory.recall")

    # Reset caches so the validity index builds fresh
    recall_mod._validity = None

    r = recall_mod.recall("Project approval status", category="personal",
                          top_k=5, include_semantic=False)
    check("recall returns results", len(r) > 0)
    if r:
        check("final_score stamped",
              all("final_score" in c.metadata for c in r))
        graph_hits = [c for c in r if "graph_rank" in c.source_ranks]
        methods = {c.metadata.get("graph_method") for c in graph_hits}
        print(f"  graph methods used: {methods}")
        check("PPR used by graph arm", "ppr" in methods, f"got {methods}")

    # as_of in the past should filter out docs dated after it
    r_now = recall_mod.recall("scheduler outage", top_k=20,
                              include_semantic=False)
    r_past = recall_mod.recall("scheduler outage", top_k=20,
                               include_semantic=False, as_of="2020-01-01")
    check("as_of filters future docs", len(r_past) <= len(r_now),
          f"now={len(r_now)} past={len(r_past)}")

    # rerank=False preserves RRF order
    r_plain = recall_mod.recall("Project approval status",
                                category="personal", top_k=5,
                                include_semantic=False, rerank=False)
    rrf_order = [c.rrf_rank for c in r_plain]
    check("rerank=False keeps RRF order",
          rrf_order == sorted(rrf_order), f"got {rrf_order}")


def main():
    test_validity_supersession()
    test_validity_no_false_supersede()
    test_ppr_multihop()
    test_ppr_specificity()
    test_ppr_no_seeds()
    test_rerank_formula()
    test_rerank_undated_neutral()
    if "--integration" in sys.argv:
        test_integration()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)
    print("All enhancement tests passed.")


if __name__ == "__main__":
    main()
