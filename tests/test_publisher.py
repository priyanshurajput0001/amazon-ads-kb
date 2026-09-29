"""Tests for pipeline.publisher — offline, temp knowledge dirs, fake clock.

Covers the concept-document contract (review Parts 1, 6, 8):
  - OKF output with a valid `type`
  - stable identity: filename == id, updates in place, no duplicates
  - per-fact provenance, dates, confidence/status, resolution preserved
  - conflicts surfaced, never erased
  - INDEX/CHANGELOG consistent with the documents, atomically batched
  - idempotency: unchanged concepts are not rewritten at all
"""

import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import okf
from pipeline.publisher import (
    PublisherError,
    confidence_level,
    publish_concepts,
)

NOW = datetime.datetime(2026, 9, 27, 12, 0, 0, tzinfo=datetime.timezone.utc)
NOW2 = datetime.datetime(2026, 9, 28, 9, 0, 0, tzinfo=datetime.timezone.utc)
TODAY = "2026-09-27"

URL = "https://advertising.amazon.com/API/docs/en-us/test"
URL2 = "https://github.com/amzn/some-repo"


def src(url=URL, date="2026-09-26T05:00:00+00:00", source_type="official"):
    return {"url": url, "date": date, "source_type": source_type}


def cfact(content, sources=None, score=0.7, resolution="single_source",
          first_seen="2026-09-26", status="valid", **extra):
    base = {
        "content": content,
        "confidence_score": score,
        "status": status,
        "resolution": resolution,
        "first_seen": first_seen,
        "sources": sources or [src()],
    }
    base.update(extra)
    return base


def concept(cid, facts, conflicts=(), title=None):
    return {"id": cid, "title": title, "type": "concept",
            "facts": list(facts), "conflicts": list(conflicts)}


def merged(concepts_, rejected=()):
    return {"concepts": list(concepts_), "rejected": list(rejected)}


class PublisherTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.kdir = Path(self._tmp.name) / "knowledge"

    def tearDown(self):
        self._tmp.cleanup()

    def publish(self, data, now=NOW):
        return publish_concepts(data, knowledge_dir=self.kdir, now=now)

    def snapshot(self):
        return {str(p): p.read_bytes()
                for p in sorted(self.kdir.glob("*.md"))}


class DocumentContractTests(PublisherTestCase):
    def test_published_document_is_valid_okf_with_type(self):
        self.publish(merged([concept("api-access", [
            cfact("Approval may take 1 business day.")])]))
        text = (self.kdir / "api-access.md").read_text(encoding="utf-8")
        meta, body = okf.parse(text)
        self.assertEqual(meta["type"], "concept")
        self.assertEqual(meta["id"], "api-access")
        self.assertEqual(meta["title"], "API Access")
        self.assertEqual(meta["status"], "official")
        self.assertEqual(meta["confidence"], "high")
        self.assertIn("type: concept", text)

    def test_filename_is_the_stable_id(self):
        self.publish(merged([concept("sponsored-products-overview", [
            cfact("Sponsored Products uses cost-per-click billing.")])]))
        self.assertTrue((self.kdir / "sponsored-products-overview.md").exists())

    def test_confidence_is_the_weakest_fact_band(self):
        self.publish(merged([concept("mixed", [
            cfact("Solid fact.", score=0.9),
            cfact("Shaky fact.", score=0.4, status="valid_low_confidence"),
        ])]))
        meta, _ = okf.parse((self.kdir / "mixed.md").read_text())
        self.assertEqual(meta["confidence"], "medium")

    def test_status_community_without_official_sources(self):
        self.publish(merged([concept("community-topic", [
            cfact("A community claim.",
                  sources=[src(url="https://blog.example/x",
                               source_type="community")])])]))
        meta, _ = okf.parse((self.kdir / "community-topic.md").read_text())
        self.assertEqual(meta["status"], "community")

    def test_fact_provenance_rendered_and_reparsable(self):
        self.publish(merged([concept("api-access", [
            cfact("Approval may take 1 business day.",
                  sources=[src(), src(url=URL2, date="2026-09-27T01:00:00+00:00")],
                  resolution="duplicate_merged")])]))
        text = (self.kdir / "api-access.md").read_text()
        self.assertIn("- Approval may take 1 business day.", text)
        self.assertIn("resolution: duplicate_merged", text)
        self.assertIn("first_seen: 2026-09-26", text)
        self.assertIn(f"{URL} (official, fetched 2026-09-26T05:00:00+00:00)", text)

        from pipeline.concepts import load_bundle
        bundle = load_bundle(self.kdir)
        fact = bundle["api-access"]["facts"][0]
        self.assertEqual(fact["content"], "Approval may take 1 business day.")
        self.assertEqual(len(fact["sources"]), 2)

    def test_conflicts_section_surfaces_superseded_fact(self):
        self.publish(merged([concept("license", [
            cfact("The repository is licensed under Apache-2.0.",
                  resolution="conflict_resolved_by_recency")],
            conflicts=[cfact("The repository is licensed under MIT-0.",
                             resolution="conflict_resolved_by_recency",
                             superseded_by="The repository is licensed under Apache-2.0.")])]))
        text = (self.kdir / "license.md").read_text()
        self.assertIn("### Conflicts", text)
        self.assertIn("MIT-0", text)
        self.assertIn("superseded_by: The repository is licensed under Apache-2.0.",
                      text)
        from pipeline.concepts import load_bundle
        bundle = load_bundle(self.kdir)
        self.assertEqual(len(bundle["license"]["conflicts"]), 1)

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(PublisherError):
            self.publish(merged([
                concept("dup", [cfact("one")]),
                concept("dup", [cfact("two")])]))
        self.assertEqual(self.snapshot(), {})  # nothing written

    def test_malformed_fact_rejected_before_writes(self):
        with self.assertRaises(PublisherError):
            self.publish(merged([concept("bad", [
                {"content": "no score", "sources": [src()]}])]))
        self.assertFalse(self.kdir.exists() and any(self.kdir.iterdir()))


