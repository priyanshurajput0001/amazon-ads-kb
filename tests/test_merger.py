"""Tests for pipeline.merger — offline; the LLM seam is faked.

Facts here are shaped exactly like Validator output: the original fields
plus "confidence_score" and "status".
"""

import copy
import io
import json
import logging
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pipeline.merger import LlmError, MergerError, _parse_label, main, merge_facts

DATE_OLD = "2026-09-20T00:00:00+00:00"
DATE_NEW = "2026-09-26T00:00:00+00:00"


def fact(url, content, source_type="official", status="valid", score=0.6,
         date=DATE_OLD, **over):
    """Validator-shaped fact; `over` patches any field."""
    base = {
        "url": url,
        "date": date,
        "confidence_pct": 50,
        "content": content,
        "is_changed": "Y",
        "last_run": None,
        "source_type": source_type,
        "community_agree_count": 0,
        "confidence_score": score,
        "status": status,
    }
    base.update(over)
    return base


def fake_llm(default="duplicate", overrides=None, calls=None):
    """LLM seam fake: `default` label, per-content-pair `overrides`.

    An override (or default) that is an Exception instance is raised."""
    overrides = overrides or {}

    def _llm(a, b):
        if calls is not None:
            calls.append((a["url"], b["url"]))
        value = overrides.get((a["content"], b["content"]), default)
        if isinstance(value, Exception):
            raise value
        return value

    return _llm


O = "official"
C = "community"
CONFLICT_TEXT = "Sponsored Brands campaigns require an active storefront."
CONFLICT_TEXT_NOT = "Sponsored Brands campaigns do not require an active storefront."
DUP_A = "Application approval may take up to 1 business day."
DUP_B = "Applications for Amazon Ads API access may take up to 1 business day to be approved."
COMP_A = "Direct advertisers, partners, and integrators are all eligible to apply."
COMP_B = "Application approval may take up to 1 business day."


class SingleSourceTests(unittest.TestCase):
    def test_single_fact_is_single_source_without_llm(self):
        calls = []
        f = fact("https://ads.amazon/a", "Sponsored Products ads appear in shopping results.")
        out = merge_facts([f], llm=fake_llm("duplicate", calls=calls))
        self.assertEqual(out, [{
            "content": f["content"],
            "sources": [{"url": f["url"], "date": f["date"], "source_type": O}],
            "confidence_score": 0.6,
            "resolution": "single_source",
            "status": "valid",
        }])
        self.assertEqual(calls, [])  # singleton group: the LLM is never invoked

    def test_facts_without_topic_id_are_separate_singletons(self):
        calls = []
        out = merge_facts([
            fact("https://ads.amazon/a", "Sponsored Products uses cost-per-click billing."),
            fact("https://blog.example/x", "Sponsored Display uses cost-per-click billing."),
        ], llm=fake_llm("complementary", calls=calls))
        self.assertEqual([f["resolution"] for f in out],
                         ["single_source", "single_source"])
        self.assertEqual(calls, [])  # no topic_id -> no group -> no pair calls


class DuplicateTests(unittest.TestCase):
    def test_duplicates_merge_into_one_fact(self):
        out = merge_facts([
            fact("https://ads.amazon/a", DUP_A, score=0.6, topic_id="approval"),
            fact("https://docs.example/b", DUP_B, score=0.75, topic_id="approval"),
        ], llm=fake_llm("duplicate"))
        self.assertEqual(len(out), 1)
        merged = out[0]
        self.assertEqual(merged["resolution"], "duplicate_merged")
        self.assertEqual(merged["content"], DUP_B)  # highest-scoring member, verbatim
        self.assertEqual(merged["confidence_score"], 0.75)
        self.assertEqual([s["url"] for s in merged["sources"]],
                         ["https://ads.amazon/a", "https://docs.example/b"])

    def test_all_contributing_sources_preserved(self):
        out = merge_facts([
            fact(f"https://mirror.example/{c}", DUP_A, topic_id="approval")
            for c in "abcd"
        ], llm=fake_llm("duplicate"))
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]["sources"]), 4)
        self.assertEqual({s["url"] for s in out[0]["sources"]},
                         {f"https://mirror.example/{c}" for c in "abcd"})


