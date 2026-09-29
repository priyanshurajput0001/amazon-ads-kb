"""Tests for fetch_many_with_state — fully offline via an injected fetcher."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from pipeline.fetch import (
    FetchError,
    FetchResult,
    fetch_many,
    fetch_many_with_state,
    fetch_url,
)
from pipeline.state import commit_state, load_state

T0 = "2026-09-26T12:00:00+00:00"
T1 = "2026-09-26T13:00:00+00:00"
T2 = "2026-09-26T14:00:00+00:00"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ok_result(url: str, content: str, fetched_at: str,
              strategy: str = "tvly-basic") -> dict:
    return {
        "url": url, "strategy": strategy, "content_type": "markdown",
        "title": "page title", "sha256": sha(content), "fetched_at": fetched_at,
        "content": content, "ok": True,
    }


class ScriptedFetcher:
    """Stands in for fetch_many; returns one canned batch and records the call."""

    def __init__(self, batch):
        self.batch = batch
        self.calls = []

    def __call__(self, urls, fetched_at):
        self.calls.append((list(urls), fetched_at))
        return self.batch


class FetchManyWithStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self._tmp.name) / "fetch_state.json"
        self.cache_path = Path(self._tmp.name) / "cache"

    def tearDown(self):
        self._tmp.cleanup()

    def run_batch(self, urls, fetched_at, batch):
        fetcher = ScriptedFetcher(batch)
        results = fetch_many_with_state(
            urls, fetched_at, state_path=self.state_path, fetcher=fetcher,
            cache_path=self.cache_path)
        return results, fetcher

    def test_first_run_marks_every_url_new(self):
        results, _ = self.run_batch(["https://a"], T0, [ok_result("https://a", "hello", T0)])
        self.assertEqual(results[0]["change"], "new")
        self.assertEqual(results[0]["sha256"], sha("hello"))
        self.assertTrue(self.state_path.exists())

    def test_fetcher_receives_urls_and_timestamp_unchanged(self):
        _, fetcher = self.run_batch(["https://a", "https://b"], T0,
                                    [ok_result("https://a", "hello", T0),
                                     ok_result("https://b", "world", T0)])
        self.assertEqual(fetcher.calls, [(["https://a", "https://b"], T0)])

    def test_unchanged_on_identical_refetch(self):
        self.run_batch(["https://a"], T0, [ok_result("https://a", "hello", T0)])
        commit_state(self.state_path)  # the orchestrator commits after publish
        results, _ = self.run_batch(["https://a"], T1, [ok_result("https://a", "hello", T1)])
        self.assertEqual(results[0]["change"], "unchanged")

    def test_changed_on_modified_content(self):
        self.run_batch(["https://a"], T0, [ok_result("https://a", "hello", T0)])
        commit_state(self.state_path)
        results, _ = self.run_batch(["https://a"], T1, [ok_result("https://a", "hello v2", T1)])
        self.assertEqual(results[0]["change"], "changed")

    def test_error_then_success_is_new_again(self):
        results1, _ = self.run_batch(
            ["https://a"], T0,
            [{"url": "https://a", "ok": False, "error": "all fetch strategies failed"}])
        results2, _ = self.run_batch(["https://a"], T1, [ok_result("https://a", "hello", T1)])
        self.assertEqual(results1[0]["change"], "error")
        self.assertEqual(results2[0]["change"], "new")

    def test_legacy_fetch_api_untouched(self):
        # The pre-existing surface must remain importable and unchanged.
        self.assertTrue(callable(fetch_url))
        self.assertTrue(callable(fetch_many))
        self.assertTrue(callable(FetchResult))
        self.assertTrue(issubclass(FetchError, Exception))


class TwoPhaseCommitTests(unittest.TestCase):
    """New/changed hashes are PENDING until commit_state() promotes them after
    a successful publish — the crash-safety contract of pipeline/state.py."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self._tmp.name) / "fetch_state.json"
        self.cache_path = Path(self._tmp.name) / "cache"

    def tearDown(self):
        self._tmp.cleanup()

    def run_batch(self, urls, fetched_at, batch):
        fetcher = ScriptedFetcher(batch)
        return fetch_many_with_state(
            urls, fetched_at, state_path=self.state_path, fetcher=fetcher,
            cache_path=self.cache_path)

    def state(self, url):
        return load_state(self.state_path)[url]

    def test_new_hash_is_pending_not_committed(self):
        self.run_batch(["https://a"], T0, [ok_result("https://a", "hello", T0)])
        entry = self.state("https://a")
        self.assertEqual(entry.pending_sha256, sha("hello"))
        self.assertIsNone(entry.sha256)  # nothing committed yet
        self.assertEqual(entry.current_content_sha256, sha("hello"))

    def test_publish_failure_then_refetch_is_changed_again(self):
        # Regression (review Part 7): fetch -> (publish fails, no commit)
        # -> source fetched again -> the run must NOT stop at Fetch.
        self.run_batch(["https://a"], T0, [ok_result("https://a", "hello", T0)])
        # no commit_state: simulate a publish failure ending the run here
        results = self.run_batch(["https://a"], T1, [ok_result("https://a", "hello", T1)])
        # The content is still pending-only, so classify vs COMMITTED
        # (absent) must say "new" again:
        self.assertEqual(results[0]["change"], "new")
        self.assertIsNone(self.state("https://a").sha256)

    def test_changed_hash_stays_pending_until_committed(self):
        self.run_batch(["https://a"], T0, [ok_result("https://a", "hello", T0)])
        commit_state(self.state_path)
        self.assertEqual(self.state("https://a").sha256, sha("hello"))

        self.run_batch(["https://a"], T1, [ok_result("https://a", "hello v2", T1)])
        entry = self.state("https://a")
        self.assertEqual(entry.sha256, sha("hello"))       # committed: old
        self.assertEqual(entry.pending_sha256, sha("hello v2"))  # staged: new

        # Publish fails again (no commit): re-fetching the new content is
        # still "changed", not "unchanged".
        results = self.run_batch(["https://a"], T2,
                                 [ok_result("https://a", "hello v2", T2)])
        self.assertEqual(results[0]["change"], "changed")

    def test_commit_state_promotes_pending_to_committed(self):
        self.run_batch(["https://a"], T0, [ok_result("https://a", "hello", T0)])
        promoted = commit_state(self.state_path, ["https://a"])
        self.assertEqual(promoted, ["https://a"])
        entry = self.state("https://a")
        self.assertEqual(entry.sha256, sha("hello"))
        self.assertIsNone(entry.pending_sha256)
        self.assertEqual(entry.strategy, "tvly-basic")
        self.assertEqual(entry.fetched_at, T0)

    def test_commit_state_scopes_to_requested_urls(self):
        self.run_batch(["https://a", "https://b"], T0,
                       [ok_result("https://a", "hello", T0),
                        ok_result("https://b", "world", T0)])
        promoted = commit_state(self.state_path, ["https://a"])
        self.assertEqual(promoted, ["https://a"])
        self.assertEqual(self.state("https://a").sha256, sha("hello"))
        self.assertIsNone(self.state("https://b").sha256)  # still pending only

    def test_unchanged_refetch_clears_stale_pending(self):
        self.run_batch(["https://a"], T0, [ok_result("https://a", "hello", T0)])
        commit_state(self.state_path)
        # page briefly changed, publish failed (pending kept), page reverted
        self.run_batch(["https://a"], T1, [ok_result("https://a", "hello v2", T1)])
        self.assertIsNotNone(self.state("https://a").pending_sha256)
        results = self.run_batch(["https://a"], T2, [ok_result("https://a", "hello", T2)])
        self.assertEqual(results[0]["change"], "unchanged")
        self.assertIsNone(self.state("https://a").pending_sha256)

    def test_error_fetch_preserves_committed_and_pending(self):
        self.run_batch(["https://a"], T0, [ok_result("https://a", "hello", T0)])
        self.run_batch(["https://a"], T1, [ok_result("https://a", "hello v2", T1)])
        results = self.run_batch(
            ["https://a"], T2,
            [{"url": "https://a", "ok": False, "error": "all fetch strategies failed"}])
        self.assertEqual(results[0]["change"], "error")
        entry = self.state("https://a")
        self.assertEqual(entry.pending_sha256, sha("hello v2"))

    def test_state_file_round_trips_pending_fields(self):
        self.run_batch(["https://a"], T0, [ok_result("https://a", "hello", T0)])
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertIn("pending_sha256", raw["https://a"])
        entry = self.state("https://a")
        self.assertEqual(entry.pending_fetched_at, T0)
        self.assertEqual(entry.pending_strategy, "tvly-basic")

    def test_legacy_state_file_without_pending_still_loads(self):
        # Pre-two-phase state files (committed only) must keep working.
        self.state_path.write_text(json.dumps({
            "https://a": {"status": "ok", "fetched_at": T0,
                         "sha256": sha("hello"), "strategy": "tvly-basic"}
        }), encoding="utf-8")
        entry = self.state("https://a")
        self.assertEqual(entry.sha256, sha("hello"))
        self.assertIsNone(entry.pending_sha256)
        self.assertEqual(entry.current_content_sha256, sha("hello"))


if __name__ == "__main__":
    unittest.main()
