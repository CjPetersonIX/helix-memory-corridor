"""Fresh-process CLI regressions. Authored by [R2] SRVP2-CODEX."""
import json
import os
from pathlib import Path
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]


def call(tmp_path, *arguments, expected=0, stdin=None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL"}}
    env.update(HOME=str(home), PYTHONPATH=str(SOURCE), PYTHONDONTWRITEBYTECODE="1",
               HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    result = subprocess.run([sys.executable, "-B", "-m", "helix_memory", *map(str, arguments)],
                            env=env, cwd=tmp_path, input=stdin, text=True,
                            capture_output=True, timeout=30)
    assert result.returncode == expected, result.stdout + result.stderr
    return result


def remember(tmp_path):
    root = tmp_path / "memory"
    result = call(tmp_path, "remember", "--memory-dir", root, "--area", "work",
                  "--actor", "[R2] EXAMPLE-AGENT", "--task-id", "orion",
                  "--idempotency-key", "orion-1", "--text", "Orion uses ultramarine.")
    return root, json.loads(result.stdout)


def test_capture_and_recall_survive_separate_processes(tmp_path):
    root, receipt = remember(tmp_path)
    assert receipt["durable"] and receipt["readback_verified"]
    result = json.loads(call(tmp_path, "Orion ultramarine", "--memory-dir", root).stdout)
    assert receipt["event_id"] in {c["id"] for c in result["results"]}
    assert result["diagnostics"]["arms"]["semantic"]["status"] == "disabled"
    latest = json.loads(call(tmp_path, "latest", "--memory-dir", root, "--task-id", "orion").stdout)
    assert latest[0]["event_id"] == receipt["event_id"]


def test_documented_index_and_legacy_scope_flags(tmp_path):
    root, _ = remember(tmp_path)
    for flag in ("--category", "--categories", "--division", "--divisions"):
        result = json.loads(call(tmp_path, "index", "--memory-dir", root, flag, "work", "--no-semantic").stdout)
        assert result["documents"] == 1
        assert result["semantic"] == "disabled"


def test_resume_preserves_handoff_and_recovers_in_fresh_process(tmp_path):
    root, receipt = remember(tmp_path)
    handoff = tmp_path / "Living_Handoff.html"
    handoff.write_text("<html><body><h1>Existing board</h1><p>Keep this.</p></body></html>")
    call(tmp_path, "resume", "set", "--memory-dir", root, "--handoff", handoff,
         "--actor", "[R2] EXAMPLE-AGENT", "--task-id", "orion", "--goal", "Finish Orion",
         "--next-step", "Review the draft", "--last-event-id", receipt["event_id"],
         "--expected-revision", "0")
    restored = json.loads(call(tmp_path, "resume", "show", "--memory-dir", root, "--handoff", handoff).stdout)
    assert restored["state"]["next_step"] == "Review the draft"
    assert restored["state"]["last_event_id"] == receipt["event_id"]
    assert "<h1>Existing board</h1><p>Keep this.</p>" in handoff.read_text()
    conflict = call(tmp_path, "resume", "set", "--memory-dir", root, "--handoff", handoff,
                    "--actor", "[R2] EXAMPLE-AGENT", "--task-id", "orion", "--goal", "Wrong revision",
                    "--expected-revision", "0", expected=1)
    assert json.loads(conflict.stderr)["type"] == "RevisionConflict"


def test_cli_whole_corpus_mirror_and_empty_directory_restore(tmp_path):
    root, receipt = remember(tmp_path)
    handoff = tmp_path / "handoff.html"
    handoff.write_text("<html><body>Canonical handoff</body></html>")
    backup = tmp_path / "backup"
    mirrored = json.loads(call(tmp_path, "mirror", "--source", f"memory={root}",
                               "--source", f"workspace/user/files/Living_Handoff.html={handoff}",
                               "--destination", backup).stdout)
    assert mirrored["status"] == "local_verified"
    generation = mirrored["local_generation"]
    assert json.loads(call(tmp_path, "verify", generation).stdout)["verified"]
    restored = tmp_path / "restore"
    assert json.loads(call(tmp_path, "restore", generation, restored).stdout)["restored"]
    assert (restored / "memory" / receipt["relative_path"]).read_bytes() == Path(receipt["path"]).read_bytes()
    assert (restored / "workspace/user/files/Living_Handoff.html").read_bytes() == handoff.read_bytes()


def test_invalid_asof_and_conflicting_semantic_flags_are_errors(tmp_path):
    root, _ = remember(tmp_path)
    bad = call(tmp_path, "recall", "Orion", "--memory-dir", root, "--as-of", "invalid", expected=1)
    assert json.loads(bad.stderr)["type"] == "ValueError"
    conflict = call(tmp_path, "index", "--memory-dir", root, "--semantic", "--no-semantic", expected=1)
    assert json.loads(conflict.stderr)["type"] == "ValueError"


def test_doctor_does_not_claim_provider_hooks_or_remote_verification(tmp_path):
    root = tmp_path / "absent"
    report = json.loads(call(tmp_path, "doctor", "--memory-dir", root).stdout)
    assert not report["source_exists"]
    assert not root.exists()
    assert "separately" in report["capture_mode"]
    assert "not checked" in report["mirror_status"]


def test_stdin_capture_preserves_line_breaks(tmp_path):
    root = tmp_path / "memory"
    text = "First line.\n\nSecond line.\n"
    receipt = json.loads(call(tmp_path, "remember", "--memory-dir", root, "--area", "work",
                              "--actor", "[R2] EXAMPLE-AGENT", "--task-id", "stdin",
                              "--stdin", stdin=text).stdout)
    assert Path(receipt["path"]).read_text().endswith(text)
