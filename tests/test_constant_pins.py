"""Exact literal pins for constants whose one-step mutation previously
left the suite green (review 3).

Several existing tests read these constants back from the module (e.g.
`range(topics.MAX_TOPIC_FACTS)`, `assertEqual(len(calls), PAIR_LLM_CAP)`)
— that is self-consistent, not a pin: mutating the constant mutates the
expectation with it. Each constant is pinned twice here:

  1. the exact literal value (mirrors the literal style of
     test_corroboration_step_is_exactly_015), and
  2. a behaviour test whose expected values are literals, so the value
     cannot move in either direction without a red suite.

Pinned constants:
  MAX_TOPIC_FACTS     12   (topics.py catch-all cap)
  PAIR_LLM_CAP        60   (merger.py per-concept seam-call ceiling)
  MATCH_PREVIEW       5    (concepts.py claims shown on the first ask)
  TOPIC_MIN_HITS      1    (topics.py matched phrases to win outright)
  SUBTOPIC_MIN_HITS   1    (topics.py matched phrases to claim in a split)
  HINT_TOKEN_MIN      0.5  (concepts.py hint-overlap candidate boost)
"""

import unittest

from pipeline import concepts, topics
from pipeline.merger import _pair_labels


class ExactLiteralValues(unittest.TestCase):
    """Each constant equals its documented literal — reading the value back
    from the module is NOT a pin; asserting the literal is."""

    def test_max_topic_facts_is_exactly_12(self):
        self.assertEqual(topics.MAX_TOPIC_FACTS, 12)

    def test_pair_llm_cap_is_exactly_60(self):
        from pipeline import merger
        self.assertEqual(merger.PAIR_LLM_CAP, 60)

    def test_match_preview_is_exactly_5(self):
        self.assertEqual(concepts.MATCH_PREVIEW, 5)

    def test_topic_min_hits_is_exactly_1(self):
        self.assertEqual(topics.TOPIC_MIN_HITS, 1)

    def test_subtopic_min_hits_is_exactly_1(self):
        self.assertEqual(topics.SUBTOPIC_MIN_HITS, 1)

    def test_hint_token_min_is_exactly_050(self):
        self.assertEqual(concepts.HINT_TOKEN_MIN, 0.5)


class MaxTopicFactsBehaviour(unittest.TestCase):
    """12 facts is AT the cap (no split); 13 is over it (split by the
    declared sub-topics). A one-step move of the cap flips one of these."""

    JUNK = ["Selling partner fact number %d covers operations." % i
            for i in range(12)]
    MODELS = ("The amzn/selling-partner-api-models repository contains "
              "OpenAPI models for developers.")

    def _concept(self, total):
        return {"id": "selling-partner-api",
                "title": "Selling Partner API", "type": "concept",
                "facts": [{"content": c} for c in
                          self.JUNK[:total - 1] + [self.MODELS]],
                "conflicts": []}

    def test_12_facts_is_at_the_cap_no_split(self):
        # The models fact must stay in the parent. If the cap were lowered
        # to 11, the concept splits and the models fact moves out.
        concept = self._concept(12)
        self.assertEqual(len(concept["facts"]), 12)
        self.assertEqual(topics.split_over_cap(concept), [concept])

    def test_13_facts_is_over_the_cap_splits(self):
        # If the cap were raised to 13, this concept no longer splits.
        pieces = topics.split_over_cap(self._concept(13))
        self.assertEqual(sorted(p["id"] for p in pieces),
                         ["selling-partner-api",
                          "selling-partner-api-docs-and-models"])


