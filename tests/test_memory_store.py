"""Regression checks for capture durability, idempotency and exact resume state."""

from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
from pathlib import Path

import pytest

from helix_memory.memory_store import (
    EVENT_PREFIX,
    IdempotencyConflict,
    MemoryIntegrityError,
    MemoryStore,
    RESUME_END,
    RESUME_START,
    RevisionConflict,
    validate_event_text,
)


ACTOR = "[R2] SRVP2-CODEX"


def capture(root, text="Preserve the existing structure.", **kwargs):
    return MemoryStore(root).remember(text, area="00-system", actor=ACTOR, task_id="memory-repair", **kwargs)


def same_process_capture(root):
    return capture(root, idempotency_key="shared-request")


def state(**updates):
    return {"task_id": "memory-repair", "goal": "Restore exact recent context", "last_verified_step": "Reproduced index freshness failure", "evidence": ["local isolated test"], "next_step": "Verify the repair", "blocker": None, "pending_approval": "No live deployment authorized", "actor": ACTOR, **updates}


def test_immutable_capture_exact_source_metadata_and_restart(tmp_path):
    text = "  Exact assertion, not a verified observation.\n\n## Keep this section intact\nRésumé ✓\n"
    metadata = {"source": {"kind": "user_message", "id": "turn-42"}, "verification_status": "source_assertion", "confidence": None}
    receipt = capture(tmp_path, text, metadata=metadata)
    event = MemoryStore(tmp_path).read_event(receipt["event_id"])
    assert event["text"] == text
    assert event["metadata"] == metadata
    assert event["actor"] == ACTOR
    assert event["task_id"] == "memory-repair"
    assert receipt["durable"] and receipt["readback_verified"]
    assert hashlib.sha256(Path(receipt["path"]).read_bytes()).hexdigest() == receipt["sha256"]
    first = Path(receipt["path"]).read_text().splitlines()[0]
    assert first.startswith(EVENT_PREFIX) and first.endswith(" -->")
    assert json.loads(first[len(EVENT_PREFIX):-4])["event_id"] == receipt["event_id"]
    assert Path(receipt["relative_path"]).parts[:2] == ("00-system", "qpulse")


def test_idempotency_two_calls_return_one_immutable_event(tmp_path):
    a = capture(tmp_path, idempotency_key="turn-42")
    b = capture(tmp_path, idempotency_key="turn-42")
    assert a == b
    assert len(list(tmp_path.glob("*/qpulse/*.md"))) == 1


def test_idempotency_conflict_preserves_original(tmp_path):
    a = capture(tmp_path, idempotency_key="turn-42")
    original = Path(a["path"]).read_bytes()
    with pytest.raises(IdempotencyConflict):
        capture(tmp_path, "Replace the structure", idempotency_key="turn-42")
    assert Path(a["path"]).read_bytes() == original
    assert len(list(tmp_path.glob("*/qpulse/*.md"))) == 1


def test_idempotency_is_cross_process(tmp_path):
    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(same_process_capture, [str(tmp_path)] * 8))
    assert len({result["event_id"] for result in results}) == 1
    assert len(list(tmp_path.glob("*/qpulse/*.md"))) == 1


def test_event_survives_lookup_loss_and_idempotency_recovers(tmp_path):
    a = capture(tmp_path, idempotency_key="turn-42")
    (tmp_path / ".helix-state/events.json").unlink()
    assert MemoryStore(tmp_path).read_event(a["event_id"])["text"] == "Preserve the existing structure."
    assert capture(tmp_path, idempotency_key="turn-42") == a
    assert len(list(tmp_path.glob("*/qpulse/*.md"))) == 1


