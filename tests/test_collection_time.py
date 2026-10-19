"""Regression evidence for collection and time repair, [R2] SRVP2-CODEX."""
from datetime import date, datetime, timezone
import json
from pathlib import Path

import pytest

from helix_memory.collector import collect_documents, normalize_categories
from helix_memory.temporal_filter import (
    date_from_qpulse_path, normalize_timestamp, timestamp_from_path,
)
from helix_memory.temporal_validity import (
    build_validity_index, filter_valid, is_valid, load_validity,
    save_validity, validity_for,
)


def write(root, relative, text):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return target


def event(root, name, identifier, timestamp, text, **metadata):
    metadata = {"event_id": identifier, "recorded_at": timestamp,
                "valid_from": timestamp, **metadata}
    return write(root, name, f'<!-- helix-event: {json.dumps(metadata)} -->\n{text}')


def test_default_collection_includes_root_and_category_files(tmp_path):
    write(tmp_path, "MEMORY.md", "Startup rules")
    write(tmp_path, "00-system/qpulse/2026-10-19-1000.md", "Keep originals")
    docs = collect_documents(str(tmp_path))
    assert len(docs) == 2
    assert {d["category"] for d in docs} == {".", "00-system"}
    assert all(d["category"] == d["division"] for d in docs)
    assert all(Path(d["source_path"]).is_absolute() for d in docs)


def test_nonrecursive_default_includes_root_and_direct_category_files(tmp_path):
    write(tmp_path, "MEMORY.md", "Startup")
    write(tmp_path, "00-system/INDEX.md", "Index")
    write(tmp_path, "00-system/deeper/record.md", "Deep")
    assert {d["text"] for d in collect_documents(str(tmp_path), recursive=False)} == {"Startup", "Index"}


@pytest.mark.parametrize("scope", ["categories", "divisions", "category", "division"])
def test_scope_aliases_all_work(tmp_path, scope):
    write(tmp_path, "00-system/README.md", "Rules")
    write(tmp_path, "02-finance/README.md", "Finances")
    value = "00-system" if scope in ("category", "division") else ["00-system"]
    assert [d["division"] for d in collect_documents(str(tmp_path), **{scope: value})] == ["00-system"]


def test_scope_alias_conflicts_fail_loudly(tmp_path):
    with pytest.raises(ValueError, match="Conflicting"):
        collect_documents(str(tmp_path), categories=["00-system"], divisions=["02-finance"])
    assert normalize_categories(categories=["b", "a"], divisions=["a", "b"]) == ["b", "a"]
    assert collect_documents(str(tmp_path), categories=[]) == []


@pytest.mark.parametrize("scope", ["../outside", "/tmp", "a/../../outside"])
def test_scope_escape_rejected(tmp_path, scope):
    with pytest.raises(ValueError, match="escape"):
        collect_documents(str(tmp_path), category=scope)