class PairLlmCapBehaviour(unittest.TestCase):
    def test_exactly_60_seam_calls_for_66_eligible_pairs(self):
        """12 members sharing exactly {kappa, lambda}: every one of the
        C(12,2) = 66 pairs is deterministic-undecided but LLM-eligible
        (2 shared informative tokens), so the seam is called exactly
        min(66, cap) = 60 times. A cap of 55 or 65 gives 55 or 65."""
        calls = []

        def seam(a, b):
            calls.append((a["content"], b["content"]))
            return "complementary"

        members = [{"content": f"kappa lambda u{i}a u{i}b u{i}c",
                    "sources": [{"url": f"https://s{i}.example/x"}]}
                   for i in range(12)]
        labels = _pair_labels(members, [True] * 12, seam)
        self.assertEqual(len(members) * (len(members) - 1) // 2, 66)
        self.assertEqual(len(calls), 60)
        self.assertEqual(len(labels), 60)


class MatchPreviewBehaviour(unittest.TestCase):
    def test_first_ask_shows_exactly_the_first_5_claims(self):
        """The preview ask shows 5 of the concept's 7 claims; the retry
        after a 'no' shows all 7. A preview of 4 or 6 changes the first
        recorded size and fails."""
        first = "alpha beta gamma delta epsilon zeta"
        junk = [f"mu nu omicron pi rho {i}" for i in range(6)]
        existing = {"preview-topic": {
            "id": "preview-topic", "title": "Preview Concept",
            "facts": [{"content": c} for c in [first] + junk],
        }}
        sizes = []

        def spy(new_claim, title, existing_claims):
            sizes.append(len(existing_claims))
            return False

        # shared 4, union 10 = 0.40 overlap with `first`: the LLM band.
        concepts.assign_concepts(
            [{"content": "alpha beta gamma delta theta iota kappa lambda",
              "topic_hint": None}],
            existing, match_llm=spy)
        self.assertEqual(sizes, [5, 7])


class TopicMinHitsBehaviour(unittest.TestCase):
    def test_one_phrase_win_routes_by_keywords_without_llm(self):
        """'Seller Central' is the ONLY matched phrase anywhere in this
        claim (exactly 1 hit, every other topic 0): one hit must win
        outright with no seam call. If TOPIC_MIN_HITS were raised to 2 the
        claim falls into the ambiguous band and the seam is consulted."""
        calls = []

        def seam(claim, candidates):
            calls.append(claim)
            return "bulksheets-and-bulk-operations"

        slug, how = topics.route_fact(
            "Sellers manage campaigns inside Seller Central.",
            choose_llm=seam)
        self.assertEqual(
            (slug, how), ("bulksheets-and-bulk-operations", "keywords"))
        self.assertEqual(calls, [])


class SubtopicMinHitsBehaviour(unittest.TestCase):
    def test_one_subtopic_phrase_claims_the_fact_in_a_split(self):
        """An over-cap api-release-notes concept: 'The feed publishes new
        entries.' matches exactly ONE subscription-feeds phrase ({feed})
        and must be claimed by that sub-topic. With SUBTOPIC_MIN_HITS at 2
        it stays in the parent; at 0 the zero-hit fillers are claimed too
        and the parent piece disappears — both fail this test."""
        junk = ["Filler bulletin item number %d concerns metrics." % i
                for i in range(12)]
        version = "The v2 endpoint was deprecated in this release."
        feed = "The feed publishes new entries."
        concept = {"id": "api-release-notes",
                   "title": "API Release Notes", "type": "concept",
                   "facts": [{"content": c}
                             for c in junk + [version, feed]],
                   "conflicts": []}
        self.assertEqual(len(concept["facts"]), 14)  # over the cap of 12
        pieces = topics.split_over_cap(concept)
        self.assertEqual(sorted(p["id"] for p in pieces),
                         ["api-release-notes",
                          "api-release-notes-subscription-feeds",
                          "api-release-notes-version-updates"])
        by_id = {p["id"]: p for p in pieces}
        self.assertEqual([f["content"] for f in
                          by_id["api-release-notes-subscription-feeds"]["facts"]],
                         [feed])
        self.assertEqual(len(by_id["api-release-notes"]["facts"]), 12)


class HintTokenMinBehaviour(unittest.TestCase):
    def _existing(self, cid):
        return {cid: {"id": cid, "title": "Disjoint Subject",
                      "facts": [{"content": "mu nu omicron pi rho sigma"}]}}

    def test_hint_overlap_exactly_050_creates_candidate(self):
        # hint {alpha..delta} vs the 8-token id: 4/8 = 0.50 — on the
        # boundary the boost must fire. If HINT_TOKEN_MIN were raised to
        # 0.55 there is no candidate and the seam is never asked.
        cid = "alpha-beta-gamma-delta-epsilon-zeta-eta-theta"
        calls = []

        def spy(new_claim, title, existing_claims):
            calls.append(new_claim)
            return True

        facts, _ = concepts.assign_concepts(
            [{"content": "tau upsilon phi chi psi omega",
              "topic_hint": "alpha beta gamma delta"}],
            self._existing(cid), match_llm=spy)
        self.assertEqual(len(calls), 1)
        self.assertEqual(facts[0]["concept_id"], cid)

    def test_hint_overlap_0454_does_not_create_candidate(self):
        # hint 5 of an 11-token id: 5/11 = 0.4545 — below the boundary, no
        # boost, no seam call. If HINT_TOKEN_MIN were lowered to 0.45 the
        # boost fires, the seam is asked, and never_llm fails the test.
        cid = "alpha-beta-gamma-delta-epsilon-zeta-eta-theta-iota-kappa-lambda"

        def never_llm(new_claim, title, existing_claims):
            raise AssertionError("LLM seam must not be called for this case")

        facts, new_concepts = concepts.assign_concepts(
            [{"content": "tau upsilon phi chi psi omega",
              "topic_hint": "alpha beta gamma delta epsilon"}],
            self._existing(cid), match_llm=never_llm)
        self.assertNotEqual(facts[0]["concept_id"], cid)
        self.assertIn(facts[0]["concept_id"], new_concepts)


if __name__ == "__main__":
    unittest.main()
