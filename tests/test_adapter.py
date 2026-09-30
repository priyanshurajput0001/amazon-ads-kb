"""Tests for pipeline.adapter — offline; pure transformations over
real-shaped claims docs and SourceState entries."""

import copy
import io
import json
import logging
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from pipeline.adapter import (
    AdapterError,
    adapt_url,
    build_facts,
    classify_source_type,
    main,
)
from pipeline.state import SourceState

T0 = "2026-09-26T12:00:00+00:00"
T1 = "2026-09-27T09:00:00+00:00"
SHA = "a" * 64

CLAIMS = [
    {"claim": "The Amazon Ads API is a REST API.",
     "quote": "The Amazon Ads API enables you to manage ... using a REST API.",
     "topic_hint": "amazon-ads-api-overview", "confidence": "high"},
    {"claim": "Application approval may take up to 1 business day.",
     "quote": "Application approval may take up to 1 business day.",
     "topic_hint": "amazon-ads-api-onboarding", "confidence": "high"},
]


def doc(claims=CLAIMS, sha=SHA, fetched_at=T0):
    return {"schema_version": 1, "source_url": "https://x/", "sha256": sha,
            "fetched_at": fetched_at, "extracted_at": fetched_at,
            "status": "ok", "claims": claims}


def state(url="https://advertising.amazon.com/API/docs/en-us",
          sha=SHA, fetched_at=T0):
    return SourceState(url=url, status="ok", fetched_at=fetched_at, sha256=sha)


class ClassifyTests(unittest.TestCase):
    def test_official_sources(self):
        for url in ("https://advertising.amazon.com/API/docs/en-us",
                    "https://github.com/amzn",
                    "https://github.com/amzn/amazon-ads-api-sdk"):
            self.assertEqual(classify_source_type(url), "official")

    def test_everything_else_is_community(self):
        for url in ("https://github.com/other-org/repo",
                    "https://sellercentral.example/thread/1",
                    "https://blog.example/post"):
            self.assertEqual(classify_source_type(url), "community")


# Verbatim excerpt of the real fetched README of Amazon's
# ads-advanced-tools-docs repository (raw.githubusercontent.com transport) —
# the exact source the review found was wrongly rejected as community.
AMAZON_DOCS_README = """# Amazon Ads advanced tools docs
This repository contains resources related to Amazon Ads advanced tools, including the Amazon Ads API and bulk operations.
For complete documentation on Amazon Ads advanced tools, see the [Amazon Ads advanced tools center](https://advertising.amazon.com/API/docs/en-us/).
"""

UNRELATED_README = """# my hobby project
A tool I wrote for fun. See my blog at https://blog.example/writing for more.
This project is not affiliated with anyone.
"""

# The exact adversarial false-positive case from the final review: generic
# repository wording + an Amazon docs link does NOT establish ownership.
COMMUNITY_SDK_README = """# ads-sdk
This repository contains a community Python client for the Amazon Ads API.
Official API documentation: https://advertising.amazon.com/API/docs/en-us
MIT licensed, not affiliated with Amazon.
"""


