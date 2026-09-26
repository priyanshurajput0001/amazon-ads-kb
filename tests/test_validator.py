"""Tests for pipeline.validator — pure-function tests, fully offline.

The Validator has no LLM seam and no I/O, so every test drives
validate_facts() directly with synthetic fact dicts.
"""

import copy
import io
import json
import logging
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pipeline.validator as pv
from pipeline.validator import ValidationError, main, validate_facts

DATE = "2026-09-26T00:00:00+00:00"
LAST_RUN = "2026-09-20T00:00:00+00:00"


def fact(url, content, source_type="official", **over):
    """Build a minimal well-formed fact; `over` patches any field."""
    base = {
        "url": url,
        "date": DATE,
        "confidence_pct": 50,          # informational only; must never matter
        "content": content,
        "is_changed": "Y",
        "last_run": None,
        "source_type": source_type,
        "community_agree_count": 0,
    }
    base.update(over)
    return base


def stable(**over):
    """Patch: unchanged now and a prior run existed -> eligible for +0.10."""
    return {"is_changed": "N", "last_run": LAST_RUN, **over}


class ConfidenceScoringTests(unittest.TestCase):
    """Required scenarios 1-6: base scores, corroboration, stability, clamp."""

    def test_official_fact_is_valid(self):
        out = validate_facts([fact("https://ads.amazon/a", "Sponsored Products ads appear in shopping results.")])
        self.assertEqual(out[0]["status"], "valid")
        self.assertEqual(out[0]["confidence_score"], 0.6)
        self.assertNotIn("reason", out[0])  # optional for clean valid

    def test_community_with_three_people_is_low_confidence(self):
        out = validate_facts([fact("https://forum.example/t1",
                                   "Amazon Marketing Stream needs an AWS account.",
                                   source_type="community", community_agree_count=3)])
        self.assertEqual(out[0]["status"], "valid_low_confidence")
        self.assertEqual(out[0]["confidence_score"], 0.3)
        self.assertIn("reason", out[0])

    def test_multiple_official_sources_corroborate(self):
        claim = "The Amazon Ads API is a REST API for managing advertising resources."
        out = validate_facts([
            fact("https://ads.amazon/a", claim),
            fact("https://docs.aws.example/b", claim),
        ])
        self.assertEqual([f["confidence_score"] for f in out], [0.75, 0.75])
        self.assertEqual([f["status"] for f in out], ["valid", "valid"])

    def test_corroboration_bonus_capped_at_030(self):
        claim = "Sponsored Brands campaigns support video creative assets."
        out = validate_facts([fact(f"https://ads.amazon/{c}", claim) for c in "abcde"])
        # 0.60 base + capped 0.30 corroboration, not 0.60 + 0.60.
        for f in out:
            self.assertEqual(f["confidence_score"], 0.9)
            self.assertEqual(f["status"], "valid")

    def test_stability_bonus_requires_unchanged_and_prior_run(self):
        url, claim = "https://ads.amazon/a", "Application approval may take up to 1 business day."
        bonus = validate_facts([fact(url, claim, **stable())])[0]
        self.assertEqual(bonus["confidence_score"], 0.7)
        changed = validate_facts([fact(url, claim, is_changed="Y", last_run=LAST_RUN)])[0]
        self.assertEqual(changed["confidence_score"], 0.6)
        no_prior = validate_facts([fact(url, claim, is_changed="N", last_run=None)])[0]
        self.assertEqual(no_prior["confidence_score"], 0.6)

    def test_score_never_exceeds_one(self):
        claim = "Sponsored Display uses cost-per-click billing."
        facts = [fact(f"https://ads.amazon/{c}", claim, **stable()) for c in "abcd"]
        out = validate_facts(facts)  # 0.60 + 0.30 + 0.10 = exactly 1.0
        for f in out:
            self.assertEqual(f["confidence_score"], 1.0)

    def test_score_clamped_when_constants_would_overflow(self):
        # Patch a constant so raw points exceed the scale; the clamp must bite.
        with patch("pipeline.validator.BASE_OFFICIAL", 95):
            out = validate_facts([
                fact("https://ads.amazon/a", "Claims API reports are available daily.",
                     **stable())])
        self.assertEqual(out[0]["confidence_score"], 1.0)


