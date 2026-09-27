"""Tests for pipeline.publisher — offline, fully deterministic (fixed clocks)."""

import copy
import io
import json
import logging
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pipeline.publisher as pub
from pipeline import okf
from pipeline.publisher import PublisherError, build_document, fact_id, main, publish_facts

NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
NOW2 = NOW + timedelta(hours=2)


def fact(content="The Amazon Ads MCP server is in open beta.", **over):
    base = {
        "content": content,
        "sources": [{"url": "https://advertising.amazon.com/API/docs/en-us",
                     "date": "2026-09-26T00:00:00+00:00", "source_type": "official"}],
        "confidence_score": 0.6,
        "resolution": "single_source",
        "status": "valid",
    }
    base.update(over)
    return base


class PublisherTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.kdir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def publish(self, facts, now=NOW):
        return publish_facts(facts, knowledge_dir=self.kdir, now=now)

    def doc_path(self, content):
        return self.kdir / f"{fact_id(content)}.md"


class PublishTests(PublisherTestCase):
    def test_single_valid_fact_publishes(self):
        report = self.publish([fact()])
        self.assertEqual((report["published"], report["updated"],
                          report["unchanged"], report["skipped"]), (1, 0, 0, 0))
        path = self.doc_path(fact()["content"])
        self.assertTrue(path.exists())
        self.assertEqual(report["documents"], [str(path)])

    def test_multiple_facts_publish(self):
        facts = [fact("Fact one about reporting."),
                 fact("Fact two about onboarding.", resolution="complementary_merge")]
        report = self.publish(facts)
        self.assertEqual(report["published"], 2)
        self.assertEqual(len(report["documents"]), 2)

    def test_valid_low_confidence_publishes(self):
        report = self.publish([fact(status="valid_low_confidence",
                                    confidence_score=0.3,
                                    sources=[{"url": "https://forum.example/t",
                                              "date": None,
                                              "source_type": "community"}])])
        self.assertEqual(report["published"], 1)
        self.assertIn("valid_low_confidence", self.doc_path(
            fact()["content"]).read_text())

    def test_rejected_fact_skipped_not_published(self):
        rejected = {"url": "https://x/", "content": "Rumor.", "status": "rejected",
                    "reason": "unsupported"}
        report = self.publish([fact(), rejected])
        self.assertEqual((report["published"], report["skipped"]), (1, 1))
        self.assertEqual(list(self.kdir.glob("kb-*.md")),
                         [self.doc_path(fact()["content"])])

    def test_malformed_facts_rejected_without_writes(self):
        for bad in [
            "not-a-dict",
            fact(content="   "),
            fact(content="two\nlines"),
            {**fact(), "sources": []},
            {**fact(), "sources": [{"url": "", "source_type": "official"}]},
            {**fact(), "sources": [{"url": "https://x/", "source_type": "blog"}]},
            {**fact(), "confidence_score": "high"},
            {**fact(), "confidence_score": 1.5},
            {**fact(), "resolution": "magic"},
            {k: v for k, v in fact().items() if k != "status"},
        ]:
            with self.assertRaises(PublisherError, msg=repr(bad)[:60]):
                self.publish([bad])
        self.assertEqual(list(self.kdir.glob("kb-*.md")), [])  # nothing written


class IdempotencyTests(PublisherTestCase):
    def test_document_id_is_deterministic(self):
        self.assertEqual(fact_id("Claim A."), fact_id("Claim A."))
        self.assertNotEqual(fact_id("Claim A."), fact_id("Claim B."))
        self.assertRegex(fact_id("Claim A."), r"^kb-[0-9a-f]{16}$")

    def test_second_identical_run_reports_unchanged(self):
        first = self.publish([fact()])
        second = self.publish([fact()])
        self.assertEqual((first["published"], first["unchanged"]), (1, 0))
        self.assertEqual((second["published"], second["updated"],
                          second["unchanged"]), (0, 0, 1))

    def test_documents_identical_despite_different_last_run(self):
        self.publish([fact()], now=NOW)
        text1 = self.doc_path(fact()["content"]).read_text()
        self.publish([fact()], now=NOW2)  # different clock, same input
        text2 = self.doc_path(fact()["content"]).read_text()
        self.assertEqual(text1, text2)

    def test_different_last_run_values_in_report(self):
        r1 = self.publish([fact()], now=NOW)
        r2 = self.publish([fact()], now=NOW2)
        self.assertNotEqual(r1["last_run"], r2["last_run"])
        for r in (r1, r2):
            self.assertRegex(r["last_run"], r"\+00:00$")  # timezone-aware ISO

    def test_index_and_changelog_stable_on_identical_rerun(self):
        self.publish([fact()])
        index1 = (self.kdir / "INDEX.md").read_text()
        changelog1 = (self.kdir / "CHANGELOG.md").read_text()
        self.publish([fact()], now=NOW2)
        self.assertEqual((self.kdir / "INDEX.md").read_text(), index1)
        self.assertEqual((self.kdir / "CHANGELOG.md").read_text(), changelog1)

    def test_changed_sources_update_existing_document(self):
        self.publish([fact()])
        grown = fact(sources=[
            {"url": "https://advertising.amazon.com/API/docs/en-us",
             "date": "2026-09-26T00:00:00+00:00", "source_type": "official"},
            {"url": "https://advertising.amazon.com/about-api",
             "date": "2026-09-27T00:00:00+00:00", "source_type": "official"}])
        report = self.publish([grown])
        self.assertEqual((report["updated"], report["published"]), (1, 0))
        text = self.doc_path(fact()["content"]).read_text()
        self.assertIn("https://advertising.amazon.com/about-api", text)

    def test_unrelated_documents_untouched(self):
        unrelated = self.kdir / "amazon-ads-api-overview.md"
        unrelated.write_text("---\nid: amazon-ads-api-overview\n"
                             "title: Old\nsources:\n  - https://x/used-before\n"
                             "confidence: high\nstatus: official\n"
                             "last_checked: 2026-01-01\n---\n\nbody\n",
                             encoding="utf-8")
        index = self.kdir / "INDEX.md"
        index.write_text("# Knowledge Index\n\n| ID | Title | Status | "
                         "Confidence | Last checked |\n|----|-------|--------|"
                         "------------|--------------|\n"
                         "| [amazon-ads-api-overview](./amazon-ads-api-overview.md)"
                         " | Old | official | high | 2026-01-01 |\n",
                         encoding="utf-8")
        self.publish([fact()])
        self.assertIn("body", unrelated.read_text())  # byte-identical content
        self.assertIn("amazon-ads-api-overview", index.read_text())  # row preserved
        rows = [l for l in index.read_text().splitlines() if l.startswith("| [")]
        self.assertEqual(len(rows), 2)  # old row kept, new row appended


