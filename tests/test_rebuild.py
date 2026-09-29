"""Tests for pipeline.rebuild — offline; LLM seams faked, temp dirs only."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from pipeline.concepts import load_bundle
from pipeline.rebuild import RebuildError, lint_bundle, rebuild_bundle

NOW = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)

URL = "https://advertising.amazon.com/about-api"
URL2 = "https://github.com/amzn"


def sha_of(text):
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def claims_doc(url, sha, fetched_at, claims):
    return {
        "schema_version": 1, "source_url": url, "sha256": sha,
        "fetched_at": fetched_at, "extracted_at": fetched_at,
        "status": "ok",
        "claims": [{"claim": c, "quote": "q", "topic_hint": h,
                    "confidence": "high"} for c, h in claims],
    }


def llm_complementary(a, b):
    return "complementary"


def llm_match_no(new_claim, title, existing_claims):
    return False


class RebuildTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.state = base / "fetch_state.json"
        self.claims = base / "claims"
        self.knowledge = base / "knowledge"
        self.claims.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def write_claims(self, url, sha, fetched_at, claims):
        path = self.claims / f"{sha}.json"
        path.write_text(json.dumps(
            claims_doc(url, sha, fetched_at, claims)), encoding="utf-8")

    def rebuild(self):
        return rebuild_bundle(state_path=self.state, claims_dir=self.claims,
                              knowledge_dir=self.knowledge,
                              classify_llm=llm_complementary,
                              match_llm=llm_match_no, now=NOW)


class HappyPathTests(RebuildTestCase):
    def test_rebuild_publishes_concepts_with_lint_clean(self):
        self.write_claims(URL, sha_of("v1"), "2026-09-26T00:00:00+00:00", [
            ("The Amazon Ads API supports asynchronous report requests.",
             "async-reporting"),
            ("Amazon Ads offers test accounts for API development.",
             "test-accounts"),
        ])
        report = self.rebuild()
        self.assertEqual(report["concepts"], 2)
        self.assertEqual(report["lint"], "clean")
        self.assertEqual(lint_bundle(self.knowledge), [])
        docs = [p.name for p in self.knowledge.glob("*.md")
                if p.name not in ("INDEX.md", "CHANGELOG.md")]
        self.assertEqual(len(docs), 2)
        bundle = load_bundle(self.knowledge)
        self.assertEqual(len(bundle), 2)

    def test_old_bundle_replaced_and_changelog_history_kept(self):
        self.knowledge.mkdir(parents=True)
        (self.knowledge / "legacy-fragment.md").write_text(
            "---\nid: kb-deadbeef\ntitle: Old fragment\nsources:\n  - "
            + URL + "\nconfidence: high\nstatus: official\n"
            "last_checked: 2026-09-26\n---\n\nold body\n", encoding="utf-8")
        (self.knowledge / "CHANGELOG.md").write_text(
            "# Changelog\n\n## 2026-09-26\n- **kb-deadbeef** — created.\n",
            encoding="utf-8")
        self.write_claims(URL, sha_of("v1"), "2026-09-26T00:00:00+00:00", [
            ("The Amazon Ads API supports asynchronous report requests.",
             "async-reporting")])
        self.rebuild()
        self.assertFalse((self.knowledge / "legacy-fragment.md").exists())
        log = (self.knowledge / "CHANGELOG.md").read_text()
        self.assertIn("## 2026-09-26", log)   # history carried over
        self.assertIn("## 2026-09-29", log)   # rebuild section appended

    def test_historical_and_current_versions_become_dated_conflict(self):
        old_sha, new_sha = sha_of("page v1"), sha_of("page v2")
        self.write_claims(URL2, old_sha, "2026-09-26T00:00:00+00:00", [
            ("The repository is licensed under MIT-0.", "repo-license")])
        self.write_claims(URL2, new_sha, "2026-09-28T00:00:00+00:00", [
            ("The repository is licensed under Apache-2.0.", "repo-license")])
        # fetch state: the NEW version is the current committed one
        self.state.write_text(json.dumps({
            URL2: {"status": "ok", "fetched_at": "2026-09-28T00:00:00+00:00",
                   "sha256": new_sha, "strategy": "direct"}}),
            encoding="utf-8")
        report = self.rebuild()
        self.assertEqual(report["concepts"], 1)
        bundle = load_bundle(self.knowledge)
        (cid, concept), = bundle.items()
        self.assertEqual(len(concept["facts"]), 1)
        self.assertIn("Apache-2.0", concept["facts"][0]["content"])
        self.assertEqual(concept["facts"][0]["resolution"],
                         "conflict_resolved_by_recency")
        # The historical value is retained with its date and superseder.
        (conflict,) = concept["conflicts"]
        self.assertIn("MIT-0", conflict["content"])
        self.assertIn("superseded_by", conflict)
        self.assertEqual(conflict["first_seen"], "2026-09-26")

    def test_duplicate_claims_across_urls_merge_into_one_concept(self):
        self.write_claims(URL, sha_of("a"), "2026-09-26T00:00:00+00:00", [
            ("The Amazon Ads API supports asynchronous report requests.",
             "async-reporting")])
        self.write_claims(URL2, sha_of("b"), "2026-09-27T00:00:00+00:00", [
            ("Asynchronous report requests are supported by the Amazon Ads "
             "API.", "async-reporting")])
        # same hint + high overlap -> same concept deterministically;
        # reworded pair -> duplicate cluster
        def dup_or_comp(a, b):
            return "duplicate"

        report = rebuild_bundle(state_path=self.state, claims_dir=self.claims,
                               knowledge_dir=self.knowledge,
                               classify_llm=dup_or_comp,
                               match_llm=llm_match_no, now=NOW)
        self.assertEqual(report["concepts"], 1)
        bundle = load_bundle(self.knowledge)
        (cid, concept), = bundle.items()
        self.assertEqual(len(concept["facts"]), 1)
        self.assertEqual(len(concept["facts"][0]["sources"]), 2)


class FailureSafetyTests(RebuildTestCase):
    def test_publish_failure_keeps_old_bundle(self):
        self.knowledge.mkdir(parents=True)
        (self.knowledge / "keep-me.md").write_text("placeholder",
                                                   encoding="utf-8")
        self.write_claims(URL, sha_of("v1"), "2026-09-26T00:00:00+00:00", [
            ("The Amazon Ads API supports asynchronous report requests.",
             "async-reporting")])
        with patch("pipeline.rebuild.publish_concepts",
                   side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.rebuild()
        self.assertTrue((self.knowledge / "keep-me.md").exists())
        self.assertEqual(lint_bundle(self.knowledge) != [], True)  # old, not new

    def test_no_claims_is_an_error(self):
        with self.assertRaises(RebuildError):
            self.rebuild()


if __name__ == "__main__":
    unittest.main()
