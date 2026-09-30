"""Tests for the relevance gate (pipeline/relevance.py) — review step 4.

Pinned behaviors:
  - named off-topic claims (the four that reached the bundle: alexa, pecos,
    smoke-framework, computer-vision-in-excel, plus amazon pay) are dropped
    DETERMINISTICALLY by drop tokens, with no LLM call
  - ads claims are kept deterministically by the allow-list
  - borderline claims make exactly ONE seam call; the verdict is cached, so
    a second gate run replays it without the seam
  - every dropped claim lands in the dropped log with a reason; re-running
    the gate is idempotent (log byte-identical, no duplicate entries)
  - a seam failure keeps the claim (fail-open, never silent loss)
  - the gate sits between Extract and Adapter in the orchestrator
"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from pipeline.relevance import (
    DEFAULT_DROPPED_PATH,
    filter_claims,
    gate_claim,
    log_dropped,
)

URL = "https://github.com/amzn"

OFF_TOPIC = [
    "Amazon's alexa-skills-kit-js repository provides SDK and example code "
    "for building voice-enabled skills for the Amazon Echo.",
    "The alexa-skills-kit-js repository is archived.",
    "Amazon's GitHub organization hosts PECOS, described as \"Prediction "
    "for Enormous and Correlated Spaces\".",
    "Amazon's GitHub organization hosts smoke-framework, a light-weight "
    "server-side service framework written in the Swift programming "
    "language.",
    "Amazon's GitHub organization hosts a repository on computer vision "
    "basics implemented in Microsoft Excel using just formulas.",
    "Amazon's GitHub organization includes an Amazon Pay API SDK repository "
    "for PHP.",
]

ON_TOPIC = [
    "Amazon maintains a public GitHub repository named ads-advanced-tools-"
    "docs, described as providing code samples and supplements for the "
    "Amazon Ads advanced tools center.",
    "The amzn/selling-partner-api-models repository contains OpenAPI models "
    "for developers to use when developing software to call Selling Partner "
    "APIs.",
    "Bulksheets is a spreadsheet-based tool used to manage sponsored ads "
    "campaigns.",
    "Amazon Marketing Stream provides hourly campaign metrics and "
    "notifications of campaign changes in near real time.",
]

BORDERLINE = ("Amazon's GitHub organization (github.com/amzn) lists 176 "
              "repositories.")


def never_llm(claim):
    raise AssertionError("LLM seam must not be called for deterministic cases")


class GateClaimTests(unittest.TestCase):
    def test_off_topic_claims_dropped_deterministically(self):
        for claim in OFF_TOPIC:
            keep, reason, by = gate_claim(claim, llm=never_llm)
            self.assertFalse(keep, claim)
            self.assertEqual(by, "drop-tokens")
            self.assertIn("off-topic token", reason)

    def test_on_topic_claims_kept_deterministically(self):
        for claim in ON_TOPIC:
            keep, _, by = gate_claim(claim, llm=never_llm)
            self.assertTrue(keep, claim)
            self.assertEqual(by, "allow-list")

    def test_borderline_claim_uses_the_seam_once_and_caches(self):
        calls = []

        def seam(claim):
            calls.append(claim)
            return True

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "gate_cache.json"
            keep1, _, by1 = gate_claim(BORDERLINE, url=URL, llm=seam,
                                       cache_path=cache, today="2026-09-30")
            self.assertTrue(keep1)
            self.assertEqual(by1, "llm")
            self.assertEqual(len(calls), 1)
            # second run: the recorded verdict replays, no seam call
            keep2, _, by2 = gate_claim(BORDERLINE, url=URL, llm=seam,
                                       cache_path=cache, today="2026-09-30")
            self.assertTrue(keep2)
            self.assertEqual(by2, "llm-cache")
            self.assertEqual(len(calls), 1)

    def test_seam_failure_keeps_the_claim_fail_open(self):
        from pipeline.relevance import GateError

        def broken(claim):
            raise GateError("claude CLI exited 1")

        keep, _, by = gate_claim(BORDERLINE, llm=broken, cache_path=None)
        self.assertTrue(keep)
        self.assertEqual(by, "error")


class FilterClaimsTests(unittest.TestCase):
    def doc(self):
        return {
            "schema_version": 1, "source_url": URL, "sha256": "abc",
            "fetched_at": "2026-09-27T00:00:00+00:00", "status": "ok",
            "claims": [
                {"claim": c, "quote": "q", "topic_hint": "h",
                 "confidence": "high"}
                for c in OFF_TOPIC[:3] + ON_TOPIC[:2] + [BORDERLINE]
            ],
        }

    def test_filter_splits_keeps_and_drops_and_writes_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            dropped_path = Path(tmp) / "dropped.json"
            cache = Path(tmp) / "gate_cache.json"
            kept, dropped = filter_claims(
                self.doc(), llm=lambda claim: True,
                dropped_path=dropped_path, cache_path=cache,
                today="2026-09-30")
            self.assertEqual(len(kept), 3)   # 2 on-topic + 1 borderline(kept)
            self.assertEqual(len(dropped), 3)
            log = json.loads(dropped_path.read_text(encoding="utf-8"))
            self.assertEqual(len(log), 3)
            for record in log.values():
                self.assertEqual(record["url"], URL)
                self.assertTrue(record["reason"])
                self.assertEqual(record["date"], "2026-09-30")

            # idempotent: re-filtering the same doc adds no entries
            before = dropped_path.read_bytes()
            filter_claims(self.doc(), llm=lambda claim: True,
                          dropped_path=dropped_path, cache_path=cache,
                          today="2026-10-01")
            self.assertEqual(dropped_path.read_bytes(), before)

    def test_claims_doc_not_mutated(self):
        doc = self.doc()
        snapshot = json.dumps(doc, sort_keys=True)
        with tempfile.TemporaryDirectory() as tmp:
            filter_claims(doc, llm=lambda claim: True,
                          dropped_path=Path(tmp) / "dropped.json",
                          cache_path=Path(tmp) / "gate_cache.json")
        self.assertEqual(json.dumps(doc, sort_keys=True), snapshot)


class ShippedCacheReplayTests(unittest.TestCase):
    """Step 3 (review 3): `state/gate_cache.json` ships with the repo. A
    fresh clone must replay every recorded borderline verdict with NO LLM
    call at all — proven here by a seam that RAISES: without the shipped
    cache the fail-open path would keep every borderline claim and stamp it
    `decided_by: 'error'`; with the shipped cache plus drop log the gate is
    deterministic across machines and rebuilds."""

    BORDERLINE_KEPT = BORDERLINE
    BORDERLINE_DROPPED = ("The organization page lists its location as "
                          "Seattle, Washington.")

    def _shipped_state(self, tmp):
        """The cache and drop log exactly as the pipeline writes them."""
        cache = Path(tmp) / "gate_cache.json"
        dropped = Path(tmp) / "dropped.json"
        entries = {}
        for claim, keep in ((self.BORDERLINE_KEPT, True),
                            (self.BORDERLINE_DROPPED, False)):
            sha = hashlib.sha256(claim.encode("utf-8")).hexdigest()
            entries[sha] = {"keep": keep,
                            "reason": "borderline claim judged by the LLM seam",
                            "url": URL, "date": "2026-09-28"}
        cache.write_text(
            json.dumps(entries, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        log = {}
        for claim, reason, decided_by in (
                (OFF_TOPIC[0], "off-topic token(s): alexa", "drop-tokens"),
                (self.BORDERLINE_DROPPED,
                 "borderline claim judged by the LLM seam", "llm")):
            key = hashlib.sha256(
                f"{URL}|{claim}".encode("utf-8")).hexdigest()
            log[key] = {"claim": claim, "url": URL, "reason": reason,
                        "decided_by": decided_by, "date": "2026-09-28"}
        dropped.write_text(
            json.dumps(log, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        return cache, dropped

    def _doc(self):
        return {"source_url": URL, "claims": [
            {"claim": c, "quote": "q", "topic_hint": "h",
             "confidence": "high"}
            for c in OFF_TOPIC[:1] + ON_TOPIC[:1]
            + [self.BORDERLINE_KEPT, self.BORDERLINE_DROPPED]]}

    def test_replay_is_deterministic_with_the_seam_raising(self):
        from pipeline.relevance import GateError

        def exploding(claim):
            raise GateError("no LLM backend available (claude CLI not found)")

        with tempfile.TemporaryDirectory() as tmp:
            cache, dropped = self._shipped_state(tmp)
            log_before = dropped.read_bytes()
            first = filter_claims(self._doc(), llm=exploding,
                                  dropped_path=dropped, cache_path=cache,
                                  today="2026-10-05")
            second = filter_claims(self._doc(), llm=exploding,
                                   dropped_path=dropped, cache_path=cache,
                                   today="2026-10-05")
            # deterministic: two replays agree exactly
            self.assertEqual(first, second)
            kept, dropped_records = first
            # off-topic dropped by tokens; on-topic kept by allow-list;
            # borderline kept/dropped exactly per the shipped verdicts —
            # NOT per the fail-open default the raising seam would give
            self.assertEqual([c["claim"] for c in kept],
                             [ON_TOPIC[0], self.BORDERLINE_KEPT])
            self.assertEqual([r["claim"] for r in dropped_records],
                             [OFF_TOPIC[0], self.BORDERLINE_DROPPED])
            self.assertEqual(dropped_records[1]["decided_by"], "llm-cache")
            # the seam was never consulted: no fail-open 'error' verdicts
            self.assertTrue(all(r["decided_by"] != "error"
                                for r in dropped_records))
            # and the shipped drop log is untouched by the replay
            self.assertEqual(dropped.read_bytes(), log_before)


class GateInOrchestratorTests(unittest.TestCase):
    """The gate sits between Extract and Adapter: off-topic claims never
    become facts; the report records the gate's counts."""

    def test_off_topic_claims_never_reach_the_adapter(self):
        import hashlib
        from pipeline.fetch import FetchResult
        from pipeline.orchestrate import orchestrate

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            state, cache, claims, knowledge = (base / "fetch_state.json",
                                               base / "cache", base / "claims",
                                               base / "knowledge")
            alexa_claim = ("Amazon's alexa-skills-kit-js repository "
                           "provides SDK and example code for building "
                           "voice-enabled skills for the Amazon Echo.")
            bulk_claim = ("Bulksheets is a spreadsheet-based tool used to "
                          "manage sponsored ads campaigns.")
            md = f"Amazon org page. {alexa_claim} {bulk_claim}"
            url = "https://github.com/amzn"

            def fetcher(urls, fetched_at):
                result = FetchResult(url=url, strategy="fake",
                                     content_type="markdown", title=None,
                                     content=md,
                                     sha256=hashlib.sha256(
                                         md.encode()).hexdigest(),
                                     fetched_at=fetched_at)
                return [result.to_json() | {"ok": True}]

            extract_claims = [
                {"claim": alexa_claim, "quote": alexa_claim,
                 "topic_hint": "alexa-skills-kit-js", "confidence": "high"},
                {"claim": bulk_claim, "quote": bulk_claim,
                 "topic_hint": "bulksheets-overview", "confidence": "high"},
            ]

            report = orchestrate(
                [url], state_path=state, cache_path=cache, claims_path=claims,
                knowledge_dir=knowledge, fetcher=fetcher,
                extract_llm=lambda content, source_url: extract_claims,
                gate_llm=lambda claim: True,
                classify_llm=lambda a, b: "complementary",
                match_llm=lambda *a: False,
                route_llm=lambda claim, candidates:
                    "bulksheets-and-bulk-operations",
                now=__import__("datetime").datetime(
                    2026, 9, 30, tzinfo=__import__("datetime").timezone.utc))
            entry = report["urls"][0]
            self.assertEqual(entry["gate"], {"kept": 1, "dropped": 1})
            self.assertEqual(entry["adapter"]["fact_count"], 1)
            self.assertEqual(report["facts"]["valid"], 1)
            dropped = json.loads((base / "dropped.json").read_text())
            self.assertEqual(len(dropped), 1)
            self.assertIn("alexa", next(iter(dropped.values()))["reason"])


if __name__ == "__main__":
    unittest.main()