class ValidityTests(unittest.TestCase):
    """Required scenarios 7-10: support, official override, quality, conflicts."""

    def test_unsupported_fact_rejected(self):
        out = validate_facts([fact("https://blog.example/x",
                                   "Sponsored Brands supports audio ads.",
                                   source_type="community", community_agree_count=1)])
        self.assertEqual(out[0]["status"], "rejected")
        self.assertIn("reason", out[0])
        self.assertIn("official", out[0]["reason"])

    def test_official_version_wins_over_community_majority(self):
        official = fact("https://ads.amazon/a",
                        "Sponsored Brands campaigns require an active storefront.")
        community_text = "Sponsored Brands campaigns do not require an active storefront."
        out = validate_facts([
            official,
            fact("https://forum.example/t1", community_text, source_type="community"),
            fact("https://forum.example/t2", community_text, source_type="community"),
        ])
        self.assertEqual(out[0]["status"], "valid")       # official unaffected
        self.assertEqual(out[0]["confidence_score"], 0.6)  # contradictors don't corroborate
        for f in out[1:]:
            self.assertEqual(f["status"], "rejected")
            self.assertIn("official", f["reason"])

    def test_low_quality_community_only_never_valid(self):
        claim = "Sponsored Display audience bidding increases conversion rate."
        facts = [fact(f"https://forum.example/t{i}", claim,
                      source_type="community", community_agree_count=1,
                      **stable()) for i in range(4)]
        out = validate_facts(facts)  # 0.15 + 0.30 corroboration + 0.10 stability = 0.55
        for f in out:
            self.assertEqual(f["status"], "valid_low_confidence")
            self.assertEqual(f["confidence_score"], 0.55)
            self.assertIn("low-quality community", f["reason"])

    def test_quality_community_sources_can_reach_valid(self):
        claim = "Amazon Marketing Stream delivers near real-time metrics."
        facts = [fact(f"https://forum.example/t{i}", claim,
                      source_type="community", community_agree_count=3)
                 for i in range(5)]
        out = validate_facts(facts)  # 0.30 base + 0.30 capped corroboration
        for f in out:
            self.assertEqual(f["status"], "valid")
            self.assertEqual(f["confidence_score"], 0.6)

    def test_official_sources_contradicting_each_other_detected(self):
        out = validate_facts([
            fact("https://ads.amazon/a", "Application approval may take up to 1 business day."),
            fact("https://ads.amazon/b", "Application approval may take up to 5 business days."),
        ])
        for f in out:
            self.assertEqual(f["status"], "valid_low_confidence")
            self.assertEqual(f["confidence_score"], 0.6)  # no corroboration between rivals
            self.assertIn("contradict", f["reason"])

    def test_wording_level_contradiction_detected(self):
        out = validate_facts([
            fact("https://ads.amazon/a", "Amazon Ads API rate limits are enforced per account."),
            fact("https://ads.amazon/b", "Amazon Ads API rate limits are enforced per client application."),
        ])
        for f in out:
            self.assertEqual(f["status"], "valid_low_confidence")
            self.assertIn("contradict", f["reason"])


