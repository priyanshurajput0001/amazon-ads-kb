"""Tests for the fetch CLI (python3 -m pipeline.fetch) — fully offline."""

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pipeline.fetch import main
import pipeline.fetch as pipeline_fetch

T0 = "2026-09-26T12:00:00+00:00"
T1 = "2026-09-26T13:00:00+00:00"


def ok_result(url: str, content: str, fetched_at: str) -> dict:
    return {
        "url": url, "strategy": "tvly-basic", "content_type": "markdown",
        "title": "page title", "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "fetched_at": fetched_at, "content": content, "ok": True,
    }


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self._tmp.name) / "fetch_state.json"
        self.cache_path = Path(self._tmp.name) / "cache"

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, argv, batch):
        """Run main() with a canned fetch batch; returns (exit_code, parsed JSON rows)."""
        def fake_fetcher(urls, fetched_at):
            return batch
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv, state_path=self.state_path, fetcher=fake_fetcher,
                        cache_path=self.cache_path)
        return code, [json.loads(line) for line in out.getvalue().splitlines()]

    def test_requires_at_least_one_url(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_cli([], [])
        self.assertEqual(cm.exception.code, 2)  # argparse usage error

    def test_first_fetch_is_new(self):
        code, rows = self.run_cli(["https://a"], [ok_result("https://a", "hello", T0)])
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["url"], "https://a")
        self.assertTrue(row["ok"])
        self.assertEqual(row["verdict"], "new")
        self.assertEqual(row["strategy"], "tvly-basic")
        self.assertEqual(row["content_length"], len("hello"))
        self.assertEqual(row["sha256"], ok_result("https://a", "hello", T0)["sha256"])
        self.assertIsNone(row["error"])

    def test_standalone_fetch_stages_pending_not_committed(self):
        # Two-phase commit: the fetch CLI alone never commits a new hash —
        # the orchestrator does, after publication succeeds. Re-fetching the
        # same content therefore reports "new" again until then.
        code, rows = self.run_cli(["https://a"], [ok_result("https://a", "hello", T0)])
        self.assertEqual(rows[0]["verdict"], "new")
        self.assertFalse(rows[0]["committed"])
        code, rows = self.run_cli(["https://a"], [ok_result("https://a", "hello", T1)])
        self.assertEqual(rows[0]["verdict"], "new")
        self.assertFalse(rows[0]["committed"])

    def test_unchanged_after_explicit_commit(self):
        from pipeline.state import commit_state
        self.run_cli(["https://a"], [ok_result("https://a", "hello", T0)])
        commit_state(self.state_path)
        code, rows = self.run_cli(["https://a"], [ok_result("https://a", "hello", T1)])
        self.assertEqual(rows[0]["verdict"], "unchanged")
        self.assertTrue(rows[0]["committed"])

    def test_failed_url_is_reported_not_fatal(self):
        batch = [{"url": "https://bad", "ok": False,
                  "error": "all fetch strategies failed"}]
        code, rows = self.run_cli(["https://bad"], batch)
        self.assertEqual(code, 0)
        self.assertFalse(rows[0]["ok"])
        self.assertEqual(rows[0]["verdict"], "error")
        self.assertEqual(rows[0]["error"], "all fetch strategies failed")

    def test_multiple_urls_keep_order(self):
        batch = [ok_result("https://a", "hello", T0),
                 ok_result("https://b", "world", T0)]
        code, rows = self.run_cli(["https://a", "https://b"], batch)
        self.assertEqual(code, 0)
        self.assertEqual([r["url"] for r in rows], ["https://a", "https://b"])


class DirectContentTypeTests(unittest.TestCase):
    """Concrete bug exposed by the Phase-2 pre-check (2026-09-29): a direct
    fetch of a raw text/plain markdown file was mislabeled html and refused
    at Fetch. Plain-text bodies ARE the readable content."""

    @staticmethod
    def _fake_response(content: bytes, content_type: str):
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.__enter__.return_value = resp   # usable as a context manager
        resp.__exit__.return_value = False
        resp.headers.get_content_charset.return_value = "utf-8"
        resp.headers.get_content_type.return_value = content_type
        resp.read.return_value = content
        return resp

    def test_plain_text_body_is_markdown(self):
        with patch("urllib.request.urlopen",
                   return_value=self._fake_response(b"# readme\n\nhello",
                                                    "text/plain")):
            result = pipeline_fetch._direct("https://raw.example/file.md",
                                            "2026-09-29T00:00:00+00:00")
        self.assertIsNotNone(result)
        self.assertEqual(result.content_type, "markdown")
        self.assertEqual(result.strategy, "direct")

    def test_text_markdown_body_is_markdown(self):
        with patch("urllib.request.urlopen",
                   return_value=self._fake_response(b"readme", "text/markdown")):
            result = pipeline_fetch._direct("https://raw.example/file.md",
                                            "2026-09-29T00:00:00+00:00")
        self.assertIsNotNone(result)
        self.assertEqual(result.content_type, "markdown")

    def test_xml_feed_converts_to_markdown(self):
        from unittest.mock import MagicMock
        rss = (b"<?xml version='1.0'?><rss><channel><title>Amazon Ads API "
                b"Release Notes</title><item><title>v2 general "
                b"availability</title><description>The reporting API v2 is "
                b"now generally available with standardized metrics and "
                b"export endpoints.</description></item></channel></rss>")
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        resp.headers.get_content_charset.return_value = "utf-8"
        resp.headers.get_content_type.return_value = "text/xml"
        resp.read.return_value = rss
        with patch("urllib.request.urlopen", return_value=resp):
            result = pipeline_fetch._direct(
                "https://cdn.example/rss/ad-api-rss.xml",
                "2026-09-30T00:00:00+00:00")
        self.assertIsNotNone(result)
        self.assertEqual(result.content_type, "markdown")
        self.assertEqual(result.strategy, "direct-html")
        self.assertIn("v2 general availability", result.content)

    def test_html_shell_page_still_labeled_html(self):
        # A JS shell converts to (almost) nothing: honest html verdict, the
        # pipeline refuses to extract noise.
        with patch("urllib.request.urlopen",
                   return_value=self._fake_response(b"<html><body>x</body>",
                                                    "text/html")):
            result = pipeline_fetch._direct("https://example.com/page",
                                            "2026-09-29T00:00:00+00:00")
        self.assertIsNotNone(result)
        self.assertEqual(result.content_type, "html")
        self.assertEqual(result.strategy, "direct")

    def test_content_html_page_converts_to_markdown(self):
        """Review step 6: an HTML page with real visible text is converted
        with the stdlib converter instead of being refused at Fetch."""
        page = (b"<html><body><h1>Release notes</h1>"
                b"<p>The Amazon Ads API version 2 adds asynchronous report "
                b"requests and deprecates the snapshots APIs in favor of "
                b"export APIs.</p></body></html>")
        with patch("urllib.request.urlopen",
                   return_value=self._fake_response(page, "text/html")):
            result = pipeline_fetch._direct(
                "https://advertising.amazon.com/API/docs/en-us/info/"
                "release-notes", "2026-09-29T00:00:00+00:00")
        self.assertIsNotNone(result)
        self.assertEqual(result.content_type, "markdown")
        self.assertEqual(result.strategy, "direct-html")
        self.assertIn("# Release notes", result.content)
        self.assertIn("asynchronous report requests", result.content)
        # the sha fingerprints the CONVERTED content the pipeline will see
        import hashlib
        self.assertEqual(
            result.sha256,
            hashlib.sha256(result.content.encode()).hexdigest())


if __name__ == "__main__":
    unittest.main()
