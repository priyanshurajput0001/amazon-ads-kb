"""Tests for the content cache (state/cache/<sha256>.<ext>)."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from pipeline.fetch import fetch_many_with_state
from pipeline.state import cache_content

T0 = "2026-09-26T12:00:00+00:00"
T1 = "2026-09-26T13:00:00+00:00"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ok_result(url: str, content: str, fetched_at: str = T0) -> dict:
    return {
        "url": url, "strategy": "tvly-basic", "content_type": "markdown",
        "title": "page title", "sha256": sha(content), "fetched_at": fetched_at,
        "content": content, "ok": True,
    }


class CacheContentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._tmp.name) / "cache"

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_file_with_exact_content(self):
        path = cache_content(self.cache_dir, "hello", sha("hello"))
        self.assertEqual(path, self.cache_dir / f"{sha('hello')}.md")
        self.assertEqual(path.read_text(encoding="utf-8"), "hello")

    def test_creates_cache_dir_automatically(self):
        self.assertFalse(self.cache_dir.exists())
        cache_content(self.cache_dir, "hello", sha("hello"))
        self.assertTrue(self.cache_dir.is_dir())

    def test_does_not_rewrite_existing_file(self):
        path = cache_content(self.cache_dir, "hello", sha("hello"))
        path.write_text("SENTINEL", encoding="utf-8")  # simulate an existing file
        cache_content(self.cache_dir, "hello", sha("hello"))
        self.assertEqual(path.read_text(encoding="utf-8"), "SENTINEL")

    def test_html_content_gets_html_extension(self):
        path = cache_content(self.cache_dir, "<p>hi</p>", sha("x"), content_type="html")
        self.assertEqual(path.suffix, ".html")


class FetchCacheIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self._tmp.name) / "cache"
        self.state_path = Path(self._tmp.name) / "fetch_state.json"

    def tearDown(self):
        self._tmp.cleanup()

    def run_fetch(self, batch, fetched_at=T0):
        def fake_fetcher(urls, ts):
            return batch
        return fetch_many_with_state(["https://a"], fetched_at,
                                     state_path=self.state_path,
                                     fetcher=fake_fetcher,
                                     cache_path=self.cache_path)

    def test_successful_fetch_creates_cache(self):
        results = self.run_fetch([ok_result("https://a", "hello")])
        expected = self.cache_path / f"{sha('hello')}.md"
        self.assertEqual(results[0]["cache_path"], str(expected))
        self.assertEqual(expected.read_text(encoding="utf-8"), "hello")

    def test_same_hash_does_not_rewrite_existing_cache(self):
        self.run_fetch([ok_result("https://a", "hello")])
        cache_file = self.cache_path / f"{sha('hello')}.md"
        cache_file.write_text("SENTINEL", encoding="utf-8")
        self.run_fetch([ok_result("https://a", "hello", fetched_at=T1)], fetched_at=T1)
        self.assertEqual(cache_file.read_text(encoding="utf-8"), "SENTINEL")

    def test_failed_fetch_creates_no_cache(self):
        results = self.run_fetch(
            [{"url": "https://a", "ok": False, "error": "all fetch strategies failed"}])
        self.assertIsNone(results[0]["cache_path"])
        self.assertFalse(self.cache_path.exists() and any(self.cache_path.iterdir()))

    def test_different_content_creates_different_cache_file(self):
        self.run_fetch([ok_result("https://a", "hello")])
        self.run_fetch([ok_result("https://a", "hello v2", fetched_at=T1)], fetched_at=T1)
        files = sorted(p.name for p in self.cache_path.iterdir())
        self.assertEqual(files, sorted([f"{sha('hello')}.md", f"{sha('hello v2')}.md"]))


if __name__ == "__main__":
    unittest.main()