class OutputContractTests(unittest.TestCase):
    """Required scenarios 11-15: completeness, preservation, determinism, groups."""

    def test_rejected_facts_stay_in_output(self):
        facts = [
            fact("https://ads.amazon/a", "Sponsored Products ads appear in shopping results."),
            fact("https://blog.example/x", "Sponsored Brands supports audio ads.",
                 source_type="community", community_agree_count=0),
        ]
        out = validate_facts(facts)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]["status"], "rejected")
        self.assertTrue(out[1]["reason"])

    def test_original_fields_preserved_and_input_not_mutated(self):
        facts = [
            fact("https://ads.amazon/a", "The Amazon Ads API is a REST API.",
                 date="2026-09-01T00:00:00+00:00", confidence_pct=12,
                 topic_id="amazon-ads-api-overview"),
            fact("https://forum.example/t", "Stream needs AWS.", source_type="community",
                 community_agree_count=4),
        ]
        snapshot = copy.deepcopy(facts)
        out = validate_facts(facts)
        self.assertEqual(facts, snapshot)  # inputs untouched
        for original, result in zip(facts, out):
            for key, value in original.items():
                self.assertEqual(result[key], value)
            self.assertIn("confidence_score", result)
            self.assertIn("status", result)

    def test_deterministic_same_input_same_output(self):
        facts = [
            fact("https://ads.amazon/a", "Application approval may take up to 1 business day.", **stable()),
            fact("https://ads.amazon/b", "Application approval may take up to 5 business days.", **stable()),
            fact("https://forum.example/t1", "Stream needs an AWS account.",
                 source_type="community", community_agree_count=3),
        ]
        first = validate_facts(copy.deepcopy(facts))
        second = validate_facts(copy.deepcopy(facts))
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))
        # Order follows input order.
        self.assertEqual([f["url"] for f in first], [f["url"] for f in facts])

    def test_empty_input(self):
        self.assertEqual(validate_facts([]), [])

    def test_topic_groups_are_independent(self):
        claim = "Sponsored Display ads can appear on and off Amazon."
        out = validate_facts([
            fact("https://ads.amazon/a", claim, topic_id="sponsored-display-overview"),
            fact("https://docs.example/b", claim, topic_id="sponsored-display-overview"),
            fact("https://ads.amazon/c", claim, topic_id="unrelated-topic"),
        ])
        # Same wording, different topics: no cross-topic corroboration.
        self.assertEqual(out[0]["confidence_score"], 0.75)
        self.assertEqual(out[1]["confidence_score"], 0.75)
        self.assertEqual(out[2]["confidence_score"], 0.6)

    def test_same_url_facts_are_not_independent_sources(self):
        claim = "Sponsored Brands campaigns support video creative assets."
        out = validate_facts([
            fact("https://ads.amazon/a", claim),
            fact("https://ads.amazon/a", claim),
        ])
        for f in out:
            self.assertEqual(f["confidence_score"], 0.6)

    def test_extractor_confidence_pct_is_ignored(self):
        out = validate_facts([
            fact("https://ads.amazon/a", "Claims API reports are available daily.",
                 confidence_pct=1),   # near-zero extractor confidence
            fact("https://blog.example/x", "Claims API reports are available hourly.",
                 source_type="community", community_agree_count=1, confidence_pct=99),
        ])
        self.assertEqual(out[0]["confidence_score"], 0.6)  # recomputed, not 0.01
        self.assertEqual(out[1]["status"], "rejected")    # 99% didn't save it


class GroupingTests(unittest.TestCase):
    """Near-duplicate grouping when topic_id is absent."""

    def test_near_duplicates_group_without_topic_id(self):
        claim = "Sponsored Display ads can appear on and off Amazon."
        out = validate_facts([
            fact("https://ads.amazon/a", claim),
            fact("https://docs.example/b", claim),
            fact("https://ads.amazon/c", "Sponsored Products ads appear in shopping results."),
        ])
        self.assertEqual(out[0]["confidence_score"], 0.75)  # a+b corroborate
        self.assertEqual(out[1]["confidence_score"], 0.75)
        self.assertEqual(out[2]["confidence_score"], 0.6)   # unrelated, alone

    def test_topicless_near_duplicate_joins_topic_bucket(self):
        claim = "Sponsored Brands campaigns require an active storefront."
        out = validate_facts([
            fact("https://ads.amazon/a", claim, topic_id="sponsored-brands-overview"),
            fact("https://forum.example/t", claim, source_type="community"),
        ])
        # Community fact joined the official's topic group: officially backed,
        # corroborated -> 0.15 + 0.15 = 0.30 instead of rejected-unsupported.
        self.assertEqual(out[0]["status"], "valid")
        self.assertEqual(out[1]["status"], "valid_low_confidence")
        self.assertEqual(out[1]["confidence_score"], 0.3)


