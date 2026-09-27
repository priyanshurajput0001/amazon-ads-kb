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
