"""Source-freshness and recovery integration tests.

Synthetic records only. Authored by [R2] SRVP2-CODEX, 2026-10-19.
"""
import builtins
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from helix_memory.fusion import RetrievalResult

recall_module = importlib.import_module("helix_memory.recall")


class RecallIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for number in range(3):
            self.write(f"00-system/background-{number}.md", f"ordinary background notes {number} unrelated to the unique query")

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def event(self, name, event_id, text, timestamp="2026-10-18T10:00:00Z", **metadata):
        data = {"event_id": event_id, "timestamp": timestamp, "recorded_at": timestamp, **metadata}
        return self.write(name, "<!-- helix-event: " + json.dumps(data) + " -->\n" + text)

    def recall(self, query, **options):
        return recall_module.recall(query, memory_dir=self.root, **options)

    def test_clean_rebuild_and_recall_offline_default(self):
        self.event("00-system/target.md", "target", "saffronbuffer correction verified")
        stats = recall_module.rebuild_all(memory_dir=self.root, index_dir=self.root / ".derived-index")
        self.assertEqual(stats["documents"], 4)
        self.assertEqual(stats["semantic"], "disabled")
        output = self.recall("saffronbuffer", return_diagnostics=True)
        self.assertIn("target", [hit.id for hit in output["results"]])
        self.assertEqual(output["diagnostics"]["arms"]["semantic"]["status"], "disabled")

    def test_all_category_aliases_select_same_corpus(self):
        self.event("00-system/target.md", "target", "saffronbuffer decision")
        self.event("02-finance/other.md", "other", "saffronbuffer financial decision")
        for arguments in ({"category": "00-system"}, {"division": "00-system"}, {"categories": ["00-system"]}, {"divisions": ["00-system"]}):
            hits = self.recall("saffronbuffer", **arguments)
            self.assertEqual([h.id for h in hits], ["target"])
            self.assertEqual(hits[0].metadata["category"], "00-system")
        with self.assertRaises(ValueError):
            self.recall("saffronbuffer", category="00-system", division="02-finance")

    def test_explicit_missing_category_is_not_global_fallback(self):
        self.event("00-system/target.md", "target", "saffronbuffer decision")
        output = self.recall("saffronbuffer", categories=["missing-category"], return_diagnostics=True)
        self.assertEqual(output["results"], [])
        self.assertEqual(output["diagnostics"]["document_count"], 0)
        self.assertIn("not proof", output["diagnostics"]["empty_result_meaning"])
        self.assertTrue(output["diagnostics"]["degraded"])
        self.assertEqual(output["diagnostics"]["missing_categories"], ["missing-category"])

    def test_missing_root_is_not_reported_as_successful_source_read(self):
        output = recall_module.recall("absent", memory_dir=self.root / "does-not-exist", return_diagnostics=True)
        self.assertEqual(output["results"], [])
        self.assertEqual(output["diagnostics"]["source_status"], "missing")
        self.assertTrue(output["diagnostics"]["degraded"])

    def test_explicit_empty_scope_collects_nothing(self):
        self.event("00-system/target.md", "target", "saffronbuffer decision")
        self.assertEqual(self.recall("saffronbuffer", categories=[]), [])

    def test_new_file_visible_after_warm_cache(self):
        self.assertEqual(self.recall("saffronbuffer"), [])
        before = self.recall("saffronbuffer", return_diagnostics=True)["diagnostics"]["source_generation"]
        self.event("00-system/new.md", "new", "saffronbuffer arrived ten minutes later")
        after = self.recall("saffronbuffer", return_diagnostics=True)
        self.assertIn("new", [c.id for c in after["results"]])
        self.assertNotEqual(before, after["diagnostics"]["source_generation"])

    def test_appended_existing_file_visible_after_warm_cache(self):
        path = self.write("00-system/current.md", "unrelated status before the new instruction")
        self.assertEqual(self.recall("saffronbuffer"), [])
        with path.open("a", encoding="utf-8") as stream:
            stream.write("\nsaffronbuffer is the latest next action\n")
        hits = self.recall("saffronbuffer")
        self.assertTrue(any("latest next action" in hit.text for hit in hits))

    def test_changed_text_with_same_size_and_mtime_invalidates(self):
        path = self.write("00-system/current.md", "saffronbuffer approved blue decision")
        before = self.recall("saffronbuffer", return_diagnostics=True)
        original_stat = path.stat()
        path.write_text("saffronbuffer approved pink decision", encoding="utf-8")
        os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        after = self.recall("saffronbuffer", return_diagnostics=True)
        self.assertNotEqual(before["diagnostics"]["source_generation"], after["diagnostics"]["source_generation"])
        self.assertIn("pink decision", after["results"][0].text)
        self.assertNotIn("blue decision", after["results"][0].text)

    def test_separate_memory_roots_never_reuse_same_relative_id(self):
        self.event("00-system/target.md", "same-id", "saffronbuffer first corpus")
        first = self.recall("saffronbuffer", return_diagnostics=True)
        with tempfile.TemporaryDirectory() as other_root:
            other = Path(other_root)
            (other / "00-system").mkdir()
            source = other / "00-system" / "target.md"
            source.write_text('<!-- helix-event: {"event_id":"same-id"} -->\nsaffronbuffer second corpus', encoding="utf-8")
            second = recall_module.recall("saffronbuffer", memory_dir=other, return_diagnostics=True)
        self.assertIn("second corpus", second["results"][0].text)
        self.assertNotEqual(first["diagnostics"]["source_generation"], second["diagnostics"]["source_generation"])

    def test_full_late_source_span_not_1200_character_excerpt(self):
        self.event("00-system/long.md", "long", "context " * 350 + "saffronbuffer exact crucial correction at the end")
        hits = self.recall("saffronbuffer")
        self.assertIn("exact crucial correction at the end", hits[0].text)
        self.assertGreater(len(hits[0].text), 1200)
        self.assertEqual(hits[0].metadata["source_path"], str(self.root / "00-system/long.md"))

    def test_stale_arm_text_is_hydrated_from_fresh_source(self):
        self.event("00-system/target.md", "target", "saffronbuffer NEW current instruction")
        stale = RetrievalResult("target", "OLD stale instruction", 4.0, "bm25", {})
        with patch.object(recall_module, "_bm25_arm", return_value=[stale]):
            results = self.recall("saffronbuffer")
        self.assertIn("NEW current instruction", results[0].text)
        self.assertNotIn("OLD stale instruction", results[0].text)

    def test_invalid_as_of_raises_even_empty_results(self):
        for operation in (lambda: self.recall("absent", categories=[], as_of="not-a-date"), lambda: recall_module.latest_events(memory_dir=self.root, categories=[], as_of="not-a-date")):
            with self.assertRaises(ValueError):
                operation()

    def test_latest_events_recovers_task_without_indexes(self):
        self.event("00-system/a.md", "old", "first task action", timestamp="2026-10-18T10:00:00Z", task_id="task-a")
        self.event("00-system/b.md", "new", "resume from this precise next step", timestamp="2026-10-18T10:10:00Z", task_id="task-a")
        self.event("00-system/c.md", "different", "unrelated task", timestamp="2026-10-18T10:20:00Z", task_id="task-b")
        with patch.object(recall_module.BM25Index, "build", side_effect=RuntimeError("broken index")):
            docs = recall_module.latest_events(memory_dir=self.root, task_id="task-a")
        self.assertEqual([d["id"] for d in docs], ["new", "old"])
        self.assertIn("precise next step", docs[0]["text"])
        self.assertIn("source_generation", docs[0])

    def test_full_timestamp_as_of_and_explicit_correction(self):
        self.event("00-system/a.md", "old", "saffronbuffer old value", timestamp="2026-10-18T10:00:00Z")
        self.event("00-system/b.md", "new", "saffronbuffer corrected value", timestamp="2026-10-18T10:10:00Z", supersedes=["old"])
        past = self.recall("saffronbuffer", as_of="2026-10-18T10:05:00Z")
        current = self.recall("saffronbuffer", as_of="2026-10-18T10:15:00Z")
        self.assertIn("old", [d.id for d in past])
        self.assertNotIn("new", [d.id for d in past])
        self.assertNotIn("old", [d.id for d in current])
        self.assertIn("new", [d.id for d in current])
        dedup = self.recall("saffronbuffer", dedup_mode=True)
        self.assertEqual({d.id for d in dedup}, {"old", "new"})

    def test_unrelated_update_does_not_suppress_existing_instruction(self):
        self.event("00-system/a.md", "preserve", "ADMIN PROJECT saffronbuffer never remove history", timestamp="2026-10-18T10:00:00Z")
        self.event("00-system/b.md", "website", "ADMIN PROJECT updated website theme", timestamp="2026-10-18T10:10:00Z")
        self.assertIn("preserve", [d.id for d in self.recall("saffronbuffer")])

    def test_known_at_does_not_leak_future_correction_metadata(self):
        self.event("00-system/a.md", "old", "saffronbuffer old value", timestamp="2026-10-18T10:00:00Z")
        self.event("00-system/b.md", "new", "saffronbuffer backdated correction", timestamp="2026-10-18T11:00:00Z", valid_from="2026-10-18T10:05:00Z", supersedes=["old"])
        hits = self.recall("saffronbuffer", as_of="2026-10-18T10:10:00Z", known_at="2026-10-18T10:15:00Z")
        self.assertEqual([hit.id for hit in hits], ["old"])
        self.assertIsNone(hits[0].metadata["valid_until"])
        self.assertIsNone(hits[0].metadata["superseded_by"])
        self.assertEqual(hits[0].metadata["supersessions"], [])

    def test_future_matches_cannot_starve_eligible_historical_result(self):
        self.event("00-system/old.md", "old-eligible", "saffronbuffer " + "background " * 300)
        for number in range(12):
            self.event(f"00-system/future-{number}.md", f"future-{number:02}", "saffronbuffer " * 6,
                       timestamp="2026-10-18T10:10:00Z")
        # Warm the full-corpus indexes before reconstructing an older view.
        self.recall("saffronbuffer", per_arm_cap=2, as_of="2026-10-18T10:15:00Z")
        historic = self.recall("saffronbuffer", per_arm_cap=2, as_of="2026-10-18T10:05:00Z", return_diagnostics=True)
        self.assertEqual([hit.id for hit in historic["results"]], ["old-eligible"])
        self.assertEqual(historic["diagnostics"]["document_count"], 16)
        self.assertEqual(historic["diagnostics"]["eligible_document_count"], 4)
        self.assertEqual(historic["diagnostics"]["excluded_by_validity"], 12)

    def test_later_recorded_matches_cannot_starve_known_at_result(self):
        self.event("00-system/old.md", "old-eligible", "saffronbuffer " + "background " * 300)
        for number in range(12):
            self.event(f"00-system/later-{number}.md", f"later-{number:02}", "saffronbuffer " * 6,
                       timestamp="2026-10-18T11:00:00Z", valid_from="2026-10-18T09:30:00Z")
        known = self.recall("saffronbuffer", per_arm_cap=2, as_of="2026-10-18T10:15:00Z", known_at="2026-10-18T10:05:00Z")
        self.assertEqual([hit.id for hit in known], ["old-eligible"])

    def test_superseded_matches_filtered_before_arm_caps(self):
        self.event("00-system/current.md", "eligible", "saffronbuffer " + "background " * 300)
        superseded = []
        for number in range(12):
            identifier = f"superseded-{number:02}"
            superseded.append(identifier)
            self.event(f"00-system/superseded-{number}.md", identifier, "saffronbuffer " * 6)
        # This correction itself does not match the query. Validity must still
        # use the complete source, rather than only ranked/matching documents.
        self.event("00-system/correction.md", "correction", "retire earlier proposals", timestamp="2026-10-18T10:10:00Z", supersedes=superseded)
        hits = self.recall("saffronbuffer", per_arm_cap=2, as_of="2026-10-18T10:15:00Z")
        self.assertEqual([hit.id for hit in hits], ["eligible"])

    def test_all_retrieval_arms_receive_prefiltered_visibility_view(self):
        self.event("00-system/old.md", "eligible", "saffronbuffer current value")
        self.event("00-system/future.md", "future", "saffronbuffer future value", timestamp="2026-10-18T11:00:00Z")
        seen = {}
        def capture(name, answer):
            def inspect(state, *args, **kwargs):
                seen[name] = set(state["by_id"])
                return answer
            return inspect
        with patch.object(recall_module, "_semantic_arm", side_effect=capture("semantic", ([], []))), patch.object(recall_module, "_bm25_arm", side_effect=capture("bm25", [])), patch.object(recall_module, "_graph_arm", side_effect=capture("graph", ([], []))), patch.object(recall_module, "_temporal_arm", side_effect=capture("temporal", ([], "abstained"))):
            self.recall("saffronbuffer", include_semantic=True, as_of="2026-10-18T10:15:00Z")
        self.assertEqual(set(seen), {"semantic", "bm25", "graph", "temporal"})
        for eligible_ids in seen.values():
            self.assertIn("eligible", eligible_ids)
            self.assertNotIn("future", eligible_ids)

    def test_degraded_arms_report_health_with_no_matching_candidates(self):
        with patch.object(recall_module, "_bm25_arm", side_effect=RuntimeError("keyword down")), patch.object(recall_module, "_graph_arm", side_effect=RuntimeError("graph down")), patch.object(recall_module, "_temporal_arm", side_effect=RuntimeError("temporal down")):
            output = self.recall("absent", return_diagnostics=True)
        self.assertEqual(output["results"], [])
        self.assertTrue(output["diagnostics"]["degraded"])
        self.assertEqual(output["diagnostics"]["arms"]["bm25"]["status"], "failed")
        self.assertEqual(output["diagnostics"]["document_count"], 3)

    def test_default_does_not_import_semantic_dependencies(self):
        original_import = builtins.__import__
        def no_semantic(name, *args, **kwargs):
            if name.startswith(("sentence_transformers", "chromadb")) or name.endswith("semantic_index"):
                raise AssertionError("Core recall tried importing optional semantic dependencies")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=no_semantic):
            self.recall("unrelated")

    def test_latest_tie_is_deterministic(self):
        self.event("00-system/a.md", "alpha", "first event")
        self.event("00-system/b.md", "beta", "second event")
        one = recall_module.latest_events(memory_dir=self.root, limit=2)
        two = recall_module.latest_events(memory_dir=self.root, limit=2)
        self.assertEqual([d["id"] for d in one], ["beta", "alpha"])
        self.assertEqual([d["id"] for d in one], [d["id"] for d in two])


if __name__ == "__main__":
    unittest.main()
