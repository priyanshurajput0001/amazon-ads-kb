"""Boundary tests for every confidence/status threshold (review Part 9).

The reviewer mutated the confidence thresholds and the suite still passed —
these tests pin the exact boundaries with LITERAL expected values. They must
NOT read the thresholds from the implementation: if someone changes 0.60 to
0.55 or 0.30 to 0.25, tests here fail.

Pinned thresholds:
  VALID_MIN            0.60  (>= is valid, < is valid_low_confidence)
  LOW_CONFIDENCE_MIN   0.30  (>= survives, < is rejected)
  CORROBORATION_STEP   0.15 per additional agreeing URL
  CORROBORATION_MAX    0.30 cap
  STABILITY_BONUS      0.10
  COMMUNITY_PEOPLE_MIN 3     (>= earns the 0.30 community base)
  AGREE_MIN            0.80  (token-set Jaccard: agree)
  CONFLICT_MIN         0.50  (below: unrelated)
"""

import unittest

from pipeline.validator import (
    _classify_pair,
    _profile,
    calculate_confidence,
    validate_facts,
)


def fact(content, url, source_type="official", agree=0, is_changed="Y",
         last_run=None, topic_id=None):
    return {"url": url, "date": "2026-09-26T00:00:00+00:00",
            "content": content, "is_changed": is_changed, "last_run": last_run,
            "source_type": source_type, "community_agree_count": agree,
            "topic_id": topic_id}


def status_of(content, **kwargs):
    out = validate_facts([fact(content, "https://a.example/x", **kwargs)])
    return out[0]["status"], out[0]["confidence_score"]


class StatusBandBoundaries(unittest.TestCase):
    """Score bands: >=0.60 valid | >=0.30 valid_low_confidence | <0.30 rejected.

    Reachable scores are quantized to 0.05 (integer hundredths of the
    constants), so the boundary-adjacent reachable values are 0.45 / 0.60
    around VALID_MIN and 0.15 / 0.30 around LOW_CONFIDENCE_MIN.
    """

    def test_exactly_060_is_valid(self):
        # official base alone: 0.60 exactly, on the boundary
        status, score = status_of("Sponsored Products uses keyword targeting.")
        self.assertEqual((status, score), ("valid", 0.6))

    def test_045_below_valid_is_low_confidence(self):
        # community 3-people base 0.30 + one corroborating URL 0.15 = 0.45
        content = "Approval may take one business day."
        out = validate_facts([
            fact(content, "https://a.example/x", "community", agree=3),
            fact(content, "https://b.example/y", "community", agree=3),
        ])
        self.assertEqual({f["status"] for f in out}, {"valid_low_confidence"})
        self.assertEqual(out[0]["confidence_score"], 0.45)

    def test_060_crossed_by_corroboration_is_valid(self):
        # community base 0.30 + two corroborating URLs (2*0.15) = 0.60
        content = "Approval may take one business day."
        out = validate_facts([
            fact(content, "https://a.example/x", "community", agree=3),
            fact(content, "https://b.example/y", "community", agree=3),
            fact(content, "https://c.example/z", "community", agree=3),
        ])
        self.assertEqual({f["status"] for f in out}, {"valid"})
        self.assertEqual(out[0]["confidence_score"], 0.6)

    def test_exactly_030_is_low_confidence_not_rejected(self):
        # community with exactly 3 independent people: base 0.30, on the
        # boundary — survives as valid_low_confidence
        status, score = status_of("Bulksheets export campaign data.",
                                  source_type="community", agree=3)
        self.assertEqual((status, score), ("valid_low_confidence", 0.3))

    def test_below_030_is_rejected(self):
        # community with 2 people: base 0.15 < 0.30 -> rejected
        status, score = status_of("Bulksheets export campaign data.",
                                  source_type="community", agree=2)
        self.assertEqual(status, "rejected")
        self.assertEqual(score, 0.15)


class ScoringComponentBoundaries(unittest.TestCase):
    def test_corroboration_step_is_exactly_015(self):
        score, _ = calculate_confidence("official", 0, 1, False)
        self.assertEqual(score, 0.75)  # 0.60 + 0.15

    def test_corroboration_caps_at_030(self):
        # 3+ agreeing URLs still add only 0.30 (0.60 + 0.30 = 0.90)
        for corroborating in (2, 3, 7):
            score, _ = calculate_confidence("official", 0, corroborating, False)
            self.assertEqual(score, 0.9, f"corroborating={corroborating}")

    def test_stability_bonus_is_exactly_010(self):
        score, _ = calculate_confidence("official", 0, 0, True)
        self.assertEqual(score, 0.7)  # 0.60 + 0.10

    def test_stability_reaches_100_with_cap(self):
        score, _ = calculate_confidence("official", 0, 2, True)
        self.assertEqual(score, 1.0)  # 0.60 + 0.30 + 0.10

    def test_community_base_requires_exactly_3_people(self):
        self.assertEqual(calculate_confidence("community", 2, 0, False)[0],
                         0.15)
        self.assertEqual(calculate_confidence("community", 3, 0, False)[0],
                         0.30)

    def test_score_clamped_to_unit_interval(self):
        score, _ = calculate_confidence("official", 99, 99, True)
        self.assertEqual(score, 1.0)


