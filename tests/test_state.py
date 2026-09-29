"""Tests for pipeline.state — persistent fetch-state (deterministic layer)."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from pipeline.state import (
    SourceState,
    StateError,
    apply_result,
    classify,
    commit_state,
    load_state,
    save_state,
    update_state,
)

T0 = "2026-09-26T12:00:00+00:00"
T1 = "2026-09-26T13:00:00+00:00"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ok_result(url: str, content: str, fetched_at: str = T0,
              strategy: str = "tvly-basic") -> dict:
    return {
        "url": url, "strategy": strategy, "content_type": "markdown",
        "title": "page title", "sha256": sha(content), "fetched_at": fetched_at,
        "content": content, "ok": True,
    }


def err_result(url: str) -> dict:
    return {"url": url, "ok": False, "error": "all fetch strategies failed"}


class StateFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "fetch_state.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_is_empty_state(self):
        self.assertEqual(load_state(self.path), {})

    def test_malformed_json_is_an_error_not_silent_reset(self):
        self.path.write_text("{oops", encoding="utf-8")
        with self.assertRaises(StateError):
            load_state(self.path)

    def test_non_object_json_is_an_error(self):
        self.path.write_text("[]", encoding="utf-8")
        with self.assertRaises(StateError):
            load_state(self.path)

    def test_roundtrip_preserves_all_fields(self):
        states = {
            "https://a.example/one": SourceState(
                url="https://a.example/one", status="ok", fetched_at=T0,
                sha256=sha("hello"), strategy="tvly-advanced"),
            "https://b.example/two": SourceState(
                url="https://b.example/two", status="error", fetched_at=T1,
                error="all fetch strategies failed"),
        }
        save_state(self.path, states)
        self.assertEqual(load_state(self.path), states)

    def test_file_is_human_readable_json(self):
        save_state(self.path, {"https://a.example/one": SourceState(
            url="https://a.example/one", status="ok", fetched_at=T0,
            sha256=sha("hello"), strategy="direct")})
        text = self.path.read_text(encoding="utf-8")
        json.loads(text)  # valid JSON
        self.assertIn('"sha256"', text)
        self.assertIn('\n  "https://a.example/one"', text)  # 2-space indent

    def test_save_is_deterministic_regardless_of_insertion_order(self):
        a = SourceState(url="https://a", status="ok", fetched_at=T0, sha256="aa")
        b = SourceState(url="https://b", status="ok", fetched_at=T0, sha256="bb")
        save_state(self.path, {"https://a": a, "https://b": b})
        first = self.path.read_bytes()
        save_state(self.path, {"https://b": b, "https://a": a})
        self.assertEqual(first, self.path.read_bytes())

    def test_save_leaves_no_temp_file_behind(self):
        save_state(self.path, {})
        self.assertFalse((self.path.parent / (self.path.name + ".tmp")).exists())


class ClassifyTests(unittest.TestCase):
    def test_failed_fetch_is_error(self):
        self.assertEqual(classify(None, err_result("https://a")), "error")

    def test_no_stored_record_is_new(self):
        self.assertEqual(classify(None, ok_result("https://a", "hello")), "new")

    def test_stored_error_only_is_new(self):
        stored = SourceState(url="https://a", status="error", fetched_at=T0)
        self.assertEqual(classify(stored, ok_result("https://a", "hello")), "new")

    def test_same_hash_is_unchanged(self):
        stored = SourceState(url="https://a", status="ok", fetched_at=T0,
                             sha256=sha("hello"))
        self.assertEqual(classify(stored, ok_result("https://a", "hello")),
                         "unchanged")

    def test_different_hash_is_changed(self):
        stored = SourceState(url="https://a", status="ok", fetched_at=T0,
                             sha256=sha("hello"))
        self.assertEqual(classify(stored, ok_result("https://a", "hello v2")),
                         "changed")


class ApplyTests(unittest.TestCase):
    def test_ok_result_stages_hash_as_pending(self):
        states: dict = {}
        verdict = apply_result(states, ok_result("https://a", "hello"), T0)
        self.assertEqual(verdict, "new")
        # Two-phase commit: a fetched hash is PENDING until commit_state().
        self.assertIsNone(states["https://a"].sha256)
        self.assertEqual(states["https://a"].pending_sha256, sha("hello"))
        self.assertEqual(states["https://a"].status, "ok")
        self.assertEqual(states["https://a"].pending_strategy, "tvly-basic")
        self.assertEqual(states["https://a"].pending_fetched_at, T0)

    def test_error_result_records_failure(self):
        states: dict = {}
        verdict = apply_result(states, err_result("https://a"), T0)
        self.assertEqual(verdict, "error")
        self.assertEqual(states["https://a"].status, "error")
        self.assertEqual(states["https://a"].error, "all fetch strategies failed")
        self.assertIsNone(states["https://a"].sha256)

    def test_error_after_commit_preserves_last_known_hash(self):
        states: dict = {}
        apply_result(states, ok_result("https://a", "hello"), T0)
        states["https://a"] = SourceState(  # simulate the post-publish commit
            url="https://a", status="ok", fetched_at=T0,
            sha256=sha("hello"), strategy="tvly-basic")
        apply_result(states, err_result("https://a"), T1)
        self.assertEqual(states["https://a"].sha256, sha("hello"))
        self.assertEqual(states["https://a"].status, "error")
        self.assertEqual(states["https://a"].fetched_at, T1)

    def test_success_after_error_is_new_not_changed(self):
        states: dict = {}
        apply_result(states, err_result("https://a"), T0)
        verdict = apply_result(states, ok_result("https://a", "hello"), T1)
        self.assertEqual(verdict, "new")


class UpdateStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "fetch_state.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_batch_records_verdicts_and_persists(self):
        results = [ok_result("https://a", "hello"), err_result("https://b")]
        verdicts = update_state(self.path, results, T0)
        self.assertEqual(verdicts, {"https://a": "new", "https://b": "error"})
        self.assertEqual(set(load_state(self.path)), {"https://a", "https://b"})

    def test_second_run_identical_content_is_unchanged_after_commit(self):
        update_state(self.path, [ok_result("https://a", "hello")], T0)
        commit_state(self.path)  # publish succeeded
        verdicts = update_state(
            self.path, [ok_result("https://a", "hello", fetched_at=T1)], T1)
        self.assertEqual(verdicts, {"https://a": "unchanged"})
        self.assertEqual(load_state(self.path)["https://a"].fetched_at, T1)

    def test_without_commit_refetch_stays_new(self):
        # The crash-safety property at update_state level: no commit after a
        # failed run means the next fetch of the same content is "new" again.
        update_state(self.path, [ok_result("https://a", "hello")], T0)
        verdicts = update_state(
            self.path, [ok_result("https://a", "hello", fetched_at=T1)], T1)
        self.assertEqual(verdicts, {"https://a": "new"})

    def test_second_run_modified_content_is_changed_after_commit(self):
        update_state(self.path, [ok_result("https://a", "hello")], T0)
        commit_state(self.path)
        verdicts = update_state(
            self.path, [ok_result("https://a", "hello v2", fetched_at=T1)], T1)
        self.assertEqual(verdicts, {"https://a": "changed"})


if __name__ == "__main__":
    unittest.main()