def test_capture_retries_after_event_written_but_lookup_commit_failed(tmp_path, monkeypatch):
    import helix_memory.memory_store as module
    real_write = module.atomic_write_json

    def fail(*args, **kwargs):
        raise OSError("simulated lookup commit interruption")

    monkeypatch.setattr(module, "atomic_write_json", fail)
    with pytest.raises(OSError, match="interruption"):
        capture(tmp_path, idempotency_key="turn-42")
    assert len(list(tmp_path.glob("*/qpulse/*.md"))) == 1
    monkeypatch.setattr(module, "atomic_write_json", real_write)
    assert capture(tmp_path, idempotency_key="turn-42")["readback_verified"]
    assert len(list(tmp_path.glob("*/qpulse/*.md"))) == 1


def test_correction_links_explicit_id_and_preserves_old_event(tmp_path):
    old = capture(tmp_path, "Old source assertion")
    before = Path(old["path"]).read_bytes()
    new = capture(tmp_path, "Correction from user", kind="correction", supersedes=[old["event_id"]], valid_from="2026-10-18T12:34:56-07:00", metadata={"source": "user direct correction", "verification_status": "source_assertion"})
    event = MemoryStore(tmp_path).read_event(new["event_id"])
    assert event["supersedes"] == [old["event_id"]]
    assert event["valid_from"] == "2026-10-18T19:34:56.000000+00:00"
    assert event["recorded_at"] != event["valid_from"]
    assert Path(old["path"]).read_bytes() == before


@pytest.mark.parametrize("kwargs", [{"actor": "SRVP2"}, {"area": "../../outside"}, {"valid_from": "2026-10-18"}, {"valid_from": "2026-10-18T10:00:00"}, {"metadata": {"x": float("nan")}}, {"area": "00-system", "category": "02-finance"}])
def test_invalid_capture_is_rejected_before_writes(tmp_path, kwargs):
    base = {"text": "A record", "area": "00-system", "actor": ACTOR, "task_id": "task"}
    base.update(kwargs)
    with pytest.raises((ValueError, TypeError)):
        MemoryStore(tmp_path).remember(**base)
    assert not list(tmp_path.rglob("*.md"))


def test_category_alias_and_no_fabricated_counter(tmp_path):
    result = MemoryStore(tmp_path).remember("No Qpoint allocated", category="00-system", actor=ACTOR, task_id="t")
    event = MemoryStore(tmp_path).read_event(result["event_id"])
    assert "epoch" not in event and "qpoint" not in event


def test_modified_primary_event_fails_readback_integrity(tmp_path):
    result = capture(tmp_path)
    Path(result["path"]).write_text("changed")
    with pytest.raises(MemoryIntegrityError):
        MemoryStore(tmp_path).read_event(result["event_id"])


def test_pure_event_parser_checks_body_checksum(tmp_path):
    receipt = capture(tmp_path)
    original = Path(receipt["path"]).read_bytes().decode("utf-8")
    assert validate_event_text(original)["text"] == "Preserve the existing structure."
    with pytest.raises(MemoryIntegrityError, match="checksum"):
        validate_event_text(original.replace("Preserve the existing structure.", "Discard the existing structure."))


def test_pure_event_parser_preserves_crlf_source_exactly(tmp_path):
    body = "Keep CRLF source.\r\n\r\n## Evidence\r\nExact original lines.\r\n"
    receipt = capture(tmp_path, body)
    original = Path(receipt["path"]).read_bytes().decode("utf-8")
    assert validate_event_text(original)["text"] == body
    # Header line endings are transport syntax, body line endings are payload.
    header, exact_body = original.split("\n\n", 1)
    assert validate_event_text(header + "\r\n\r\n" + exact_body)["text"] == body
    with pytest.raises(MemoryIntegrityError, match="checksum"):
        validate_event_text(original.replace("\r\n", "\n"))
    assert MemoryStore(tmp_path).read_event(receipt["event_id"])["text"] == body


