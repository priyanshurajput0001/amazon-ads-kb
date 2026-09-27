"""Tests for pipeline.publisher — offline, fully deterministic (fixed clocks)."""

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
from pipeline.publisher import (
    PublisherError,
    build_document,
    fact_id,
    main,
    publish_facts,
    readable_slug,
)

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

    def path_for(self, f):
        """Expected path for a NEW fact (no collision)."""
        return self.kdir / f"{readable_slug(pub._slug_base(f))}.md"

    def doc_files(self):
        return sorted(p for p in self.kdir.glob("*.md")
                      if p.name not in ("INDEX.md", "CHANGELOG.md"))


class SlugTests(unittest.TestCase):
    def test_readable_kebab_slug(self):
        self.assertEqual(readable_slug("The new Amazon Ads reporting API is in open beta."),
                         "new-amazon-ads-reporting-api-open-beta")

    def test_special_characters_and_case_normalized(self):
        self.assertEqual(readable_slug("Sponsored Brands/Video: A/B & C++ (2026)!"),
                         "sponsored-brands-video-b-c-2026")
        self.assertEqual(readable_slug("  Multiple   spaces\tand\ntabs  "),
                         "multiple-spaces-tabs")

    def test_slug_capped_on_word_boundary(self):
        slug = readable_slug("word " * 40)
        self.assertLessEqual(len(slug), pub.SLUG_MAX)
        self.assertFalse(slug.startswith("-") or slug.endswith("-"))

    def test_slug_deterministic(self):
        self.assertEqual(readable_slug("Same input."), readable_slug("Same input."))

    def test_all_stopwords_falls_back(self):
        self.assertEqual(readable_slug("The of and"), "fact")

    def test_topic_id_preferred_over_content(self):
        f = fact(topic_id="amazon-ads-reporting-api")
        self.assertEqual(pub._slug_base(f), "amazon-ads-reporting-api")


class PublishTests(PublisherTestCase):
    def test_single_valid_fact_publishes(self):
        report = self.publish([fact()])
        self.assertEqual((report["published"], report["updated"],
                          report["unchanged"], report["skipped"]), (1, 0, 0, 0))
        path = self.path_for(fact())
        self.assertTrue(path.exists())
        self.assertEqual(report["documents"], [str(path)])

    def test_multiple_facts_publish(self):
        facts = [fact("Fact one about reporting."),
                 fact("Fact two about onboarding.", resolution="complementary_merge")]
        report = self.publish(facts)
        self.assertEqual(report["published"], 2)
        self.assertEqual(len(report["documents"]), 2)
        self.assertEqual(len(self.doc_files()), 2)

    def test_valid_low_confidence_publishes(self):
        f = fact(status="valid_low_confidence", confidence_score=0.3,
                 sources=[{"url": "https://forum.example/t", "date": None,
                           "source_type": "community"}])
        self.publish([f])
        self.assertIn("valid_low_confidence", self.path_for(f).read_text())

    def test_rejected_fact_skipped_not_published(self):
        rejected = {"url": "https://x/", "content": "Rumor.", "status": "rejected",
                    "reason": "unsupported"}
        report = self.publish([fact(), rejected])
        self.assertEqual((report["published"], report["skipped"]), (1, 1))
        self.assertEqual(self.doc_files(), [self.path_for(fact())])

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
        self.assertEqual(self.doc_files(), [])  # nothing written