class ConflictTests(unittest.TestCase):
    def llm_for(self):
        return fake_llm("conflicting")

    def test_authority_beats_recency(self):
        # Official is OLDER and alone; community is newer — authority still wins.
        out = merge_facts([
            fact("https://ads.amazon/a", CONFLICT_TEXT, source_type=O,
                 date=DATE_OLD, topic_id="storefront"),
            fact("https://forum.example/t1", CONFLICT_TEXT_NOT, source_type=C,
                 date=DATE_NEW, topic_id="storefront"),
        ], llm=self.llm_for())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["content"], CONFLICT_TEXT)
        self.assertEqual(out[0]["resolution"], "conflict_resolved_by_authority")

    def test_community_majority_does_not_override_official(self):
        # Three community duplicates (a majority) still lose to one official.
        out = merge_facts([
            fact("https://ads.amazon/a", CONFLICT_TEXT, source_type=O,
                 topic_id="storefront"),
            *[fact(f"https://forum.example/t{i}", CONFLICT_TEXT_NOT,
                   source_type=C, topic_id="storefront") for i in range(3)],
        ], llm=fake_llm("conflicting", overrides={
            (CONFLICT_TEXT_NOT, CONFLICT_TEXT_NOT): "duplicate",
        }))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["content"], CONFLICT_TEXT)
        self.assertEqual(out[0]["resolution"], "conflict_resolved_by_authority")
        self.assertEqual(len(out[0]["sources"]), 1)

    def test_recency_wins_when_authority_tied(self):
        out = merge_facts([
            fact("https://ads.amazon/old", CONFLICT_TEXT, date=DATE_OLD,
                 topic_id="storefront"),
            fact("https://ads.amazon/new", CONFLICT_TEXT_NOT, date=DATE_NEW,
                 topic_id="storefront"),
        ], llm=self.llm_for())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["content"], CONFLICT_TEXT_NOT)
        self.assertEqual(out[0]["resolution"], "conflict_resolved_by_recency")

    def test_majority_wins_when_authority_and_date_tied(self):
        # Cluster of two agreeing official sources beats one official source.
        out = merge_facts([
            fact("https://ads.amazon/a1", CONFLICT_TEXT, topic_id="storefront"),
            fact("https://ads.amazon/a2", CONFLICT_TEXT, topic_id="storefront"),
            fact("https://ads.amazon/b", CONFLICT_TEXT_NOT, topic_id="storefront"),
        ], llm=fake_llm("conflicting", overrides={
            (CONFLICT_TEXT, CONFLICT_TEXT): "duplicate",
        }))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["content"], CONFLICT_TEXT)
        self.assertEqual(out[0]["resolution"], "conflict_resolved_by_majority")
        self.assertEqual(len(out[0]["sources"]), 2)

    def test_completely_tied_conflict_keeps_both(self):
        out = merge_facts([
            fact("https://ads.amazon/a", CONFLICT_TEXT, topic_id="storefront"),
            fact("https://ads.amazon/b", CONFLICT_TEXT_NOT, topic_id="storefront"),
        ], llm=self.llm_for())
        self.assertEqual(len(out), 2)  # never silently discard one
        for f in out:
            self.assertEqual(f["resolution"], "unresolved_conflict")
        self.assertEqual([f["content"] for f in out],
                         [CONFLICT_TEXT, CONFLICT_TEXT_NOT])

    def test_llm_not_called_during_conflict_resolution(self):
        calls = []
        merge_facts([
            fact("https://ads.amazon/a", CONFLICT_TEXT, topic_id="storefront"),
            fact("https://ads.amazon/b", CONFLICT_TEXT_NOT, date=DATE_NEW,
                 topic_id="storefront"),
        ], llm=fake_llm("conflicting", calls=calls))
        # Exactly one classification call; resolution itself is pure Python.
        self.assertEqual(len(calls), 1)


