"""Tests for fetch_many_with_state — fully offline via an injected fetcher."""

import hashlib
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

T0 = "2026-09-26T12:00:00+00:00"
T1 = "2026-09-26T13:00:00+00:00"


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

    def tearDown(self):
        self._tmp.cleanup()

    def run_batch(self, urls, fetched_at, batch):
        fetcher = ScriptedFetcher(batch)
        results = fetch_many_with_state(
            urls, fetched_at, state_path=self.state_path, fetcher=fetcher)
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
        results, _ = self.run_batch(["https://a"], T1, [ok_result("https://a", "hello", T1)])
        self.assertEqual(results[0]["change"], "unchanged")

    def test_changed_on_modified_content(self):
        self.run_batch(["https://a"], T0, [ok_result("https://a", "hello", T0)])
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


if __name__ == "__main__":
    unittest.main()