class IdentityAndCollisionTests(PublisherTestCase):
    def test_identity_is_content_hash_not_filename(self):
        f = fact()
        self.publish([f])
        meta, _ = okf.parse(self.path_for(f).read_text())
        self.assertEqual(meta["id"], fact_id(f["content"]))
        self.assertRegex(meta["id"], r"^kb-[0-9a-f]{16}$")
        self.assertNotIn("kb-", self.path_for(f).stem)  # filename is readable

    def test_colliding_slugs_never_overwrite(self):
        prefix = "alpha beta gamma delta epsilon zeta eta theta iota kappa " \
                 "lambda mu nu xi omicron pi rho sigma tau "
        a = fact(prefix + "first variant")
        b = fact(prefix + "second variant")  # same slug prefix after capping
        report = self.publish([a, b])
        self.assertEqual(report["published"], 2)
        self.assertEqual(len(self.doc_files()), 2)  # both survived
        texts = {p.read_text() for p in self.doc_files()}
        self.assertEqual(len(texts), 2)  # contents differ, no silent overwrite

    def test_collision_suffix_derived_from_stable_id(self):
        prefix = "alpha beta gamma delta epsilon zeta eta theta iota kappa " \
                 "lambda mu nu xi omicron pi rho sigma tau "
        b = fact(prefix + "second variant")
        self.publish([fact(prefix + "first variant"), b])
        stable = fact_id(b["content"])
        self.assertTrue(
            any(p.stem == f"{readable_slug(prefix)}-{stable[-6:]}"
                for p in self.doc_files()),
            [p.stem for p in self.doc_files()])

    def test_duplicate_facts_in_one_batch_single_document(self):
        f = fact()
        report = self.publish([f, dict(f)])
        self.assertEqual((report["published"], report["unchanged"]), (1, 1))
        self.assertEqual(len(self.doc_files()), 1)

    def test_existing_document_updated_in_place_same_filename(self):
        self.publish([fact()])
        path_before = self.path_for(fact())
        grown = fact(sources=[
            {"url": "https://advertising.amazon.com/API/docs/en-us",
             "date": "2026-09-26T00:00:00+00:00", "source_type": "official"},
            {"url": "https://advertising.amazon.com/about-api",
             "date": "2026-09-27T00:00:00+00:00", "source_type": "official"}])
        report = self.publish([grown], now=NOW2)
        self.assertEqual((report["updated"], report["published"]), (1, 0))
        self.assertTrue(path_before.exists())           # same file, updated
        self.assertEqual(len(self.doc_files()), 1)      # no duplicate
        self.assertIn("https://advertising.amazon.com/about-api",
                      path_before.read_text())


class IdempotencyTests(PublisherTestCase):
    def test_second_identical_run_reports_unchanged(self):
        first = self.publish([fact()])
        second = self.publish([fact()])
        self.assertEqual((first["published"], first["unchanged"]), (1, 0))
        self.assertEqual((second["published"], second["updated"],
                          second["unchanged"], second["renamed"]), (0, 0, 1, 0))

    def test_documents_identical_despite_different_last_run(self):
        self.publish([fact()], now=NOW)
        text1 = self.path_for(fact()).read_text()
        self.publish([fact()], now=NOW2)
        text2 = self.path_for(fact()).read_text()
        self.assertEqual(text1, text2)

    def test_different_last_run_values_in_report(self):
        r1 = self.publish([fact()], now=NOW)
        r2 = self.publish([fact()], now=NOW2)
        self.assertNotEqual(r1["last_run"], r2["last_run"])
        for r in (r1, r2):
            self.assertRegex(r["last_run"], r"\+00:00$")

    def test_index_and_changelog_stable_on_identical_rerun(self):
        self.publish([fact()])
        index1 = (self.kdir / "INDEX.md").read_text()
        changelog1 = (self.kdir / "CHANGELOG.md").read_text()
        r2 = self.publish([fact()], now=NOW2)
        self.assertFalse(r2["index_updated"])
        self.assertEqual((self.kdir / "INDEX.md").read_text(), index1)
        self.assertEqual((self.kdir / "CHANGELOG.md").read_text(), changelog1)

    def test_unrelated_documents_untouched(self):
        unrelated = self.kdir / "amazon-ads-api-overview.md"
        unrelated.write_text("---\nid: amazon-ads-api-overview\n"
                             "title: Old\nsources:\n  - https://x/used-before\n"
                             "confidence: high\nstatus: official\n"
                             "last_checked: 2026-01-01\n---\n\nbody\n",
                             encoding="utf-8")
        self.publish([fact()])
        self.assertIn("body", unrelated.read_text())  # byte-identical content
        index = (self.kdir / "INDEX.md").read_text()
        self.assertIn("[amazon-ads-api-overview](./amazon-ads-api-overview.md)",
                      index)  # row preserved with its own filename
        self.assertEqual(len([l for l in index.splitlines()
                              if l.startswith("| [")]), 2)


class IndexTests(PublisherTestCase):
    def test_index_links_readable_filenames(self):
        f = fact()
        self.publish([f])
        index = (self.kdir / "INDEX.md").read_text()
        stem = self.path_for(f).stem
        self.assertIn(f"| [{stem}](./{stem}.md) |", index)

    def test_index_follows_migration(self):
        f = fact()
        legacy = self.kdir / f"{fact_id(f['content'])}.md"
        legacy.write_text(build_document(f, "2026-09-26"), encoding="utf-8")
        self.publish([f])
        index = (self.kdir / "INDEX.md").read_text()
        self.assertNotIn(fact_id(f["content"]) + ".md", index)  # old link gone
        self.assertIn(f"(./{self.path_for(f).stem}.md)", index)  # new link