@pytest.mark.parametrize("change", [{"schema_version": 2}, {"schema_version": True}, {"actor": "SRVP2"}, {"metadata": {"verification_status": "forged independent verification"}}, {"idempotency_key_hash": "invalid"}, {"event_id": "invented-id"}])
def test_pure_event_parser_rejects_unsupported_or_modified_schema(tmp_path, change):
    receipt = capture(tmp_path)
    original = Path(receipt["path"]).read_bytes().decode("utf-8")
    header, body = original.split("\n\n", 1)
    meta = json.loads(header[len(EVENT_PREFIX):-4])
    meta.update(change)
    modified = EVENT_PREFIX + json.dumps(meta) + " -->\n\n" + body
    with pytest.raises(MemoryIntegrityError):
        validate_event_text(modified)


@pytest.mark.parametrize("change", [{"recorded_at": "2026-10-19T01:02:03.000000+00:00"}, {"event_id": "a4f105d3-48e1-451f-afd0-6215b8b30367"}, {"next_step": "Unrecorded different step"}, {"unrecorded_extra_field": "Changed header metadata"}])
def test_whole_record_checksum_rejects_header_changes_in_parser_and_collector(tmp_path, change):
    from helix_memory.collector import collect_documents

    receipt = capture(tmp_path, valid_from="2026-10-18T10:00:00+00:00")
    path = Path(receipt["path"])
    header, body = path.read_bytes().decode("utf-8").split("\n\n", 1)
    meta = json.loads(header[len(EVENT_PREFIX):-4])
    meta.update(change)
    modified = EVENT_PREFIX + json.dumps(meta) + " -->\n\n" + body
    with pytest.raises(MemoryIntegrityError, match="checksum"):
        validate_event_text(modified)
    path.write_bytes(modified.encode("utf-8"))
    with pytest.raises(MemoryIntegrityError):
        collect_documents(str(tmp_path))
    with pytest.raises(MemoryIntegrityError):
        MemoryStore(tmp_path).read_event(receipt["event_id"])


def test_store_schema_requires_whole_record_checksum_without_legacy_downgrade(tmp_path):
    from helix_memory.collector import collect_documents

    receipt = capture(tmp_path)
    path = Path(receipt["path"])
    header, body = path.read_bytes().decode("utf-8").split("\n\n", 1)
    meta = json.loads(header[len(EVENT_PREFIX):-4])
    del meta["record_sha256"]
    modified = EVENT_PREFIX + json.dumps(meta) + " -->\n\n" + body
    with pytest.raises(MemoryIntegrityError, match="schema"):
        validate_event_text(modified)
    path.write_bytes(modified.encode("utf-8"))
    with pytest.raises(MemoryIntegrityError):
        collect_documents(str(tmp_path))


def test_corrupt_lookup_fails_without_overwriting_primary(tmp_path):
    result = capture(tmp_path)
    before = Path(result["path"]).read_bytes()
    (tmp_path / ".helix-state/events.json").write_text("not JSON")
    with pytest.raises(MemoryIntegrityError):
        capture(tmp_path, "New record")
    assert Path(result["path"]).read_bytes() == before


def test_symlinked_area_cannot_create_files_outside_store(tmp_path):
    root = tmp_path / "memory"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "00-system").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="outside"):
        capture(root)
    assert list(outside.iterdir()) == []


def test_fsync_failure_never_returns_success_receipt(tmp_path, monkeypatch):
    import helix_memory.memory_store as module

    def fail(_):
        raise OSError("simulated durable write failure")

    monkeypatch.setattr(module.os, "fsync", fail)
    with pytest.raises(OSError, match="durable write failure"):
        capture(tmp_path)


def test_resume_requires_existing_authoritative_handoff(tmp_path):
    with pytest.raises(FileNotFoundError):
        MemoryStore(tmp_path / "memory").update_resume(tmp_path / "Living_Handoff.html", state())
    assert not (tmp_path / "Living_Handoff.html").exists()