class ContentEvidenceClassificationTests(unittest.TestCase):
    """Review criticism: 'classify sources by page content, not a host list'
    — a raw.githubusercontent.com URL serving Amazon's own documentation
    content must not be rejected solely because of its transport."""

    def test_official_amazon_documentation_url(self):
        # 1. the official docs host is official on URL alone
        self.assertEqual(
            classify_source_type("https://advertising.amazon.com/API/docs/en-us"),
            "official")

    def test_amazon_github_docs_repository(self):
        # 2. Amazon's GitHub org pages keep the existing URL rule
        self.assertEqual(
            classify_source_type("https://github.com/amzn/ads-advanced-tools-docs"),
            "official")

    def test_raw_github_amazon_docs_content_is_official(self):
        # 3. raw transport + Amazon's documentation content -> official
        self.assertEqual(
            classify_source_type(
                "https://raw.githubusercontent.com/amzn/ads-advanced-tools-docs"
                "/main/README.md",
                content=AMAZON_DOCS_README),
            "official")

    def test_genuinely_unrelated_github_content_is_community(self):
        # 4. same transport, unrelated content -> community
        self.assertEqual(
            classify_source_type(
                "https://raw.githubusercontent.com/example/hobby/main/README.md",
                content=UNRELATED_README),
            "community")
        # a page that merely LINKS to Amazon docs in third-person voice,
        # without an ownership declaration, stays community
        third_party = ("Great tutorial about the Ads API! Official docs: "
                       "https://advertising.amazon.com/API/docs/en-us\n")
        self.assertEqual(
            classify_source_type("https://blog.example/ads-tutorial",
                                 content=third_party),
            "community")

    def test_community_sdk_readme_is_not_official(self):
        # Regression (final adversarial review): generic wording such as
        # "This repository contains ..." plus an Amazon docs link must NOT
        # be classified official — the phrase establishes nothing about
        # ownership, and the README even disclaims affiliation.
        self.assertEqual(
            classify_source_type("https://github.com/independent-dev/ads-sdk",
                                 content=COMMUNITY_SDK_README),
            "community")
        self.assertEqual(
            classify_source_type(
                "https://raw.githubusercontent.com/independent-dev/ads-sdk"
                "/main/README.md",
                content=COMMUNITY_SDK_README),
            "community")

    def test_explicit_ownership_marker_is_official(self):
        content = ("Utilities for the Ads API. This repository is maintained "
                   "by Amazon. Docs: https://advertising.amazon.com/API/docs/en-us\n")
        self.assertEqual(
            classify_source_type("https://example.example/utils",
                                 content=content),
            "official")

    def test_generic_wording_without_amazon_title_stays_community(self):
        # generic self-reference + docs link, but the document's own title
        # does not name an Amazon product -> community
        content = ("# awesome-tool\nThis repository will be home to helpers "
                   "for the Amazon Ads API. See "
                   "https://advertising.amazon.com/API/docs/en-us\n")
        self.assertEqual(
            classify_source_type("https://github.com/someone/awesome-tool",
                                 content=content),
            "community")

    def test_mid_title_amazon_mention_is_not_official(self):
        # Regression (strict review): a third-party README whose title merely
        # CONTAINS "Amazon Ads" ("Community SDK for Amazon Ads") must stay
        # community — the self-title marker is anchored to the START of the
        # title, so a mid-title mention proves nothing about ownership.
        content = ("# Community SDK for Amazon Ads\n"
                   "An unofficial wrapper. Docs: "
                   "https://advertising.amazon.com/API/docs/en-us\n")
        self.assertEqual(
            classify_source_type("https://github.com/indie/ads-sdk",
                                 content=content),
            "community")

    def test_leading_amazon_title_is_official(self):
        # The positive side of the same rule: the document's own title BEGINS
        # with the Amazon product name -> self-titled Amazon artifact.
        content = ("# Amazon Ads advanced tools docs\n"
                   "See https://advertising.amazon.com/API/docs/en-us/ for "
                   "the documentation.\n")
        self.assertEqual(
            classify_source_type("https://mirror.example/readme.md",
                                 content=content),
            "official")

    def test_transport_does_not_determine_authority(self):
        # 5a. the SAME Amazon-owned content from a different host is still
        # official (authority follows content, not URL)
        self.assertEqual(
            classify_source_type("https://mirror.example/readme.md",
                                 content=AMAZON_DOCS_README),
            "official")
        # 5b. the SAME raw URL with unrelated content is community (the
        # transport alone never makes a source official)
        self.assertEqual(
            classify_source_type(
                "https://raw.githubusercontent.com/amzn/ads-advanced-tools-docs"
                "/main/README.md",
                content=UNRELATED_README),
            "community")

    def test_content_evidence_requires_both_signals(self):
        docs_link_only = "See https://advertising.amazon.com/API/docs/en-us.\n"
        ownership_only = "This repository contains resources.\n"
        self.assertEqual(
            classify_source_type("https://example.com/a", content=docs_link_only),
            "community")
        self.assertEqual(
            classify_source_type("https://example.com/b", content=ownership_only),
            "community")

    def test_adapt_url_uses_cached_content_for_classification(self):
        # end to end at adapter level: the cache file provides the evidence
        import hashlib
        import tempfile
        from pathlib import Path
        content = AMAZON_DOCS_README
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        url = "https://raw.githubusercontent.com/amzn/ads-advanced-tools-docs/main/README.md"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "cache").mkdir()
            (base / "cache" / f"{sha}.md").write_text(content, encoding="utf-8")
            (base / "claims").mkdir()
            (base / "claims" / f"{sha}.json").write_text(json.dumps({
                "schema_version": 1, "source_url": url, "sha256": sha,
                "fetched_at": "2026-09-29T00:00:00+00:00",
                "extracted_at": "2026-09-29T00:00:00+00:00", "status": "ok",
                "claims": [{"claim": "The docs repository exists.",
                            "quote": "Amazon Ads advanced tools docs",
                            "topic_hint": "docs-repo", "confidence": "high"}],
            }), encoding="utf-8")
            states = {url: SourceState(url=url, status="ok",
                                       fetched_at="2026-09-29T00:00:00+00:00",
                                       sha256=sha)}
            result = adapt_url(url, states, base / "claims",
                               cache_dir=base / "cache")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["facts"][0]["source_type"], "official")


