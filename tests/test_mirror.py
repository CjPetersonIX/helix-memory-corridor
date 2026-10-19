"""Mirror regression tests. Authored by [R2] SRVP2-CODEX."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from helix_memory import mirror


def sample(tmp_path):
    memory = tmp_path / "source"
    (memory / "helix" / "00-system" / "qpulse").mkdir(parents=True)
    (memory / "empty").mkdir()
    (memory / "MEMORY.md").write_text("# Startup\n", encoding="utf-8")
    (memory / "helix" / "00-system" / "qpulse" / "2026-10-19.jsonl").write_bytes(b'{"event":"one"}\n')
    outside = tmp_path / "workspace" / "user" / "files" / "Living_Handoff.html"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"<h1>Real task state</h1>\x00")
    return {"memory": memory, "handoff/Living_Handoff.html": outside}


def file_map(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def current(root):
    return json.loads((root / "current.json").read_text())


class FakeRclone:
    """A local stand-in exercising exact rclone argument flow, never a network."""

    def __init__(self, root, *, corrupt=False, interrupt=False):
        self.root = root
        self.corrupt = corrupt
        self.interrupt = interrupt
        self.calls = []

    def path(self, value):
        return self.root / value.split(":", 1)[1] if value.startswith("drive:") else Path(value)

    def __call__(self, arguments, *, executable, timeout):
        self.calls.append(arguments)
        verb, source, destination = arguments[:3]
        assert verb in ("copy", "copyto")
        src, dst = self.path(source), self.path(destination)
        if self.interrupt and destination.startswith("drive:") and verb == "copy":
            dst.mkdir(parents=True, exist_ok=True)
            (dst / "interrupted-upload.txt").write_text("partial")
            raise mirror.MirrorError("simulated interrupted upload")
        if verb == "copy":
            for path in src.rglob("*"):
                target = dst / path.relative_to(src)
                if path.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if "--immutable" in arguments and target.exists() and target.read_bytes() != path.read_bytes():
                        raise mirror.MirrorError("immutable destination differs")
                    target.write_bytes(path.read_bytes())
            if self.corrupt and source.startswith("drive:"):
                (dst / "tree" / "memory" / "MEMORY.md").write_text("CORRUPTED")
        else:
            if not src.exists():
                raise mirror.MirrorError("file not found", returncode=4)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)


def test_explicit_whole_memory_and_outside_handoff_mapping(tmp_path):
    sources = sample(tmp_path)
    receipt = mirror.mirror_to_directory(sources, tmp_path / "mirror", generation_id="first")
    generation = Path(receipt["local_generation"])
    assert receipt["status"] == "local_verified"
    assert receipt["remote_verified_generation"] is None
    assert file_map(generation / "tree" / "memory") == file_map(sources["memory"])
    assert (generation / "tree" / "handoff" / "Living_Handoff.html").read_bytes() == sources["handoff/Living_Handoff.html"].read_bytes()
    assert (generation / "tree" / "memory" / "empty").is_dir()
    assert current(tmp_path / "mirror")["generation_id"] == "first"


def test_manifest_records_every_path_size_and_digest(tmp_path):
    receipt = mirror.mirror_to_directory(sample(tmp_path), tmp_path / "mirror")
    generation = Path(receipt["local_generation"])
    manifest = json.loads((generation / "manifest.json").read_text())
    for logical, data in file_map(generation / "tree").items():
        assert manifest["entries"][logical] == {"kind": "file", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    assert mirror.verify_generation(generation)["verified"] is True


def test_rename_and_removal_preserve_previous_generation(tmp_path):
    sources = sample(tmp_path)
    root = tmp_path / "mirror"
    first = mirror.mirror_to_directory(sources, root, generation_id="first")
    old_bytes = file_map(Path(first["local_generation"]))
    (sources["memory"] / "MEMORY.md").rename(sources["memory"] / "renamed.md")
    sources["handoff/Living_Handoff.html"].unlink()
    second = mirror.mirror_to_directory({"memory": sources["memory"]}, root, generation_id="second")
    assert file_map(Path(first["local_generation"])) == old_bytes
    assert "memory/renamed.md" in file_map(Path(second["local_generation"]) / "tree")
    assert "memory/MEMORY.md" not in file_map(Path(second["local_generation"]) / "tree")
    assert "handoff/Living_Handoff.html" in file_map(Path(first["local_generation"]) / "tree")


def test_same_generation_is_idempotent_but_never_replaced(tmp_path):
    sources = sample(tmp_path)
    root = tmp_path / "mirror"
    first = mirror.mirror_to_directory(sources, root, generation_id="stable")
    assert mirror.mirror_to_directory(sources, root, generation_id="stable")["generation_id"] == "stable"
    original = file_map(Path(first["local_generation"]))
    (sources["memory"] / "MEMORY.md").write_text("changed")
    with pytest.raises(mirror.MirrorError, match="different source"):
        mirror.mirror_to_directory(sources, root, generation_id="stable")
    assert file_map(Path(first["local_generation"])) == original


@pytest.mark.parametrize("logical", ["../escape", "/absolute", "a/../escape", "a//b", "a/./b", "a\\b", ""])
def test_rejects_logical_path_escapes(tmp_path, logical):
    source = tmp_path / "file"
    source.write_text("one")
    with pytest.raises(mirror.MirrorError):
        mirror.mirror_to_directory({logical: source}, tmp_path / "mirror")


def test_overlapping_mapping_is_rejected(tmp_path):
    sources = sample(tmp_path)
    with pytest.raises(mirror.MirrorError, match="Overlapping"):
        mirror.mirror_to_directory({"memory": sources["memory"], "memory/file": sources["handoff/Living_Handoff.html"]}, tmp_path / "mirror")


def test_source_symlink_and_target_inside_source_rejected(tmp_path):
    sources = sample(tmp_path)
    (sources["memory"] / "link").symlink_to(sources["handoff/Living_Handoff.html"])
    with pytest.raises(mirror.MirrorError, match="Symbolic"):
        mirror.mirror_to_directory(sources, tmp_path / "mirror")
    (sources["memory"] / "link").unlink()
    with pytest.raises(mirror.MirrorError, match="inside a source"):
        mirror.mirror_to_directory(sources, sources["memory"] / "mirror")


def test_database_names_headers_and_wal_are_rejected(tmp_path):
    source = tmp_path / "active.sqlite3"
    connection = sqlite3.connect(source)
    connection.execute("create table state (value text)")
    connection.commit()
    with pytest.raises(mirror.MirrorError, match="Database copies"):
        mirror.mirror_to_directory({"memory/db": source}, tmp_path / "mirror")
    renamed = tmp_path / "db-disguised.bin"
    renamed.write_bytes(source.read_bytes())
    with pytest.raises(mirror.MirrorError, match="Database copies"):
        mirror.mirror_to_directory({"memory/db": renamed}, tmp_path / "mirror")
    connection.close()
    wal = tmp_path / "anything-wal"
    wal.write_bytes(b"not a valid backup")
    with pytest.raises(mirror.MirrorError, match="Database copies"):
        mirror.mirror_to_directory({"memory/db": wal}, tmp_path / "mirror")


def test_consistent_sqlite_text_export_is_allowed(tmp_path):
    connection = sqlite3.connect(":memory:")
    connection.execute("create table state (value text)")
    connection.execute("insert into state values ('saved')")
    export = tmp_path / "mempalace.sql"
    export.write_text("\n".join(connection.iterdump()))
    connection.close()
    receipt = mirror.mirror_to_directory({"memory/mempalace.sql": export}, tmp_path / "mirror")
    restored = sqlite3.connect(":memory:")
    restored.executescript((Path(receipt["local_generation"]) / "tree" / "memory" / "mempalace.sql").read_text())
    assert restored.execute("select value from state").fetchone()[0] == "saved"
    restored.close()


def test_concurrent_source_change_rejects_before_pointer_advance(tmp_path, monkeypatch):
    sources = sample(tmp_path)
    root = tmp_path / "mirror"
    mirror.mirror_to_directory(sources, root, generation_id="first")
    real_scan = mirror._scan
    scans = 0

    def changing_scan(mapping):
        nonlocal scans
        scans += 1
        if scans == 2:
            (sources["memory"] / "MEMORY.md").write_text("concurrent writer changed this")
        return real_scan(mapping)

    monkeypatch.setattr(mirror, "_scan", changing_scan)
    with pytest.raises(mirror.MirrorError, match="changed during snapshot"):
        mirror.mirror_to_directory(sources, root, generation_id="second")
    assert current(root)["generation_id"] == "first"
    assert (root / "generations" / "second").exists()
    assert any(json.loads(p.read_text())["status"] == "failed" for p in (root / "receipts").glob("*.json"))


def test_corrupted_generation_or_extra_file_fails_verification(tmp_path):
    receipt = mirror.mirror_to_directory(sample(tmp_path), tmp_path / "mirror")
    generation = Path(receipt["local_generation"])
    (generation / "tree" / "extra.txt").write_text("not in manifest")
    with pytest.raises(mirror.MirrorError, match="verification failed"):
        mirror.verify_generation(generation)


def test_restore_is_exact_and_refuses_existing_content(tmp_path):
    receipt = mirror.mirror_to_directory(sample(tmp_path), tmp_path / "mirror")
    generation = Path(receipt["local_generation"])
    target = tmp_path / "restored"
    assert mirror.restore_generation(generation, target)["restored"] is True
    assert file_map(target) == file_map(generation / "tree")
    assert (target / "memory" / "empty").is_dir()
    with pytest.raises(mirror.MirrorError, match="absent or empty"):
        mirror.restore_generation(generation, target)
    assert file_map(target) == file_map(generation / "tree")


def test_rclone_roundtrip_verifies_bytes_and_pointer_before_success(tmp_path, monkeypatch):
    fake = FakeRclone(tmp_path / "remote")
    monkeypatch.setattr(mirror, "_run_rclone", fake)
    receipt = mirror.mirror_to_rclone(sample(tmp_path), "drive:memory-library", state_dir=tmp_path / "state", generation_id="one")
    assert receipt["status"] == "remote_verified"
    assert receipt["remote_verified_generation"] == "one"
    assert json.loads((tmp_path / "remote" / "memory-library" / "current.json").read_text())["generation_id"] == "one"
    assert [call[0] for call in fake.calls] == ["copy", "copy", "copyto", "copyto", "copyto"]
    assert "--immutable" in fake.calls[0]
    assert receipt["remote_identity"] == "drive:memory-library"
    assert receipt["snapshot_created_at"]
    assert receipt["current_advanced"] is True


def test_remote_corruption_keeps_previous_pointer(tmp_path, monkeypatch):
    fake = FakeRclone(tmp_path / "remote")
    monkeypatch.setattr(mirror, "_run_rclone", fake)
    sources = sample(tmp_path)
    mirror.mirror_to_rclone(sources, "drive:library", state_dir=tmp_path / "state", generation_id="first")
    fake.corrupt = True
    with pytest.raises(mirror.MirrorError, match="verification failed") as error:
        mirror.mirror_to_rclone(sources, "drive:library", state_dir=tmp_path / "state", generation_id="second")
    assert error.value.receipt["status"] == "remote_pending"
    assert error.value.receipt["remote_verified_generation"] is None
    assert json.loads((tmp_path / "remote" / "library" / "current.json").read_text())["generation_id"] == "first"
    assert (tmp_path / "state" / "generations" / "second").exists()


def test_interrupted_remote_upload_keeps_snapshot_and_retryable_receipt(tmp_path, monkeypatch):
    fake = FakeRclone(tmp_path / "remote", interrupt=True)
    monkeypatch.setattr(mirror, "_run_rclone", fake)
    with pytest.raises(mirror.MirrorError, match="interrupted upload") as error:
        mirror.mirror_to_rclone(sample(tmp_path), "drive:library", state_dir=tmp_path / "state", generation_id="queued")
    assert error.value.receipt["status"] == "remote_pending"
    generation = Path(error.value.receipt["local_generation"])
    assert mirror.verify_generation(generation)["verified"] is True
    assert not (tmp_path / "remote" / "library" / "current.json").exists()
    # Simulate upload interruption after a valid subset, without an extraneous object.
    (tmp_path / "remote" / "library" / "generations" / "queued" / "interrupted-upload.txt").unlink()
    fake.interrupt = False
    retried = mirror.retry_to_rclone(generation, "drive:library")
    assert retried["status"] == "remote_verified"
    assert retried["generation_id"] == "queued"


def test_failed_subprocess_output_does_not_leak_secrets(tmp_path, monkeypatch):
    class Failed:
        returncode = 1
        stderr = b"secret signed URL must not be included"
    monkeypatch.setattr(mirror.subprocess, "run", lambda *args, **kwargs: Failed())
    with pytest.raises(mirror.MirrorError) as error:
        mirror.mirror_to_rclone(sample(tmp_path), "drive:library", state_dir=tmp_path / "state")
    assert "secret" not in str(error.value)


def test_manifest_traversal_rejected_before_restore(tmp_path):
    receipt = mirror.mirror_to_directory(sample(tmp_path), tmp_path / "mirror")
    generation = Path(receipt["local_generation"])
    manifest = json.loads((generation / "manifest.json").read_text())
    manifest["entries"]["../escape"] = {"kind": "directory"}
    (generation / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(mirror.MirrorError, match="Unsafe logical path"):
        mirror.restore_generation(generation, tmp_path / "restored")
    assert not (tmp_path / "escape").exists()


def test_symlink_destination_never_writes_through(tmp_path):
    sources = sample(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    destination = tmp_path / "linked"
    destination.symlink_to(real, target_is_directory=True)
    with pytest.raises(mirror.MirrorError, match="Symbolic"):
        mirror.mirror_to_directory(sources, destination)
    assert not any(real.iterdir())


def test_source_mutation_between_scan_and_copy_fails(tmp_path, monkeypatch):
    sources = sample(tmp_path)
    real_read = mirror._read_file
    reads = 0

    def changing_read(path):
        nonlocal reads
        reads += 1
        if reads == 4:  # Three mapped files were read during the initial scan.
            path.write_bytes(b"concurrent new bytes")
        return real_read(path)

    monkeypatch.setattr(mirror, "_read_file", changing_read)
    with pytest.raises(mirror.MirrorError, match="changed during snapshot"):
        mirror.mirror_to_directory(sources, tmp_path / "mirror", generation_id="changing")
    assert not (tmp_path / "mirror" / "current.json").exists()


def test_pointer_readback_is_required_for_remote_verified_receipt(tmp_path, monkeypatch):
    fake = FakeRclone(tmp_path / "remote")

    def corrupt_pointer(arguments, **kwargs):
        fake(arguments, **kwargs)
        if arguments[0] == "copyto" and arguments[1].startswith("drive:"):
            Path(arguments[2]).write_text('{"generation_id":"unexpected"}')

    monkeypatch.setattr(mirror, "_run_rclone", corrupt_pointer)
    with pytest.raises(mirror.MirrorError, match="pointer readback mismatch") as error:
        mirror.mirror_to_rclone(sample(tmp_path), "drive:library", state_dir=tmp_path / "state", generation_id="one")
    assert error.value.receipt["status"] == "remote_pending"
    assert error.value.receipt["remote_verified_generation"] is None
    assert not (tmp_path / "state" / "remote-current.json").exists()


def test_older_retry_preserves_remote_history_without_rewinding_current(tmp_path, monkeypatch):
    fake = FakeRclone(tmp_path / "remote")
    monkeypatch.setattr(mirror, "_run_rclone", fake)
    sources = sample(tmp_path)
    state = tmp_path / "state"
    older = mirror.mirror_to_directory(sources, state, generation_id="older")
    (sources["memory"] / "MEMORY.md").write_text("newer state")
    newer = mirror.mirror_to_rclone(sources, "drive:library", state_dir=state, generation_id="newer")
    pointer = (tmp_path / "remote" / "library" / "current.json").read_bytes()
    retried = mirror.retry_to_rclone(older["local_generation"], "drive:library")
    assert retried["status"] == "remote_history_verified"
    assert retried["remote_verified_generation"] == "older"
    assert retried["remote_current_generation"] == "newer"
    assert retried["current_advanced"] is False
    assert retried["snapshot_created_at"] < newer["snapshot_created_at"]
    assert (tmp_path / "remote" / "library" / "current.json").read_bytes() == pointer
    assert (tmp_path / "remote" / "library" / "generations" / "older").exists()


def test_unknown_existing_pointer_read_failure_never_overwrites_current(tmp_path, monkeypatch):
    fake = FakeRclone(tmp_path / "remote")
    monkeypatch.setattr(mirror, "_run_rclone", fake)
    sources = sample(tmp_path)
    state = tmp_path / "state"
    mirror.mirror_to_rclone(sources, "drive:library", state_dir=state, generation_id="first")
    pointer_path = tmp_path / "remote" / "library" / "current.json"
    pointer = pointer_path.read_bytes()

    def inaccessible_pointer(arguments, **kwargs):
        if arguments[0] == "copyto" and arguments[1] == "drive:library/current.json":
            raise mirror.MirrorError("access denied", returncode=1)
        return fake(arguments, **kwargs)

    monkeypatch.setattr(mirror, "_run_rclone", inaccessible_pointer)
    with pytest.raises(mirror.MirrorError, match="Cannot verify"):
        mirror.mirror_to_rclone(sources, "drive:library", state_dir=state, generation_id="second")
    assert pointer_path.read_bytes() == pointer


def test_known_pointer_disappearing_does_not_fail_open(tmp_path, monkeypatch):
    fake = FakeRclone(tmp_path / "remote")
    monkeypatch.setattr(mirror, "_run_rclone", fake)
    sources = sample(tmp_path)
    state = tmp_path / "state"
    mirror.mirror_to_rclone(sources, "drive:library", state_dir=state, generation_id="first")
    pointer_path = tmp_path / "remote" / "library" / "current.json"
    pointer_path.unlink()
    with pytest.raises(mirror.MirrorError, match="Cannot verify"):
        mirror.mirror_to_rclone(sources, "drive:library", state_dir=state, generation_id="second")
    assert not pointer_path.exists()


def test_concurrent_publications_are_serialized_and_newest_wins(tmp_path, monkeypatch):
    fake = FakeRclone(tmp_path / "remote")
    sources = sample(tmp_path)
    state = tmp_path / "state"
    older = mirror.mirror_to_directory(sources, state, generation_id="older")
    (sources["memory"] / "MEMORY.md").write_text("newer state")
    newer = mirror.mirror_to_directory(sources, state, generation_id="newer")
    calls = []

    def delayed_call(arguments, **kwargs):
        calls.append(threading.current_thread().name)
        time.sleep(0.005)
        return fake(arguments, **kwargs)

    monkeypatch.setattr(mirror, "_run_rclone", delayed_call)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(mirror.retry_to_rclone, newer["local_generation"], "drive:library")
        two = pool.submit(mirror.retry_to_rclone, older["local_generation"], "drive:library")
        receipts = [one.result(), two.result()]
    runs = [name for i, name in enumerate(calls) if i == 0 or name != calls[i - 1]]
    assert len(runs) == 2  # Entire upload/readback/advance regions do not interleave.
    pointer = json.loads((tmp_path / "remote" / "library" / "current.json").read_text())
    assert pointer["generation_id"] == "newer"
    assert all(r["remote_verified_generation"] for r in receipts)


def test_legacy_pointer_uses_referenced_manifest_capture_timestamp(tmp_path, monkeypatch):
    fake = FakeRclone(tmp_path / "remote")
    monkeypatch.setattr(mirror, "_run_rclone", fake)
    sources = sample(tmp_path)
    state = tmp_path / "state"
    older = mirror.mirror_to_directory(sources, state, generation_id="older")
    (sources["memory"] / "MEMORY.md").write_text("newer state")
    mirror.mirror_to_rclone(sources, "drive:library", state_dir=state, generation_id="newer")
    path = tmp_path / "remote" / "library" / "current.json"
    legacy = json.loads(path.read_text())
    del legacy["snapshot_created_at"]
    del legacy["remote_identity"]
    path.write_text(json.dumps(legacy))
    receipt = mirror.retry_to_rclone(older["local_generation"], "drive:library")
    assert receipt["status"] == "remote_history_verified"
    assert receipt["current_advanced"] is False
    assert json.loads(path.read_text())["generation_id"] == "newer"


def test_local_old_generation_retry_does_not_rewind_current(tmp_path):
    sources = sample(tmp_path)
    root = tmp_path / "mirror"
    mirror.mirror_to_directory(sources, root, generation_id="older")
    original = (sources["memory"] / "MEMORY.md").read_bytes()
    (sources["memory"] / "MEMORY.md").write_bytes(b"newer")
    mirror.mirror_to_directory(sources, root, generation_id="newer")
    (sources["memory"] / "MEMORY.md").write_bytes(original)
    receipt = mirror.mirror_to_directory(sources, root, generation_id="older")
    assert receipt["current_advanced"] is False
    assert receipt["local_current_generation"] == "newer"
    assert current(root)["generation_id"] == "newer"
