"""Explicit capture, fresh recall, recovery and verified mirror commands.

Authored by [R2] SRVP2-CODEX. Nothing schedules autonomous work, publishes,
or grants authority; applications must call the capture/recovery hooks.
"""
import argparse
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
import json
import os
from pathlib import Path
import shutil
import sys


def _default_root():
    configured = os.environ.get("HELIX_MEMORY_DIR")
    if configured:
        return configured
    helix = Path.home() / "memory" / "helix"
    return str(helix if helix.is_dir() else Path.home() / "memory")


def _memory_argument(parser):
    parser.add_argument("--memory-dir", default=_default_root(),
                        help="source root; HELIX_MEMORY_DIR or ~/memory[/helix]")


def _scope_arguments(parser):
    _memory_argument(parser)
    parser.add_argument("--category", "--division", dest="categories", action="append",
                        help="limit to this category; repeat for several")
    parser.add_argument("--categories", "--divisions", dest="category_csv",
                        help="comma-separated category list (legacy alias supported)")


def _categories(args):
    values = list(getattr(args, "categories", None) or [])
    if getattr(args, "category_csv", None):
        values.extend(x.strip() for x in args.category_csv.split(",") if x.strip())
    return list(dict.fromkeys(values)) if values else None


def _jsonable(value):
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (Path, date, datetime)):
        return str(value) if isinstance(value, Path) else value.isoformat()
    return value