class IdempotencyTests(PublisherTestCase):
    def test_republish_identical_input_is_unchanged_and_byte_identical(self):
        data = merged([concept("api-access", [
            cfact("Approval may take 1 business day.")])])
        first = self.publish(data)
        before = self.snapshot()
        second = self.publish(data, now=NOW2)  # different clock!
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(second["published"], 0)
        self.assertEqual(second["updated"], 0)
        self.assertEqual(self.snapshot(), before)  # byte-identical, no rewrite
        self.assertEqual(first["updated"], 0)

    def test_last_checked_bumped_only_on_real_change(self):
        data = merged([concept("api-access", [
            cfact("Approval may take 1 business day.")])])
        self.publish(data)
        meta1, _ = okf.parse((self.kdir / "api-access.md").read_text())
        changed = merged([concept("api-access", [
            cfact("Approval may take 1 business day."),
            cfact("Applications may be submitted by advertisers and partners.",
                  sources=[src(date="2026-09-28T05:00:00+00:00")]),
        ])])
        report = self.publish(changed, now=NOW2)
        meta2, _ = okf.parse((self.kdir / "api-access.md").read_text())
        self.assertEqual(report["updated"], 1)
        self.assertEqual(meta1["last_checked"], "2026-09-27")
        self.assertEqual(meta2["last_checked"], "2026-09-28")
        self.assertEqual(meta2["id"], meta1["id"])  # same concept updated

    def test_update_in_place_never_creates_second_document(self):
        data = merged([concept("api-access", [
            cfact("Approval may take 1 business day.")])])
        self.publish(data)
        grown = merged([concept("api-access", [
            cfact("Approval may take 1 business day."),
            cfact("Applications may be submitted by advertisers and partners.")])])
        self.publish(grown, now=NOW2)
        docs = [p for p in self.kdir.glob("*.md")
                if p.name not in ("INDEX.md", "CHANGELOG.md")]
        self.assertEqual([p.name for p in docs], ["api-access.md"])


class IndexChangelogTests(PublisherTestCase):
    def test_index_lists_every_document_exactly_once(self):
        self.publish(merged([
            concept("api-access", [cfact("Approval may take 1 business day.")]),
            concept("reporting", [cfact("The API supports asynchronous reports.")]),
        ]))
        index = (self.kdir / "INDEX.md").read_text()
        self.assertIn("[api-access](./api-access.md)", index)
        self.assertIn("[reporting](./reporting.md)", index)
        self.assertEqual(index.count("./api-access.md"), 1)

    def test_changelog_records_created_and_updated(self):
        data = merged([concept("api-access", [cfact("Approval may take 1 day.")])])
        self.publish(data)
        self.publish(merged([concept("api-access", [
            cfact("Approval may take 1 day."),
            cfact("Applications may be submitted by advertisers.")])]),
            now=NOW2)
        log = (self.kdir / "CHANGELOG.md").read_text()
        self.assertIn("## 2026-09-27", log)
        self.assertIn("## 2026-09-28", log)
        self.assertIn("**api-access** — created.", log)
        self.assertIn("**api-access** — updated.", log)

    def test_rejected_facts_are_skipped_and_counted(self):
        report = self.publish(merged(
            [concept("ok", [cfact("fine")])],
            rejected=[{"content": "bad", "status": "rejected"}]))
        self.assertEqual(report["skipped"], 1)
        self.assertTrue((self.kdir / "ok.md").exists())

    def test_index_unchanged_documents_kept_without_rewrite(self):
        self.publish(merged([concept("api-access", [cfact("Approval may take 1 day.")])]))
        before_index = (self.kdir / "INDEX.md").read_bytes()
        self.publish(merged([concept("reporting", [cfact("Async reports.")] )]),
                     now=NOW2)
        after_index = (self.kdir / "INDEX.md").read_bytes()
        self.assertNotEqual(before_index, after_index)  # new row added


