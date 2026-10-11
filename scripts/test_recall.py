#!/usr/bin/env python3
"""Smoke test: 4-arm hybrid recall on sample data.

Usage:
    python scripts/test_recall.py [--no-semantic]

Requires the helix_memory package installed (pip install -e .)
and indexes built (python scripts/build_index.py).
"""

import argparse
import os
import sys
import time

# Allow running from the repo root without installing: python scripts/test_recall.py
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from helix_memory import recall


def show(query, **kw):
    print(f"\n=== {query} ===")
    t0 = time.time()
    results = recall(query, top_k=5, **kw)
    dt = time.time() - t0
    print(f"({dt:.1f}s, {len(results)} results)")
    for r in results:
        arms = ",".join(
            f"{k.replace('_rank', '')}#{v}" for k, v in r.source_ranks.items()
        )
        print(f"  #{r.rrf_rank} score={r.rrf_score:.4f} [{arms}]")
        print(f"      {r.id}")
    return results


def main():
    parser = argparse.ArgumentParser(description="Smoke-test helix-memory recall")
    parser.add_argument("--no-semantic", action="store_true")
    args = parser.parse_args()
    kw = {"include_semantic": not args.no_semantic}

    r1 = show("What did the team decide about the project timeline?", **kw)
    assert len(r1) > 0, "expected results for crypto query"

    r2 = show("Project approval status", category="personal", **kw)
    r3 = show("What happened with the scheduler outage?", **kw)
    r4 = show("morning brief voice note", **kw)

    # Temporal arm check
    r5 = show("What happened yesterday?", **kw)
    temporal_hits = [r for r in r5 if "temporal_rank" in r.source_ranks]
    print(f"\nTemporal arm contributed to {len(temporal_hits)}/{len(r5)} results")

    # Dedup mode check
    r6 = show(
        "The team agreed to pause notifications, Alex owns the backend",
        dedup_mode=True,
        **kw,
    )
    assert len(r6) > 0, "expected results in dedup mode"

    print("\nAll smoke tests passed.")


if __name__ == "__main__":
    main()