def test_symlink_escape_and_hidden_directories_not_collected(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    outside = write(tmp_path, "outside/secret.md", "Do not index")
    (memory / "link.md").symlink_to(outside)
    (memory / "linked").symlink_to(outside.parent, target_is_directory=True)
    write(memory, ".hidden/secret.md", "Hidden")
    write(memory, "safe.md", "Visible")
    assert [d["text"] for d in collect_documents(str(memory))] == ["Visible"]
    with pytest.raises(ValueError, match="outside"):
        collect_documents(str(memory), categories=["linked"])


def test_section_ids_do_not_shift_on_insert_and_text_is_not_truncated(tmp_path):
    original = "# Notes\n\n## Archive\n" + "Preserve the full original. " * 80 + "\n## Work\nKeep working.\n"
    source = write(tmp_path, "00-system/notes.md", original)
    before = collect_documents(str(tmp_path))
    source.write_text(original.replace("## Archive", "## Newly inserted\nNew topic.\n## Archive", 1))
    after = collect_documents(str(tmp_path))
    archive_before = next(d for d in before if d["text"].startswith("## Archive"))
    archive_after = next(d for d in after if d["text"].startswith("## Archive"))
    assert archive_before["id"] == archive_after["id"]
    assert archive_before["text"] == archive_after["text"]
    assert len(archive_before["text"]) > 1500
    assert "".join(d["text"] for d in before) == original


def test_immutable_event_metadata_and_full_body_preserved(tmp_path):
    source = event(tmp_path, "00-system/qpulse/event.md", "ev:1", "2026-10-19T10:12:30-07:00",
                   "## Decision\n" + "content " * 500, task_id="task:1", supersedes=["ev:old"])
    docs = collect_documents(str(tmp_path))
    assert len(docs) == 1
    doc = docs[0]
    assert doc["id"] == "ev:1"
    assert doc["text"] == source.read_text()
    assert doc["recorded_at"] == "2026-10-19T17:12:30+00:00"
    assert doc["task_id"] == "task:1"
    assert doc["supersedes"] == ["ev:old"]
    assert doc["metadata"]["event_id"] == "ev:1"


def test_recorded_and_effective_times_are_distinct(tmp_path):
    event(tmp_path, "00-system/event.md", "ev:late", "2026-10-19T12:00:00Z", "Backdated correction",
          valid_from="2026-10-19T09:30:00Z")
    doc = collect_documents(str(tmp_path))[0]
    assert doc["recorded_at"] == "2026-10-19T12:00:00+00:00"
    assert doc["valid_from"] == "2026-10-19T09:30:00+00:00"


def test_event_timestamp_overrides_date_only_filename(tmp_path):
    event(tmp_path, "00-system/2026-10-19.md", "ev:fresh", "2026-10-19T12:37:12Z", "Fresh")
    doc = collect_documents(str(tmp_path))[0]
    assert doc["timestamp"] == "2026-10-19T12:37:12+00:00"


@pytest.mark.parametrize("filename, expected", [
    ("2026-10-19-1030-pulse.md", "2026-10-19T10:30:00+00:00"),
    ("2026-10-19-1030-45-pulse.md", "2026-10-19T10:30:45+00:00"),
    ("2026-10-19T10:30:45-07:00.md", "2026-10-19T17:30:45+00:00"),
    ("2026-10-19.md", "2026-10-19T00:00:00+00:00"),
])
def test_filename_timestamps_keep_minutes_seconds_and_timezone(filename, expected):
    assert timestamp_from_path(filename).isoformat() == expected
    assert date_from_qpulse_path(filename) == date(2026, 10, 19)
    assert timestamp_from_path("/2026-10-19/no-date.md") is None


@pytest.mark.parametrize("value", ["not a date", "2026-13-19", "2026-10-19T25:30:00Z", "", 19])
def test_invalid_query_times_raise_even_with_empty_results(value):
    with pytest.raises(ValueError):
        filter_valid([], {}, value)
    with pytest.raises(ValueError):
        is_valid({}, value)


def test_bad_filename_time_and_bad_event_metadata_are_errors(tmp_path):
    with pytest.raises(ValueError):
        timestamp_from_path("2026-10-19-2560.md")
    write(tmp_path, "malformed.md", '<!-- helix-event: {broken} -->\nDecision')
    with pytest.raises(ValueError, match="JSON"):
        collect_documents(str(tmp_path))


def test_unrelated_update_never_hides_older_rule():
    docs = [
        {"id": "rule", "division": "system", "date": "2026-10-18", "text": "AGENT-08 and AGENT-01 preserve originals"},
        {"id": "web", "division": "system", "date": "2026-10-19", "text": "Updated website by AGENT-08 and AGENT-01"},
    ]
    validity = build_validity_index(docs)
    assert validity["rule"]["valid_until"] is None
    assert filter_valid(["rule", "web"], validity, "2026-10-19T12:00:00Z") == ["rule", "web"]


def test_explicit_supersession_keeps_minute_precision(tmp_path):
    event(tmp_path, "00-system/a.md", "ev:old", "2026-10-19T10:00:00Z", "Previous decision")
    event(tmp_path, "00-system/b.md", "ev:new", "2026-10-19T10:12:00Z", "Correction", supersedes="ev:old")
    validity = build_validity_index(collect_documents(str(tmp_path)))
    assert filter_valid(["ev:old", "ev:new"], validity, "2026-10-19T10:11:59Z") == ["ev:old"]
    assert filter_valid(["ev:old", "ev:new"], validity, "2026-10-19T10:12:00Z") == ["ev:new"]
    assert validity["ev:old"]["superseded_by"] == "ev:new"


def test_known_at_reconstructs_knowledge_before_late_correction():
    docs = [
        {"id": "old", "recorded_at": "2026-10-19T09:00:00Z", "valid_from": "2026-10-19T09:00:00Z"},
        {"id": "new", "recorded_at": "2026-10-19T12:00:00Z", "valid_from": "2026-10-19T09:30:00Z", "supersedes": ["old"]},
    ]
    validity = build_validity_index(docs)
    assert filter_valid(["old", "new"], validity, "2026-10-19T10:00:00Z", known_at="2026-10-19T10:00:00Z") == ["old"]
    assert filter_valid(["old", "new"], validity, "2026-10-19T10:00:00Z", known_at="2026-10-19T13:00:00Z") == ["new"]
    projected = validity_for("old", validity, known_at="2026-10-19T10:00:00Z")
    assert projected["valid_until"] is None
    assert projected["superseded_by"] is None
    assert projected["supersessions"] == []
    assert validity["old"]["superseded_by"] == "new"


def test_naive_and_date_timestamps_documented_utc_fallback():
    assert normalize_timestamp(datetime(2026, 10, 19, 10, 30)).tzinfo == timezone.utc
    assert normalize_timestamp(date(2026, 10, 19)).isoformat() == "2026-10-19T00:00:00+00:00"


def test_persist_validity_roundtrip_and_missing_defaults(tmp_path):
    validity = build_validity_index([{"id": "one", "date": "2026-10-19"}])
    path = str(tmp_path / "nested/validity.json")
    save_validity(validity, path)
    assert load_validity(path) == validity
    assert load_validity(str(tmp_path / "missing")) == {}
    assert is_valid(validity_for("missing", validity), "2026-10-19")


def test_store_crlf_payload_collected_without_newline_translation(tmp_path):
    from helix_memory.memory_store import MemoryStore

    body = "Original decision.\r\n\r\nPreserve each original newline.\r\n"
    receipt = MemoryStore(tmp_path).remember(
        body, area="00-system", actor="[R2] SRVP2-CODEX", task_id="crlf-regression"
    )
    doc = collect_documents(str(tmp_path))[0]
    assert doc["text"] == Path(receipt["path"]).read_bytes().decode("utf-8")
    assert doc["text"].split("\n\n", 1)[1] == body
    assert doc["id"] == receipt["event_id"]


@pytest.mark.parametrize("route", ["collect", "recall", "latest"])
def test_tampered_store_body_rejected_by_every_retrieval_route(tmp_path, route):
    from helix_memory.memory_store import MemoryIntegrityError, MemoryStore
    from helix_memory.recall import latest_events, recall

    receipt = MemoryStore(tmp_path).remember(
        "Original checksum-sensitive decision.", area="00-system",
        actor="[R2] SRVP2-CODEX", task_id="tamper-regression",
    )
    source = Path(receipt["path"])
    source.write_bytes(source.read_bytes().replace(b"Original checksum", b"Tampered checksum"))
    with pytest.raises(MemoryIntegrityError):
        if route == "collect":
            collect_documents(str(tmp_path))
        elif route == "recall":
            recall("checksum-sensitive decision", memory_dir=str(tmp_path))
        else:
            latest_events(memory_dir=str(tmp_path), task_id="tamper-regression")


@pytest.mark.parametrize("mutation", ["unsupported_schema", "missing_checksum"])
def test_recognized_store_header_never_downgraded_to_legacy(tmp_path, mutation):
    from helix_memory.memory_store import MemoryIntegrityError, MemoryStore

    receipt = MemoryStore(tmp_path).remember(
        "Store-format decision.", area="00-system",
        actor="[R2] SRVP2-CODEX", task_id="schema-regression",
    )
    source = Path(receipt["path"])
    header, body = source.read_bytes().decode("utf-8").split("\n\n", 1)
    metadata = json.loads(header[len("<!-- helix-event: "):-len(" -->")])
    if mutation == "unsupported_schema":
        metadata["schema_version"] = 999
    else:
        del metadata["payload_sha256"]
    source.write_bytes(f"<!-- helix-event: {json.dumps(metadata)} -->\n\n{body}".encode("utf-8"))
    with pytest.raises(MemoryIntegrityError):
        collect_documents(str(tmp_path))