def test_resume_preserves_every_byte_outside_block_and_full_history(tmp_path):
    path = tmp_path / "Living_Handoff.html"
    before = b'<!doctype html>\r\n<html><body>\r\n<section>Original authoritative task</section>\r\n</body></html>\x00'
    path.write_bytes(before)
    store = MemoryStore(tmp_path / "memory")
    assert store.read_resume(path) is None
    receipt = store.update_resume(path, state(), expected_revision=0)
    current = path.read_bytes()
    bounds = store._resume_bounds(current)
    assert current[:bounds[0]] + current[bounds[1]:] == before
    assert Path(receipt["history_path"]).read_bytes() == before
    first_resume = store.read_resume(path)
    assert first_resume["revision"] == 1
    assert first_resume["state"] == state()
    original_prefix, original_suffix = current[:bounds[0]], current[bounds[1]:]
    second = store.update_resume(path, state(next_step="Review measured repair"), expected_revision=1)
    updated = path.read_bytes()
    bounds2 = store._resume_bounds(updated)
    assert updated[:bounds2[0]] == original_prefix
    assert updated[bounds2[1]:] == original_suffix
    assert Path(second["history_path"]).read_bytes() == current
    assert store.read_resume(path)["revision"] == 2
    assert not list((tmp_path / "memory").rglob("*.html"))


def test_resume_restart_restores_exact_task_and_event_id(tmp_path):
    memory = tmp_path / "memory"
    path = tmp_path / "Living_Handoff.html"
    path.write_text("<html><body>Keep me</body></html>")
    event = capture(memory)
    original = state(last_event_id=event["event_id"])
    MemoryStore(memory).update_resume(path, original)
    restored = MemoryStore(memory).read_resume(path)
    assert restored["state"] == original
    assert restored["event_ids"] == [event["event_id"]]


def test_resume_cas_rejects_stale_revision_without_modifying_handoff(tmp_path):
    path = tmp_path / "Living_Handoff.html"
    path.write_text("<body>Preserve</body>")
    store = MemoryStore(tmp_path / "memory")
    store.update_resume(path, state(), expected_revision=0)
    before = path.read_bytes()
    with pytest.raises(RevisionConflict):
        store.update_resume(path, state(next_step="Stale update"), expected_revision=0)
    assert path.read_bytes() == before


@pytest.mark.parametrize("bad", [RESUME_START, RESUME_END, RESUME_END + RESUME_START, RESUME_START + RESUME_END + RESUME_START + RESUME_END, RESUME_START + b"<div>No state</div>" + RESUME_END])
def test_resume_malformed_markers_rejected_without_change(tmp_path, bad):
    path = tmp_path / "Living_Handoff.html"
    path.write_bytes(bad)
    with pytest.raises(MemoryIntegrityError):
        MemoryStore(tmp_path / "memory").update_resume(path, state())
    assert path.read_bytes() == bad


def test_resume_json_and_visible_text_cannot_close_script_or_inject_html(tmp_path):
    path = tmp_path / "Living_Handoff.html"
    path.write_text("<body>Existing</body>")
    malicious = '</script><img src=x onerror="alert(1)"><script>'
    wanted = state(goal=malicious, next_step="<b>Read, do not execute</b>")
    store = MemoryStore(tmp_path / "memory")
    store.update_resume(path, wanted)
    raw = path.read_text()
    assert '<img src=x' not in raw
    assert raw.count("</script>") == 1
    assert "&lt;img" in raw
    assert "\\u003c/script\\u003e" in raw
    assert store.read_resume(path)["state"] == wanted


def test_capture_collector_sees_exact_immutable_event(tmp_path):
    from helix_memory.collector import collect_documents
    text = "Freshly captured recent decision.\n\n## Evidence\n" + ("Detailed evidence remains in one immutable record.\n" * 40)
    result = capture(tmp_path, text)
    docs = collect_documents(str(tmp_path))
    matches = [doc for doc in docs if doc.get("event_id") == result["event_id"]]
    assert len(matches) == 1
    assert text in matches[0]["text"]