class BuildFactsTests(unittest.TestCase):
    def test_fact_shape_matches_validator_input(self):
        facts = build_facts(doc(), state())
        self.assertEqual(len(facts), 2)
        f = facts[0]
        self.assertEqual(f["url"], "https://advertising.amazon.com/API/docs/en-us")
        self.assertEqual(f["date"], T0)
        self.assertEqual(f["content"], "The Amazon Ads API is a REST API.")
        self.assertEqual(f["source_type"], "official")
        self.assertEqual(f["community_agree_count"], 0)
        self.assertEqual(f["topic_id"], "amazon-ads-api-overview")

    def test_extractor_provenance_preserved(self):
        f = build_facts(doc(), state())[0]
        self.assertEqual(f["quote"], CLAIMS[0]["quote"])
        self.assertEqual(f["confidence"], "high")
        self.assertEqual(f["topic_hint"], CLAIMS[0]["topic_hint"])
        self.assertEqual(f["sha256"], SHA)

    def test_first_sighting_is_changed(self):
        # Claims extracted from the latest fetch: no later confirming run.
        facts = build_facts(doc(fetched_at=T0), state(fetched_at=T0))
        self.assertEqual((facts[0]["is_changed"], facts[0]["last_run"]), ("Y", None))

    def test_confirmed_unchanged_across_runs(self):
        # A later fetch re-saw the same sha256: content survived unchanged.
        facts = build_facts(doc(fetched_at=T0), state(fetched_at=T1))
        self.assertEqual((facts[0]["is_changed"], facts[0]["last_run"]),
                         ("N", T0))

    def test_changed_content_is_not_stable(self):
        # State sha differs from the claims doc: this claims version is stale.
        facts = build_facts(doc(sha="b" * 64), state(sha="a" * 64, fetched_at=T1))
        self.assertEqual((facts[0]["is_changed"], facts[0]["last_run"]), ("Y", None))

    def test_malformed_claims_skipped_and_reported(self):
        claims = [dict(CLAIMS[0]), {"claim": "   "}, "not-a-dict"]
        with self.assertLogs("pipeline.adapter", level="WARNING"):
            facts = build_facts(doc(claims=claims), state())
        self.assertEqual(len(facts), 1)

    def test_no_claims_list_raises(self):
        with self.assertRaises(AdapterError):
            build_facts({"status": "ok"}, state())

    def test_deterministic(self):
        d, s = doc(), state()
        self.assertEqual(build_facts(copy.deepcopy(d), s),
                         build_facts(copy.deepcopy(d), s))


class AdaptUrlTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.claims_dir = Path(self._tmp.name)
        path = self.claims_dir / f"{SHA}.json"
        path.write_text(json.dumps(doc()), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_ok_path_returns_facts(self):
        result = adapt_url("https://advertising.amazon.com/API/docs/en-us",
                           {"https://advertising.amazon.com/API/docs/en-us": state()},
                           self.claims_dir)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["fact_count"], 2)
        self.assertEqual(len(result["facts"]), 2)

    def test_url_not_in_state(self):
        result = adapt_url("https://never.example/", {}, self.claims_dir)
        self.assertEqual(result["status"], "error")
        self.assertIn("not found in fetch state", result["error"])

    def test_never_successfully_fetched(self):
        entry = SourceState(url="https://x/", status="error", fetched_at=T0)
        result = adapt_url("https://x/", {"https://x/": entry}, self.claims_dir)
        self.assertEqual(result["status"], "error")
        self.assertIn("never successfully fetched", result["error"])

    def test_missing_claims_file(self):
        entry = state(url="https://advertising.amazon.com/y", sha="c" * 64)
        result = adapt_url("https://advertising.amazon.com/y",
                           {"https://advertising.amazon.com/y": entry},
                           self.claims_dir)
        self.assertEqual(result["status"], "error")
        self.assertIn("run the Extractor first", result["error"])

    def test_corrupt_claims_file(self):
        (self.claims_dir / f"{SHA}.json").write_text("{bad", encoding="utf-8")
        result = adapt_url("https://advertising.amazon.com/API/docs/en-us",
                           {"https://advertising.amazon.com/API/docs/en-us": state()},
                           self.claims_dir)
        self.assertEqual(result["status"], "error")
        self.assertIn("corrupt claims file", result["error"])


class CliTests(unittest.TestCase):
    def setUp(self):
        self._root_handlers = logging.getLogger().handlers[:]

    def tearDown(self):
        logging.getLogger().handlers[:] = self._root_handlers

    def test_cli_prints_facts_array(self):
        with tempfile.TemporaryDirectory() as tmp:
            claims_dir = Path(tmp)
            (claims_dir / f"{SHA}.json").write_text(json.dumps(doc()), encoding="utf-8")
            state_file = Path(tmp) / "fetch_state.json"
            state_file.write_text(json.dumps(
                {"https://advertising.amazon.com/API/docs/en-us":
                 {"status": "ok", "fetched_at": T0, "sha256": SHA}}), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["https://advertising.amazon.com/API/docs/en-us"],
                            state_path=state_file, claims_path=claims_dir)
        self.assertEqual(code, 0)
        facts = json.loads(out.getvalue())
        self.assertEqual(len(facts), 2)
        self.assertEqual(facts[0]["source_type"], "official")

    def test_cli_all_failed_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "fetch_state.json"
            state_file.write_text(json.dumps(
                {"https://never.example/": {"status": "ok", "fetched_at": T0}}),
                encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["https://never.example/"], state_path=state_file,
                            claims_path=tmp)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())[0]["status"], "error")


if __name__ == "__main__":
    unittest.main()