class RelatedLinksTests(PublisherTestCase):
    def test_related_links_only_with_shared_source_and_overlap(self):
        facts_a = [cfact("The reporting API offers asynchronous report requests.")]
        facts_b = [cfact("Asynchronous report requests are supported.",
                         sources=[src()])]
        facts_c = [cfact("Bulk sheets export campaign statistics.",
                         sources=[src(url="https://other.example/z")])]
        self.publish(merged([
            concept("reporting-api", facts_a),
            concept("async-reports", facts_b),
            concept("bulk-sheets", facts_c),
        ]))
        text_a = (self.kdir / "reporting-api.md").read_text()
        self.assertIn("[Async Reports](./async-reports.md)", text_a)
        self.assertNotIn("bulk-sheets", text_a)  # no shared source -> no link

    def test_no_related_section_when_nothing_justified(self):
        self.publish(merged([concept("solo", [
            cfact("An isolated claim.",
                  sources=[src(url="https://only.example/s")])])]))
        self.assertNotIn("## Related",
                         (self.kdir / "solo.md").read_text())

    def test_links_target_existing_documents_only(self):
        self.publish(merged([
            concept("a", [cfact("Shared subject alpha claim.")]),
            concept("b", [cfact("Shared subject beta claim about alpha.",
                                sources=[src()])]),
        ]))
        for name in ("a", "b"):
            text = (self.kdir / f"{name}.md").read_text()
            for line in text.splitlines():
                if "](./" in line:
                    target = line.split("](./", 1)[1].split(".md)", 1)[0]
                    self.assertTrue((self.kdir / f"{target}.md").exists())


class AtomicityTests(PublisherTestCase):
    def test_render_failure_writes_nothing(self):
        data = merged([concept("bad\nslug", [cfact("x")])])
        with self.assertRaises(PublisherError):
            self.publish(data)
        self.assertEqual(self.snapshot(), {})

    def test_stage_failure_cleans_temps_and_writes_nothing(self):
        data = merged([
            concept("api-access", [cfact("Approval may take 1 business day.")])])
        real_write = Path.write_text

        def flaky(path, text, encoding=None):
            if str(path).endswith(".tmp"):
                raise OSError("disk full")
            return real_write(path, text, encoding=encoding)

        with patch.object(Path, "write_text", flaky):
            with self.assertRaises(OSError):
                self.publish(data)
        self.assertEqual(self.snapshot(), {})  # nothing final written
        leftovers = list(self.kdir.glob("*.tmp"))
        self.assertEqual(leftovers, [])  # temps cleaned up

    def test_documents_index_and_changelog_move_together(self):
        report = self.publish(merged([
            concept("api-access", [cfact("Approval may take 1 business day.")])]))
        # One batch: all three artifacts exist after a successful publish,
        # and the report accounts for them together.
        self.assertTrue((self.kdir / "api-access.md").exists())
        self.assertTrue((self.kdir / "INDEX.md").exists())
        self.assertTrue((self.kdir / "CHANGELOG.md").exists())
        self.assertTrue(report["index_updated"])
        self.assertTrue(report["changelog_updated"])


class ConfidenceBandTests(unittest.TestCase):
    """Pinned boundaries — must fail if the implementation bands change."""

    def test_boundaries(self):
        self.assertEqual(confidence_level(0.599), "medium")
        self.assertEqual(confidence_level(0.60), "high")   # exactly at band
        self.assertEqual(confidence_level(0.601), "high")
        self.assertEqual(confidence_level(0.299), "low")
        self.assertEqual(confidence_level(0.30), "medium") # exactly at band
        self.assertEqual(confidence_level(0.31), "medium")
        self.assertEqual(confidence_level(0.0), "low")
        self.assertEqual(confidence_level(1.0), "high")


if __name__ == "__main__":
    unittest.main()