class PreservationTests(PublisherTestCase):
    def setUp(self):
        super().setUp()
        self.content = ("Export APIs provide campaign management information in a "
                        "common model and format across sponsored ads products.")
        self.f = fact(self.content, confidence_score=0.7,
                      resolution="complementary_merge", sources=[
                          {"url": "https://b.example/", "date": "2026-09-26T00:00:00+00:00",
                           "source_type": "community"},
                          {"url": "https://a.example/", "date": None,
                           "source_type": "official"}])
        self.publish([self.f])
        self.text = self.doc_path(self.content).read_text()
        self.meta, self.body = okf.parse(self.text)

    def test_content_preserved_verbatim(self):
        self.assertEqual(self.meta["title"], self.content)
        self.assertIn(f"\n{self.content}\n", self.body)  # full line, unedited

    def test_all_source_metadata_preserved(self):
        self.assertEqual(self.meta["sources"], ["https://a.example/",
                                                "https://b.example/"])
        self.assertIn("- https://a.example/ — official, fetched unknown", self.body)
        self.assertIn("- https://b.example/ — community, fetched 2026-09-26T00:00:00+00:00",
                      self.body)

    def test_confidence_score_and_level_preserved(self):
        self.assertEqual(self.meta["confidence"], "high")  # 0.7 -> high band
        self.assertIn("- confidence_score: 0.70", self.body)

    def test_resolution_preserved(self):
        self.assertIn("- resolution: complementary_merge", self.body)

    def test_status_preserved(self):
        self.assertIn("- status: valid", self.body)
        self.assertEqual(self.meta["status"], "official")  # official source present

    def test_document_parses_as_valid_okf(self):
        okf.validate(self.meta)  # raises on any violation
        self.assertRegex(self.meta["id"], r"^kb-[0-9a-f]{16}$")
        self.assertEqual(self.meta["last_checked"], "2026-09-27")


class AtomicityTests(PublisherTestCase):
    def test_failed_write_leaves_no_partial_documents(self):
        original = Path.write_text

        def flaky(self, data, encoding=None, **kw):
            if self.name.endswith(".tmp") and self.name.startswith("kb-"):
                raise OSError("disk full")
            return original(self, data, encoding=encoding, **kw)

        with patch.object(Path, "write_text", flaky):
            with self.assertRaises(OSError):
                self.publish([fact("Fact A."), fact("Fact B.")])
        self.assertEqual(list(self.kdir.glob("kb-*.md")), [])   # no docs
        self.assertEqual(list(self.kdir.glob("*.tmp")), [])     # no temp litter
        self.assertFalse((self.kdir / "INDEX.md").exists())     # no index either


class ReportTests(PublisherTestCase):
    def test_report_shape(self):
        report = self.publish([fact()])
        self.assertEqual(set(report), {"input", "published", "updated",
                                       "unchanged", "skipped", "documents",
                                       "last_run"})
        self.assertEqual(report["input"], 1)


class CliTests(PublisherTestCase):
    def setUp(self):
        super().setUp()
        self._root_handlers = logging.getLogger().handlers[:]

    def tearDown(self):
        logging.getLogger().handlers[:] = self._root_handlers
        self._tmp.cleanup()

    def test_cli_with_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "merged.json"
            path.write_text(json.dumps([fact()]), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = main([str(path)], knowledge_dir=self.kdir)
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["published"], 1)
        self.assertTrue((self.kdir / "INDEX.md").exists())

    def test_cli_with_stdin(self):
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps([fact()]))), \
                redirect_stdout(out):
            code = main(["-"], knowledge_dir=self.kdir)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["published"], 1)

    def test_cli_invalid_input_exit_2(self):
        err = io.StringIO()
        with patch("sys.stdin", io.StringIO("[{\"content\": \"x\"}]")), \
                redirect_stderr(err):
            code = main(["-"], knowledge_dir=self.kdir)
        self.assertEqual(code, 2)
        self.assertIn("error:", err.getvalue())

    def test_cli_facts_object_unwrapped(self):
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps({"facts": [fact()]}))), \
                redirect_stdout(out):
            code = main(["-"], knowledge_dir=self.kdir)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["input"], 1)


if __name__ == "__main__":
    unittest.main()