class LoggingTests(unittest.TestCase):
    def test_every_decision_logged_with_rule(self):
        facts = [
            fact("https://ads.amazon/a", "Sponsored Products ads appear in shopping results."),
            fact("https://blog.example/x", "Sponsored Brands supports audio ads.",
                 source_type="community", community_agree_count=0),
        ]
        with self.assertLogs("pipeline.validator", level="INFO") as captured:
            validate_facts(facts)
        text = "\n".join(captured.output)
        self.assertIn("status=valid rule=score-band", text)
        self.assertIn("status=rejected rule=support-required", text)


class SpecScenarioTests(unittest.TestCase):
    """One test per scenario in the Validator acceptance checklist."""

    def test_basic_official_pass_stable(self):
        # 1 official source, no contradiction, stable across 2 runs.
        out = validate_facts([fact("https://ads.amazon/a",
                                   "The Amazon Ads API is a REST API.", **stable())])
        self.assertEqual(out[0]["confidence_score"], 0.7)  # 0.6 + 0.1
        self.assertEqual(out[0]["status"], "valid")

    def test_basic_community_pass_two_agreeing_quality_sources(self):
        # Community sources with 3+ people agreeing; each fact sees ONE
        # additional corroborating source: 0.3 + 0.15 = 0.45.
        claim = "Amazon Marketing Stream delivers near real-time metrics."
        out = validate_facts([
            fact("https://forum.example/t1", claim, source_type="community",
                 community_agree_count=3),
            fact("https://forum.example/t2", claim, source_type="community",
                 community_agree_count=3),
        ])
        for f in out:
            self.assertEqual(f["confidence_score"], 0.45)
            self.assertEqual(f["status"], "valid_low_confidence")

    def test_three_agreeing_quality_community_sources_cross_into_valid(self):
        # Same setup with three sources: each sees TWO corroborators, so the
        # (capped) bonus is +0.30, not +0.15 -> 0.3 + 0.3 = 0.60 -> valid.
        claim = "Amazon Marketing Stream delivers near real-time metrics."
        out = validate_facts([
            fact(f"https://forum.example/t{i}", claim, source_type="community",
                 community_agree_count=3) for i in range(3)
        ])
        for f in out:
            self.assertEqual(f["confidence_score"], 0.6)
            self.assertEqual(f["status"], "valid")

    def test_reject_threshold_is_inclusive_at_030(self):
        # Exactly 0.30 -> valid_low_confidence, not rejected.
        single = validate_facts([fact("https://forum.example/t",
                                      "Stream needs an AWS account.",
                                      source_type="community",
                                      community_agree_count=3)])
        self.assertEqual(single[0]["confidence_score"], 0.3)
        self.assertEqual(single[0]["status"], "valid_low_confidence")
        # Two weak agreeing sources also land exactly on 0.30: 0.15 + 0.15.
        pair = validate_facts([
            fact("https://blog.example/a", "Stream needs an AWS account.",
                 source_type="community", community_agree_count=1),
            fact("https://forums.example/b", "Stream needs an AWS account.",
                 source_type="community", community_agree_count=1),
        ])
        for f in pair:
            self.assertEqual(f["confidence_score"], 0.3)
            self.assertEqual(f["status"], "valid_low_confidence")
        # Just below the line (0.25 = 0.15 + 0.10 stability, but unsupported)
        # -> rejected.
        below = validate_facts([fact("https://blog.example/c",
                                     "Stream needs an AWS account.",
                                     source_type="community",
                                     community_agree_count=2, **stable())])
        self.assertEqual(below[0]["confidence_score"], 0.25)
        self.assertEqual(below[0]["status"], "rejected")

    def test_true_rejection_weak_mentions_without_agreement(self):
        # 1-2 weak community mentions that do NOT corroborate each other.
        out = validate_facts([
            fact("https://blog.example/a", "Sponsored Brands supports audio ads.",
                 source_type="community", community_agree_count=1),
            fact("https://forums.example/b", "Sponsored Display supports radio ads.",
                 source_type="community", community_agree_count=1),
        ])
        for f in out:
            self.assertEqual(f["confidence_score"], 0.15)
            self.assertEqual(f["status"], "rejected")
            self.assertIn("reason", f)

    def test_two_weak_agreeing_sources_forced_low_confidence(self):
        claim = "Sponsored Display audience bidding increases conversion rate."
        out = validate_facts([
            fact("https://blog.example/a", claim, source_type="community",
                 community_agree_count=1),
            fact("https://forums.example/b", claim, source_type="community",
                 community_agree_count=1),
        ])
        for f in out:
            self.assertEqual(f["status"], "valid_low_confidence")
            self.assertIn("low-quality community", f["reason"])

    def test_quality_cap_overrides_raw_score(self):
        # Force the raw score above 0.6 by patching the low-quality base:
        # the cap must still hold -- raw score can never buy valid status.
        claim = "Sponsored Display audience bidding increases conversion rate."
        with patch("pipeline.validator.BASE_COMMUNITY_LOW", 60):
            out = validate_facts([
                fact("https://blog.example/a", claim, source_type="community",
                     community_agree_count=1),
                fact("https://forums.example/b", claim, source_type="community",
                     community_agree_count=1),
            ])
        for f in out:
            self.assertEqual(f["confidence_score"], 0.75)  # raw 0.6 + 0.15
            self.assertEqual(f["status"], "valid_low_confidence")  # capped anyway

    def test_authority_overrides_majority_of_three(self):
        # 1 official says A; 3 community sources (3 people each) say B. The
        # community version's raw score reaches 0.60 and it is the majority --
        # it is still rejected, never averaged or voted against the official.
        official_claim = "Sponsored Brands campaigns require an active storefront."
        community_claim = "Sponsored Brands campaigns do not require an active storefront."
        out = validate_facts([
            fact("https://ads.amazon/a", official_claim),
            *[fact(f"https://forum.example/t{i}", community_claim,
                   source_type="community", community_agree_count=3)
              for i in range(3)],
        ])
        self.assertEqual(out[0]["status"], "valid")
        self.assertEqual(out[0]["confidence_score"], 0.6)  # rivals don't corroborate
        for f in out[1:]:
            self.assertEqual(f["confidence_score"], 0.6)   # majority, yet...
            self.assertEqual(f["status"], "rejected")
            self.assertIn("official", f["reason"])

    def test_cross_page_contradiction_with_topic_and_high_score(self):
        # Two officials in the same topic disagree; a third corroborates one
        # side, pushing its raw score to 0.85 -- neither may reach valid.
        claim_a = "Application approval may take up to 1 business day."
        claim_b = "Application approval may take up to 5 business days."
        out = validate_facts([
            fact("https://ads.amazon/a", claim_a, topic_id="onboarding", **stable()),
            fact("https://ads.amazon/c", claim_a, topic_id="onboarding", **stable()),
            fact("https://ads.amazon/b", claim_b, topic_id="onboarding", **stable()),
        ])
        self.assertEqual(out[0]["confidence_score"], 0.85)  # corroborated + stable
        for f in out:
            self.assertEqual(f["status"], "valid_low_confidence")
            self.assertIn("contradict", f["reason"])

    def test_stability_bonus_tips_low_confidence_to_valid(self):
        # With the shipped constants no natural score sits at 0.50-0.59 for a
        # supported fact, so widen the stability step to make the tipping
        # observable: 0.30 + 0.15 corroboration + 0.15 stability = 0.60.
        claim = "Amazon Marketing Stream delivers near real-time metrics."
        facts = [
            fact("https://forum.example/t1", claim, source_type="community",
                 community_agree_count=3, **stable()),
            fact("https://forum.example/t2", claim, source_type="community",
                 community_agree_count=3, **stable()),
        ]
        with patch("pipeline.validator.STABILITY_BONUS", 15):
            tipped = validate_facts(copy.deepcopy(facts))
            untipped = validate_facts([
                {**f, "is_changed": "Y", "last_run": None} for f in facts])
        self.assertEqual(tipped[0]["status"], "valid")       # 0.45 + 0.15 = 0.60
        self.assertEqual(tipped[0]["confidence_score"], 0.6)
        self.assertEqual(untipped[0]["status"], "valid_low_confidence")  # 0.45

    def test_corroboration_capped_with_four_extra_sources(self):
        # 1 official + 4 additional corroborating sources: bonus is +0.30
        # (capped), not +0.60.
        claim = "Sponsored Brands campaigns support video creative assets."
        out = validate_facts([fact("https://ads.amazon/a", claim)] +
                             [fact(f"https://mirror.example/{c}", claim)
                              for c in "bcde"])
        self.assertEqual(out[0]["confidence_score"], 0.9)  # 0.6 + 0.3, not 1.2
        self.assertEqual(out[0]["status"], "valid")
        for f in out[1:]:
            self.assertEqual(f["confidence_score"], 0.9)