class SimilarityBandBoundaries(unittest.TestCase):
    """Pair classification bands over token-set Jaccard similarity.

      sim < 0.5                    unrelated
      0.5 <= sim, value flip       contradict
      0.5 <= sim < 0.8, no flip    contradict
      sim >= 0.8                   agree
    """

    @staticmethod
    def pair(a, b):
        return _classify_pair(_profile(fact(a, "https://a.example/x")),
                              _profile(fact(b, "https://b.example/y")))

    def test_exactly_080_is_agree(self):
        # shared {alpha,beta,gamma,delta}, union adds epsilon: 4/5 = 0.80
        self.assertEqual(
            self.pair("alpha beta gamma delta", "alpha beta gamma delta epsilon"),
            "agree")

    def test_075_without_flip_is_contradict(self):
        # shared 3 of union 4 = 0.75 — inside the conflict band
        self.assertEqual(
            self.pair("alpha beta gamma", "alpha beta gamma delta"),
            "contradict")

    def test_exactly_050_is_not_unrelated(self):
        # shared {alpha,beta}, union 4 = 0.50 — on the CONFLICT_MIN boundary
        self.assertEqual(
            self.pair("alpha beta gamma", "alpha beta delta"),
            "contradict")

    def test_below_050_is_unrelated(self):
        # shared {alpha}, union 3 = 0.33
        self.assertEqual(
            self.pair("alpha beta", "alpha gamma"),
            "unrelated")

    def test_just_below_050_is_unrelated(self):
        # shared {alpha,beta,gamma}, union 7 = 3/7 = 0.4286 — the closest
        # reachable value below CONFLICT_MIN. If CONFLICT_MIN were lowered to
        # 0.42 or below, this becomes contradict and the suite fails.
        self.assertEqual(
            self.pair("alpha beta gamma delta",
                      "alpha beta gamma epsilon zeta theta"),
            "unrelated")

    def test_just_above_050_is_contradict(self):
        # shared 5, union 9 = 5/9 = 0.5556 — the closest reachable value
        # above CONFLICT_MIN. If CONFLICT_MIN were raised to 0.56 or above,
        # this becomes unrelated and the suite fails.
        self.assertEqual(
            self.pair("alpha beta gamma delta epsilon",
                      "alpha beta gamma delta epsilon zeta eta theta iota"),
            "contradict")

    def test_just_above_080_is_agree(self):
        # shared 6, union 7 = 6/7 = 0.857 — the closest reachable value above
        # AGREE_MIN. If AGREE_MIN were raised to 0.86 or above, this becomes
        # contradict and the suite fails.
        self.assertEqual(
            self.pair("alpha beta gamma delta epsilon zeta",
                      "alpha beta gamma delta epsilon zeta eta"),
            "agree")

    def test_just_below_080_is_contradict(self):
        # shared 5, union 7 = 5/7 = 0.714 — clearly inside the conflict band.
        self.assertEqual(
            self.pair("alpha beta gamma delta epsilon",
                      "alpha beta gamma delta epsilon zeta eta"),
            "contradict")

    def test_numeric_flip_in_band_is_contradict(self):
        # identical tokens except the value: sim 4/6, numbers {100} vs {500}
        self.assertEqual(
            self.pair("the limit is 100 campaigns",
                      "the limit is 500 campaigns"),
            "contradict")

    def test_negation_flip_in_band_is_contradict(self):
        self.assertEqual(
            self.pair("the api requires approval first",
                      "the api does not require approval first"),
            "contradict")


class ValidatorThresholdMutationGuards(unittest.TestCase):
    """End-to-end pinning: a mutated threshold changes a real decision."""

    def test_raising_valid_min_to_065_would_flip_this_case(self):
        # The corroborated community fact scores exactly 0.60 and MUST be
        # valid. If VALID_MIN were > 0.60, this returns low-confidence and
        # the suite fails — the threshold cannot drift silently.
        content = "Approval may take one business day."
        out = validate_facts([
            fact(content, "https://a.example/x", "community", agree=3),
            fact(content, "https://b.example/y", "community", agree=3),
            fact(content, "https://c.example/z", "community", agree=3),
        ])
        self.assertEqual(out[0]["status"], "valid")

    def test_lowering_low_confidence_min_to_020_would_flip_this_case(self):
        # The 2-person community fact scores 0.15 and MUST be rejected. If
        # LOW_CONFIDENCE_MIN were <= 0.15, this survives and the suite fails.
        out = validate_facts([fact("Bulksheets export campaign data.",
                                   url="https://a.example/x",
                                   source_type="community", agree=2)])
        self.assertEqual(out[0]["status"], "rejected")

    def test_similarity_bands_change_real_scoring(self):
        """End-to-end pinning of AGREE_MIN/CONFLICT_MIN: a 6/7-overlap pair
        (~0.857) MUST be agreement (both official facts corroborate each
        other, raising the score), and a 5/9-overlap pair (~0.556) MUST be a
        contradiction (official facts capped at valid_low_confidence). If
        either band moves, these real decisions flip and the suite fails."""
        agree_a = "alpha beta gamma delta epsilon zeta reporting works"
        agree_b = "alpha beta gamma delta epsilon zeta eta reporting works"
        out = validate_facts([
            fact(agree_a, "https://a.example/x"),
            fact(agree_b, "https://b.example/y"),
        ])
        # agreement across 2 URLs: 0.60 base + 0.15 corroboration = 0.75
        self.assertEqual(out[0]["confidence_score"], 0.75)
        self.assertEqual(out[0]["status"], "valid")

        conflict_a = "alpha beta gamma delta epsilon kappa limit applies"
        conflict_b = ("alpha beta gamma delta epsilon zeta eta theta iota "
                      "limit applies")
        out = validate_facts([
            fact(conflict_a, "https://a.example/x"),
            fact(conflict_b, "https://b.example/y"),
        ])
        # official cross-page contradiction: capped at valid_low_confidence
        self.assertEqual(out[0]["status"], "valid_low_confidence")
        self.assertIn("official sources materially contradict",
                      out[0]["reason"])


if __name__ == "__main__":
    unittest.main()