class ComplementaryTests(unittest.TestCase):
    def test_complementary_merge_preserves_both_details(self):
        out = merge_facts([
            fact("https://ads.amazon/a", COMP_A, topic_id="eligibility"),
            fact("https://ads.amazon/b", COMP_B, topic_id="eligibility"),
        ], llm=fake_llm("complementary"))
        self.assertEqual(len(out), 1)
        merged = out[0]
        self.assertEqual(merged["resolution"], "complementary_merge")
        self.assertEqual(merged["content"], f"{COMP_A} {COMP_B}")
        self.assertEqual(len(merged["sources"]), 2)

    def test_no_fabricated_information_in_complementary_merge(self):
        out = merge_facts([
            fact("https://ads.amazon/a", COMP_A, topic_id="eligibility"),
            fact("https://ads.amazon/b", COMP_B, topic_id="eligibility"),
        ], llm=fake_llm("complementary"))
        # The merged content is EXACTLY the two originals joined — nothing added.
        self.assertEqual(out[0]["content"], COMP_A + " " + COMP_B)
        self.assertNotIn("however", out[0]["content"].lower())

    def test_complementary_merge_blocked_by_conflicting_pair(self):
        # A~B complementary, B~C complementary, but A~C conflicting: C must not
        # be pulled into the merged CONTENT. The A~C conflict is then resolved
        # by precedence — {A,B} (2 URLs) beats singleton C by majority — so C
        # is dropped and the surviving cluster records the conflict rule.
        out = merge_facts([
            fact("https://ads.amazon/a", COMP_A, topic_id="t"),
            fact("https://ads.amazon/b", COMP_B, topic_id="t"),
            fact("https://ads.amazon/c", CONFLICT_TEXT, topic_id="t"),
        ], llm=fake_llm("complementary", overrides={
            (COMP_A, CONFLICT_TEXT): "conflicting",
            (COMP_B, CONFLICT_TEXT): "complementary",
        }))
        self.assertEqual(len(out), 1)
        merged = out[0]
        self.assertEqual(merged["content"], f"{COMP_A} {COMP_B}")  # C's text kept out
        self.assertEqual(merged["resolution"], "conflict_resolved_by_majority")
        self.assertEqual([s["url"] for s in merged["sources"]],
                         ["https://ads.amazon/a", "https://ads.amazon/b"])


class PassThroughTests(unittest.TestCase):
    def test_rejected_facts_pass_through_unchanged(self):
        rejected = fact("https://blog.example/x", CONFLICT_TEXT_NOT,
                        source_type=C, status="rejected", score=0.3,
                        reason="contradicts official source(s) https://ads.amazon/a",
                        topic_id="storefront")
        calls = []
        out = merge_facts([rejected,
                           fact("https://ads.amazon/a", CONFLICT_TEXT,
                                topic_id="storefront")],
                          llm=fake_llm("conflicting", calls=calls))
        self.assertEqual(out[0], rejected)  # byte-for-byte, original fields intact
        self.assertEqual(out[1]["resolution"], "single_source")
        self.assertEqual(calls, [])  # rejected fact is never even compared


class ConfidenceStatusTests(unittest.TestCase):
    def test_strongest_confidence_and_status_preserved(self):
        out = merge_facts([
            fact("https://ads.amazon/a", DUP_A, score=0.6,
                 status="valid_low_confidence", topic_id="approval"),
            fact("https://docs.example/b", DUP_B, score=0.75,
                 status="valid", topic_id="approval"),
        ], llm=fake_llm("duplicate"))
        self.assertEqual(out[0]["confidence_score"], 0.75)
        self.assertEqual(out[0]["status"], "valid")


class FailSafeTests(unittest.TestCase):
    def test_invalid_llm_json_reported_and_pair_kept_separate(self):
        facts = [
            fact("https://ads.amazon/a", DUP_A, topic_id="approval"),
            fact("https://docs.example/b", DUP_B, topic_id="approval"),
        ]
        with self.assertLogs("pipeline.merger", level="WARNING") as captured:
            out = merge_facts(facts, llm=fake_llm(LlmError("LLM returned invalid JSON")))
        text = "\n".join(captured.output)
        self.assertIn("LLM returned invalid JSON", text)
        self.assertIn("https://ads.amazon/a", text)
        self.assertIn("https://docs.example/b", text)
        self.assertEqual([f["resolution"] for f in out],
                         ["single_source", "single_source"])  # no silent guess

    def test_invalid_label_handled_safely(self):
        facts = [
            fact("https://ads.amazon/a", DUP_A, topic_id="approval"),
            fact("https://docs.example/b", DUP_B, topic_id="approval"),
        ]
        with self.assertLogs("pipeline.merger", level="WARNING"):
            out = merge_facts(facts, llm=fake_llm("kinda-same"))
        self.assertEqual(len(out), 2)  # kept separate, nothing dropped