class MigrationTests(PublisherTestCase):
    def test_legacy_kb_file_migrated_to_readable_name(self):
        f = fact()
        stable = fact_id(f["content"])
        legacy = self.kdir / f"{stable}.md"
        legacy.write_text(build_document(f, "2026-09-26"), encoding="utf-8")
        before = legacy.read_text()
        report = self.publish([f])
        self.assertFalse(legacy.exists())                 # old name gone
        new_path = self.path_for(f)
        self.assertTrue(new_path.exists())                # readable name present
        self.assertEqual(new_path.read_text().replace("last_checked: 2026-09-27",
                                                      "last_checked: 2026-09-26"),
                         before.replace("last_checked: 2026-09-26",
                                        "last_checked: 2026-09-26"))
        self.assertEqual(report["renamed"], 1)
        self.assertEqual((report["published"], report["updated"],
                          report["unchanged"]), (0, 0, 1))  # content untouched

    def test_legacy_file_migrated_even_when_not_in_input(self):
        f = fact("Amazon Marketing Stream provides hourly metrics.")
        legacy = self.kdir / f"{fact_id(f['content'])}.md"
        legacy.write_text(build_document(f, "2026-09-26"), encoding="utf-8")
        report = self.publish([fact()])  # different fact entirely
        self.assertEqual(report["renamed"], 1)
        self.assertFalse(legacy.exists())
        self.assertTrue(self.path_for(f).exists())

    def test_non_legacy_handwritten_docs_not_renamed(self):
        doc = self.kdir / "amazon-ads-api-overview.md"
        text = ("---\nid: amazon-ads-api-overview\ntitle: T\n"
                "sources:\n  - https://x/\nconfidence: high\nstatus: official\n"
                "last_checked: 2026-01-01\n---\n\nbody\n")
        doc.write_text(text, encoding="utf-8")
        self.publish([fact()])
        self.assertEqual(doc.read_text(), text)  # untouched, same filename


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
        self.text = self.path_for(self.f).read_text()
        self.meta, self.body = okf.parse(self.text)

    def test_content_preserved_verbatim(self):
        self.assertEqual(self.meta["title"], self.content)
        self.assertIn(f"\n{self.content}\n", self.body)

    def test_all_source_metadata_preserved(self):
        self.assertEqual(self.meta["sources"], ["https://a.example/",
                                                "https://b.example/"])
        self.assertIn("- https://a.example/ — official, fetched unknown", self.body)
        self.assertIn("- https://b.example/ — community, fetched 2026-09-26T00:00:00+00:00",
                      self.body)

    def test_confidence_score_and_level_preserved(self):
        self.assertEqual(self.meta["confidence"], "high")
        self.assertIn("- confidence_score: 0.70", self.body)

    def test_resolution_preserved(self):
        self.assertIn("- resolution: complementary_merge", self.body)

    def test_status_preserved(self):
        self.assertIn("- status: valid", self.body)
        self.assertEqual(self.meta["status"], "official")

    def test_document_parses_as_valid_okf(self):
        okf.validate(self.meta)
        self.assertRegex(self.meta["id"], r"^kb-[0-9a-f]{16}$")
        self.assertEqual(self.meta["last_checked"], "2026-09-27")


class AtomicityTests(PublisherTestCase):
    def test_failed_write_leaves_no_partial_documents(self):
        original = Path.write_text

        def flaky(self, data, encoding=None, **kw):
            if self.name.endswith(".md.tmp"):
                raise OSError("disk full")
            return original(self, data, encoding=encoding, **kw)

        with patch.object(Path, "write_text", flaky):
            with self.assertRaises(OSError):
                self.publish([fact("Fact A."), fact("Fact B.")])
        self.assertEqual(self.doc_files(), [])       # no docs
        self.assertEqual(list(self.kdir.glob("*.tmp")), [])  # no temp litter
        self.assertFalse((self.kdir / "INDEX.md").exists())


class ReportTests(PublisherTestCase):
    def test_report_shape(self):
        report = self.publish([fact()])
        self.assertEqual(set(report), {"input", "published", "updated",
                                       "unchanged", "skipped", "renamed",
                                       "index_updated", "documents", "last_run"})
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
