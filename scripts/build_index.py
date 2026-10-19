#!/usr/bin/env python3
"""Compatibility entry point. Authored by [R2] SRVP2-CODEX."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helix_memory.cli import main
if __name__ == "__main__":
    raise SystemExit(main(["index", *sys.argv[1:]]))