class LlmParseTests(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(_parse_label('{"label": "duplicate"}'), "duplicate")

    def test_fenced_json(self):
        self.assertEqual(_parse_label('```json\n{"label": "conflicting"}\n```'),
                         "conflicting")

    def test_invalid_json_raises(self):
        with self.assertRaises(LlmError):
            _parse_label("they are the same, I think")

    def test_invalid_label_raises(self):
        with self.assertRaises(LlmError):
            _parse_label('{"label": "same"}')


class DeterminismTests(unittest.TestCase):
    def test_same_input_same_output(self):
        facts = [
            fact("https://ads.amazon/a", CONFLICT_TEXT, topic_id="storefront"),
            fact("https://forum.example/t1", CONFLICT_TEXT_NOT, source_type=C,
                 topic_id="storefront"),
            fact("https://ads.amazon/x", DUP_A, topic_id="approval"),
            fact("https://docs.example/y", DUP_B, topic_id="approval"),
            fact("https://blog.example/r", "Rumored feature.", status="rejected",
                 score=0.15),
        ]
        first = merge_facts(copy.deepcopy(facts), llm=fake_llm("conflicting", overrides={
            (DUP_A, DUP_B): "duplicate"}))
        second = merge_facts(copy.deepcopy(facts), llm=fake_llm("conflicting", overrides={
            (DUP_A, DUP_B): "duplicate"}))
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))

    def test_output_order_follows_input_order(self):
        out = merge_facts([
            fact("https://blog.example/r", "Rumored.", status="rejected", score=0.15),
            fact("https://ads.amazon/a", CONFLICT_TEXT, topic_id="storefront"),
            fact("https://ads.amazon/x", DUP_A, topic_id="approval"),
            fact("https://docs.example/y", DUP_B, topic_id="approval"),
        ], llm=fake_llm("conflicting", overrides={(DUP_A, DUP_B): "duplicate"}))
        self.assertEqual([f.get("url") or f["sources"][0]["url"] for f in out],
                         ["https://blog.example/r", "https://ads.amazon/a",
                          "https://ads.amazon/x"])


class ContractTests(unittest.TestCase):
    def test_not_a_list_rejected(self):
        with self.assertRaises(MergerError):
            merge_facts({"url": "https://ads.amazon/a"})

    def test_missing_status_rejected(self):
        bad = fact("https://ads.amazon/a", "Some claim.")
        del bad["status"]
        with self.assertRaises(MergerError):
            merge_facts([bad])

    def test_unknown_status_rejected(self):
        with self.assertRaises(MergerError):
            merge_facts([fact("https://a.example/", "x", status="maybe")])

    def test_participating_fact_needs_confidence_score(self):
        bad = fact("https://ads.amazon/a", "Some claim.")
        del bad["confidence_score"]
        with self.assertRaises(MergerError):
            merge_facts([bad])


class CliTests(unittest.TestCase):
    def setUp(self):
        self._root_handlers = logging.getLogger().handlers[:]

    def tearDown(self):
        logging.getLogger().handlers[:] = self._root_handlers

    def _facts(self):
        return [
            fact("https://ads.amazon/a", DUP_A, topic_id="approval"),
            fact("https://docs.example/b", DUP_B, topic_id="approval"),
        ]

    def test_cli_reads_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "facts.json"
            path.write_text(json.dumps(self._facts()), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = main([str(path)], llm=fake_llm("duplicate"))
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["resolution"], "duplicate_merged")

    def test_cli_reads_stdin(self):
        payload = json.dumps({"facts": self._facts()})
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO(payload)), redirect_stdout(out):
            code = main(["-"], llm=fake_llm("duplicate"))
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out.getvalue())), 1)

    def test_cli_invalid_json_exit_2(self):
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            with redirect_stderr(err):
                code = main([str(path)])
        self.assertEqual(code, 2)
        self.assertIn("invalid JSON", err.getvalue())


if __name__ == "__main__":
    unittest.main()