class ContractTests(unittest.TestCase):
    def test_not_a_list_rejected(self):
        with self.assertRaises(ValidationError):
            validate_facts({"url": "https://ads.amazon/a"})

    def test_missing_required_field_rejected(self):
        bad = fact("https://ads.amazon/a", "Some claim.")
        del bad["source_type"]
        with self.assertRaises(ValidationError):
            validate_facts([bad])

    def test_bad_source_type_rejected(self):
        with self.assertRaises(ValidationError):
            validate_facts([fact("https://a.example/", "x", source_type="blog")])


class CliTests(unittest.TestCase):
    def setUp(self):
        # main() calls logging.basicConfig(); undo it so later tests'
        # log records don't spill into unittest's output.
        self._root_handlers = logging.getLogger().handlers[:]

    def tearDown(self):
        logging.getLogger().handlers[:] = self._root_handlers

    def test_cli_reads_file_and_prints_validated_array(self):
        facts = [
            fact("https://ads.amazon/a", "Sponsored Products ads appear in shopping results."),
            fact("https://forum.example/t", "Stream needs an AWS account.",
                 source_type="community", community_agree_count=3),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "facts.json"
            path.write_text(json.dumps(facts), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = main([str(path)])
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertEqual([f["status"] for f in result], ["valid", "valid_low_confidence"])

    def test_cli_reads_stdin(self):
        payload = json.dumps([fact("https://ads.amazon/a", "Some factual claim.")])
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO(payload)), redirect_stdout(out):
            code = main(["-"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())[0]["status"], "valid")

    def test_cli_invalid_json_fails_cleanly(self):
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            from contextlib import redirect_stderr
            with redirect_stderr(err):
                code = main([str(path)])
        self.assertEqual(code, 2)
        self.assertIn("invalid JSON", err.getvalue())

    def test_cli_facts_object_form_unwrapped(self):
        payload = json.dumps({"facts": [fact("https://ads.amazon/a", "Some factual claim.")]})
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO(payload)), redirect_stdout(out):
            code = main(["-"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out.getvalue())), 1)


if __name__ == "__main__":
    unittest.main()