def _parser():
    from . import __version__
    parser = argparse.ArgumentParser(prog="hmc", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    index = sub.add_parser("index", aliases=["build-index"], help="refresh source indexes")
    _scope_arguments(index)
    index.add_argument("--semantic", action="store_true", help="opt in to model download/indexing")
    index.add_argument("--no-semantic", action="store_true", help="explicit core-only mode")

    recall = sub.add_parser("recall", help="query current source with freshness diagnostics")
    _scope_arguments(recall)
    recall.add_argument("question", nargs="+")
    recall.add_argument("--top-k", type=int, default=10)
    recall.add_argument("--as-of")
    recall.add_argument("--known-at")
    recall.add_argument("--semantic", action="store_true")
    recall.add_argument("--no-semantic", action="store_true")
    recall.add_argument("--dedup", action="store_true")

    latest = sub.add_parser("latest", help="read recent source events without optional indexes")
    _scope_arguments(latest)
    latest.add_argument("--task-id")
    latest.add_argument("--limit", type=int, default=10)
    latest.add_argument("--as-of")
    latest.add_argument("--known-at")

    remember = sub.add_parser("remember", help="append a durable event and verify readback")
    _memory_argument(remember)
    remember.add_argument("--area", "--category", dest="area", required=True)
    remember.add_argument("--actor", required=True, help="full registered author identifier")
    remember.add_argument("--task-id", required=True)
    remember.add_argument("--kind", default="decision")
    content = remember.add_mutually_exclusive_group(required=True)
    content.add_argument("--text")
    content.add_argument("--stdin", action="store_true")
    remember.add_argument("--idempotency-key")
    remember.add_argument("--supersedes", action="append")
    remember.add_argument("--valid-from")
    remember.add_argument("--next-step")

    resume = sub.add_parser("resume", help="read/update the single existing handoff resume block")
    actions = resume.add_subparsers(dest="resume_action", required=True)
    show = actions.add_parser("show")
    update = actions.add_parser("set")
    for child in (show, update):
        _memory_argument(child)
        child.add_argument("--handoff", required=True)
    update.add_argument("--actor", required=True)
    update.add_argument("--task-id", required=True)
    update.add_argument("--goal", required=True)
    update.add_argument("--last-verified-step")
    update.add_argument("--evidence")
    update.add_argument("--next-step")
    update.add_argument("--blocker")
    update.add_argument("--pending-approval")
    update.add_argument("--last-event-id")
    update.add_argument("--expected-revision", type=int)

    mirror = sub.add_parser("mirror", help="create a complete, verified, non-deleting generation")
    _memory_argument(mirror)
    mirror.add_argument("--source", action="append", default=[], metavar="RELATIVE=PATH",
                        help="include a canonical file/directory, including out-of-tree state")
    dest = mirror.add_mutually_exclusive_group(required=True)
    dest.add_argument("--destination", help="directory backend for backup/verification")
    dest.add_argument("--rclone-remote", help="already configured private Drive remote/path")
    mirror.add_argument("--state-dir", default=str(Path.home() / ".helix-memory" / "mirror-outbox"))
    mirror.add_argument("--generation-id")

    verify = sub.add_parser("verify", help="verify every file in a saved generation")
    verify.add_argument("generation")
    restore = sub.add_parser("restore", help="restore into a new or empty directory only")
    restore.add_argument("generation")
    restore.add_argument("target")

    doctor = sub.add_parser("doctor", help="inspect local source/configuration without changing it")
    _scope_arguments(doctor)
    doctor.add_argument("--handoff")
    return parser


def _run(args):
    from .recall import latest_events, recall, rebuild_all
    if args.command in {"index", "build-index"}:
        if args.semantic and args.no_semantic:
            raise ValueError("Choose semantic or no-semantic, not both")
        return rebuild_all(memory_dir=args.memory_dir, categories=_categories(args),
                           include_semantic=args.semantic)
    if args.command == "recall":
        if args.semantic and args.no_semantic:
            raise ValueError("Choose semantic or no-semantic, not both")
        return recall(" ".join(args.question), memory_dir=args.memory_dir,
                      categories=_categories(args), top_k=args.top_k,
                      include_semantic=args.semantic, dedup_mode=args.dedup,
                      as_of=args.as_of, known_at=args.known_at, return_diagnostics=True)
    if args.command == "latest":
        return latest_events(memory_dir=args.memory_dir, categories=_categories(args),
                             task_id=args.task_id, limit=args.limit,
                             as_of=args.as_of, known_at=args.known_at)
    if args.command == "remember":
        from .memory_store import MemoryStore
        text = sys.stdin.read() if args.stdin else args.text
        return MemoryStore(args.memory_dir).remember(
            text, area=args.area, actor=args.actor, task_id=args.task_id,
            kind=args.kind, idempotency_key=args.idempotency_key,
            supersedes=args.supersedes, valid_from=args.valid_from,
            next_step=args.next_step)
    if args.command == "resume":
        from .memory_store import MemoryStore
        store = MemoryStore(args.memory_dir)
        if args.resume_action == "show":
            return store.read_resume(args.handoff)
        state = {key: getattr(args, key) for key in [
            "actor", "task_id", "goal", "last_verified_step", "evidence", "next_step",
            "blocker", "pending_approval", "last_event_id"]}
        return store.update_resume(args.handoff, state, expected_revision=args.expected_revision)
    if args.command == "mirror":
        from .mirror import mirror_to_directory, mirror_to_rclone
        sources = {}
        for source in args.source:
            logical, separator, path = source.partition("=")
            if not separator or not logical or not path or logical in sources:
                raise ValueError("Each source must be a unique RELATIVE=PATH mapping")
            sources[logical] = path
        if not sources:
            sources = {"memory": args.memory_dir}
        if args.destination:
            return mirror_to_directory(sources, args.destination, generation_id=args.generation_id)
        return mirror_to_rclone(sources, args.rclone_remote, state_dir=args.state_dir,
                                generation_id=args.generation_id)
    if args.command == "verify":
        from .mirror import verify_generation
        return verify_generation(args.generation)
    if args.command == "restore":
        from .mirror import restore_generation
        return restore_generation(args.generation, args.target)
    if args.command == "doctor":
        from .collector import collect_documents
        from . import __version__
        root = Path(args.memory_dir).expanduser().resolve()
        docs = collect_documents(memory_dir=str(root), categories=_categories(args))
        result = {"version": __version__, "memory_dir": str(root), "source_exists": root.is_dir(),
                  "document_count": len(docs), "categories": sorted({d["category"] for d in docs}),
                  "rclone_available": shutil.which("rclone") is not None,
                  "capture_mode": "explicit API/CLI; provider hooks must be connected separately",
                  "mirror_status": "not checked; run mirror and inspect its verification receipt"}
        if args.handoff:
            from .memory_store import MemoryStore
            result["handoff_exists"] = Path(args.handoff).expanduser().is_file()
            result["resume"] = MemoryStore(args.memory_dir).read_resume(args.handoff)
        return result
    raise ValueError("Unknown command")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        _parser().print_help()
        return 0
    commands = {"index", "build-index", "recall", "latest", "remember", "resume",
                "mirror", "verify", "restore", "doctor", "--help", "-h", "--version"}
    # Backward-compatible fleet spelling: hmc "question" or hmc --as-of DATE "question".
    if argv[0] not in commands:
        argv.insert(0, "recall")
    args = _parser().parse_args(argv)
    try:
        value = _run(args)
        print(json.dumps(_jsonable(value), indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, OSError, RuntimeError, TimeoutError) as error:
        message = {"ok": False, "error": str(error), "type": type(error).__name__}
        if getattr(error, "receipt", None) is not None:
            message["receipt"] = error.receipt
        print(json.dumps(_jsonable(message), ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
