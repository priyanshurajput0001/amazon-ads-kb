"""Tests for pipeline.orchestrate — offline; every seam is faked.

The orchestrator itself must contain no pipeline logic, so these tests only
assert sequencing: which stages ran, where each URL stopped, and that the
knowledge bundle is never touched on failure.
"""

import hashlib
import io
import json
import logging
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from pipeline.extractor import LlmError
from pipeline.fetch import FetchResult
from pipeline.orchestrate import OrchestratorError, main, orchestrate

NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
NOW2 = datetime(2026, 9, 27, 13, 0, 0, tzinfo=timezone.utc)

URL = "https://advertising.amazon.com/API/docs/en-us/test-page"
URL2 = "https://advertising.amazon.com/API/docs/en-us/other-page"

MD = ("Amazon Ads API test overview. Sponsored Products uses cost-per-click "
      "billing. Application approval may take up to 1 business day.")
MD_CHANGED = MD + " New reporting endpoints were added this quarter."

CLAIMS = [
    {"claim": "Sponsored Products uses cost-per-click billing.",
     "quote": "Sponsored Products uses cost-per-click billing.",
     "topic_hint": "sponsored-products-overview", "confidence": "high"},
    {"claim": "Application approval may take up to 1 business day.",
     "quote": "Application approval may take up to 1 business day.",
     "topic_hint": "amazon-ads-api-onboarding", "confidence": "high"},
]


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fake_fetcher(pages):
    """pages: {url: "markdown" | ("html", "...") | Exception}."""

    def _fetch(urls, fetched_at):
        out = []
        for url in urls:
            spec = pages.get(url)
            if isinstance(spec, Exception) or spec is None:
                out.append({"url": url, "ok": False,
                            "error": "all fetch strategies failed"})
                continue
            content_type, content = spec if isinstance(spec, tuple) \
                else ("markdown", spec)
            out.append(FetchResult(
                url=url, strategy="fake", content_type=content_type,
                title=None, content=content, sha256=sha(content),
                fetched_at=fetched_at).to_json() | {"ok": True})
        return out

    return _fetch


def fake_extract(claims=CLAIMS, calls=None):
    def _llm(content, url):
        if calls is not None:
            calls.append(url)
        return claims

    return _llm


def fake_merge(calls=None):
    def _llm(a, b):
        if calls is not None:
            calls.append((a["url"], b["url"]))
        return "complementary"

    return _llm


class OrchestrateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.state = base / "fetch_state.json"
        self.cache = base / "cache"
        self.claims = base / "claims"
        self.knowledge = base / "knowledge"

    def tearDown(self):
        self._tmp.cleanup()

    def run_pipeline(self, urls, pages, extract_llm=None, now=NOW,
                     extract_calls=None, merge_calls=None):
        return orchestrate(
            urls, state_path=self.state, cache_path=self.cache,
            claims_path=self.claims, knowledge_dir=self.knowledge,
            fetcher=fake_fetcher(pages),
            extract_llm=extract_llm or fake_extract(calls=extract_calls),
            merge_llm=fake_merge(calls=merge_calls), now=now)

    def knowledge_files(self):
        return sorted(p for p in self.knowledge.glob("**/*") if p.is_file())

    def snapshot(self):
        return {str(p): p.read_bytes() for p in self.knowledge_files()}


