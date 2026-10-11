#!/usr/bin/env python3
"""Build (or rebuild) all helix-memory indexes from ~/memory/helix/.

Usage:
    python scripts/build_index.py [--no-semantic] [--divisions 00-system,03-work]

The BM25, entity-graph, and temporal indexes are always built.
The semantic (vector) index requires sentence-transformers + chromadb;
pass --no-semantic to skip it (recall still works with 3 arms).
"""

import argparse
import os
import sys
import time

# Allow running from the repo root without installing: python scripts/build_index.py
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from helix_memory import build_index
from helix_memory.collector import DIVISIONS


def main() -> None:
    parser = argparse.ArgumentParser(description="Build helix-memory indexes")
    parser.add_argument(
        "--no-semantic",
        action="store_true",
        help="skip the semantic (vector) arm",
    )
    parser.add_argument(
        "--divisions",
        default=None,
        help="comma-separated division list (default: all)",
    )
    args = parser.parse_args()

    divisions = args.divisions.split(",") if args.divisions else None
    if divisions:
        unknown = set(divisions) - set(DIVISIONS)
        if unknown:
            parser.error(f"unknown divisions: {sorted(unknown)} (valid: {DIVISIONS})")

    print(f"Building indexes for divisions: {divisions or 'all'}")
    t0 = time.time()
    stats = build_index(include_semantic=not args.no_semantic, divisions=divisions)
    dt = time.time() - t0

    print(f"\nDone in {dt:.1f}s")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
