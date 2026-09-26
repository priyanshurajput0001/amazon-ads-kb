"""Tests for the fetch CLI (python3 -m pipeline.fetch) — fully offline."""

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from pipeline.fetch import main

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

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, argv, batch):
        """Run main() with a canned fetch batch; returns (exit_code, parsed JSON rows)."""
        def fake_fetcher(urls, fetched_at):
            return batch
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv, state_path=self.state_path, fetcher=fake_fetcher)
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

    def test_second_identical_fetch_is_unchanged(self):
        self.run_cli(["https://a"], [ok_result("https://a", "hello", T0)])
        code, rows = self.run_cli(["https://a"], [ok_result("https://a", "hello", T1)])
        self.assertEqual(code, 0)
        self.assertEqual(rows[0]["verdict"], "unchanged")

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


if __name__ == "__main__":
    unittest.main()