class SequencingTests(OrchestrateTestCase):
    def test_new_url_runs_all_stages(self):
        extract_calls, merge_calls = [], []
        report = self.run_pipeline([URL], {URL: MD}, extract_calls=extract_calls,
                                   merge_calls=merge_calls)
        entry = report["urls"][0]
        self.assertEqual(entry["fetch"]["verdict"], "new")
        self.assertNotIn("stopped_at", entry)
        self.assertEqual(entry["extract"]["claim_count"], 2)
        self.assertEqual(entry["adapter"]["fact_count"], 2)
        self.assertEqual(report["stages_executed"],
                         ["fetch", "adapter", "validator", "merger", "publisher"])
        self.assertEqual(report["facts"]["valid"], 2)
        self.assertEqual(report["merge"], {"input": 2, "output": 2})
        self.assertEqual(report["publish"]["published"], 2)
        self.assertTrue(report["knowledge_bundle_modified"])
        self.assertEqual(extract_calls, [URL])  # real extraction seam used

    def test_changed_url_reruns_extract_onward(self):
        self.run_pipeline([URL], {URL: MD})
        extra = {"claim": "New reporting endpoints were added this quarter.",
                 "quote": "New reporting endpoints were added this quarter.",
                 "topic_hint": "amazon-ads-reporting", "confidence": "high"}
        extract_calls = []
        report = self.run_pipeline(
            [URL], {URL: MD_CHANGED}, now=NOW2, extract_calls=extract_calls,
            extract_llm=fake_extract(CLAIMS + [extra], extract_calls))
        self.assertEqual(report["urls"][0]["fetch"]["verdict"], "changed")
        self.assertIn("extract", report["urls"][0])  # Extractor ran again
        self.assertEqual(extract_calls, [URL])
        # Content-hash identity: the two known sentences map to their existing
        # documents (refreshed with the new fetch date -> updated), and only
        # the genuinely new sentence creates a new document.
        self.assertEqual(report["publish"]["published"], 1)
        self.assertEqual(report["publish"]["updated"], 2)

    def test_unchanged_url_stops_after_fetch(self):
        self.run_pipeline([URL], {URL: MD})
        before = self.snapshot()
        extract_calls, merge_calls = [], []
        report = self.run_pipeline([URL], {URL: MD}, now=NOW2,
                                   extract_calls=extract_calls,
                                   merge_calls=merge_calls)
        entry = report["urls"][0]
        self.assertEqual(entry["fetch"]["verdict"], "unchanged")
        self.assertEqual(entry["stopped_at"], "fetch")
        self.assertEqual(entry["result"], "unchanged; bundle not modified")
        self.assertNotIn("extract", entry)  # Extractor never invoked
        self.assertEqual(report["stages_executed"], ["fetch"])
        self.assertEqual((extract_calls, merge_calls), ([], []))
        self.assertFalse(report["knowledge_bundle_modified"])
        self.assertEqual(self.snapshot(), before)  # bundle byte-identical

    def test_fetch_failure_stops_url_before_any_llm(self):
        extract_calls = []
        report = self.run_pipeline([URL], {}, extract_calls=extract_calls)
        entry = report["urls"][0]
        self.assertFalse(entry["fetch"]["ok"])
        self.assertEqual(entry["stopped_at"], "fetch")
        self.assertIn("error", entry)
        self.assertEqual(extract_calls, [])
        self.assertNotIn("publish", report)
        self.assertFalse(report["knowledge_bundle_modified"])

    def test_html_cache_reported_honestly(self):
        report = self.run_pipeline([URL], {URL: ("html", "<html><body>shell</body>")})
        entry = report["urls"][0]
        self.assertEqual(entry["fetch"]["verdict"], "new")
        self.assertEqual(entry["stopped_at"], "fetch")
        self.assertIn("HTML, not Markdown", entry["error"])
        self.assertNotIn("extract", entry)

    def test_extract_failure_publishes_nothing(self):
        extract_calls = []
        report = self.run_pipeline(
            [URL], {URL: MD},
            extract_llm=(lambda content, url: (_ for _ in ()).throw(
                LlmError("LLM returned invalid JSON"))))
        entry = report["urls"][0]
        self.assertEqual(entry["stopped_at"], "extract")
        self.assertIn("LLM returned invalid JSON", entry["error"])
        self.assertNotIn("publish", report)
        self.assertEqual(self.knowledge_files(), [])  # nothing published

    def test_multiple_urls_handled_independently(self):
        extract_calls = []
        report = self.run_pipeline(
            [URL, URL2, "https://blog.example/gone"],
            {URL: MD, URL2: ("html", "<html></html>")},
            extract_calls=extract_calls)
        by_url = {e["url"]: e for e in report["urls"]}
        self.assertNotIn("stopped_at", by_url[URL])           # full pipeline
        self.assertEqual(by_url[URL2]["stopped_at"], "fetch")  # HTML cache
        self.assertEqual(by_url["https://blog.example/gone"]["stopped_at"],
                         "fetch")                              # fetch failed
        self.assertEqual(extract_calls, [URL])  # only the usable URL extracted
        self.assertEqual(report["publish"]["published"], 2)  # good URL published

    def test_existing_bundle_intact_on_failure(self):
        self.run_pipeline([URL], {URL: MD})
        before = self.snapshot()
        self.assertNotEqual(before, {})
        report = self.run_pipeline(
            [URL2], {URL2: MD_CHANGED},
            extract_llm=(lambda content, url: (_ for _ in ()).throw(
                LlmError("boom"))))
        self.assertEqual(report["urls"][0]["stopped_at"], "extract")
        self.assertEqual(self.snapshot(), before)  # untouched, nothing added

    def test_knowledge_dir_defaults_to_real_bundle(self):
        # Regression: the CLI passes knowledge_dir=None — orchestrate must
        # resolve the Publisher's default instead of handing None downstream.
        with patch("pipeline.orchestrate.DEFAULT_KNOWLEDGE_PATH", self.knowledge):
            report = orchestrate(
                [URL], state_path=self.state, cache_path=self.cache,
                claims_path=self.claims, knowledge_dir=None,
                fetcher=fake_fetcher({URL: MD}),
                extract_llm=fake_extract(), merge_llm=fake_merge(), now=NOW)
        self.assertEqual(report["publish"]["published"], 2)
        self.assertEqual(len(self.knowledge_files()), 4)  # 2 docs + INDEX + CHANGELOG


class CliTests(OrchestrateTestCase):
    def setUp(self):
        super().setUp()
        self._root_handlers = logging.getLogger().handlers[:]

    def tearDown(self):
        logging.getLogger().handlers[:] = self._root_handlers
        self._tmp.cleanup()

    def test_main_prints_report_and_exits_zero(self):
        with patch("pipeline.orchestrate.orchestrate",
                   return_value={"urls": [], "stages_executed": ["fetch"]}):
            out = io.StringIO()
            with redirect_stdout(out):
                code = main([URL])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["stages_executed"], ["fetch"])

    def test_main_requires_urls(self):
        with patch("sys.argv", ["orchestrate"]):
            with self.assertRaises(SystemExit) as ctx:
                main([])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
