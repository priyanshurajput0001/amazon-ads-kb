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
  RELATED_MIN         0.10 (publisher.py Related-link token overlap)
  RELATED_MAX         5    (publisher.py Related links per document)
  SLUG_MAX            64   (concepts.py slug length cap)
  ID_WORDS_MAX        8    (concepts.py words taken when coining an id)
  DEFAULT_CAP         10   (discover.py candidate cap)
  MIN_TEXT_CHARS      40   (htmlconvert.py minimum convertible text)
"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from pipeline import concepts, discover, htmlconvert, publisher, topics
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

    def test_related_min_is_exactly_010(self):
        self.assertEqual(publisher.RELATED_MIN, 0.10)

    def test_related_max_is_exactly_5(self):
        self.assertEqual(publisher.RELATED_MAX, 5)

    def test_slug_max_is_exactly_64(self):
        self.assertEqual(concepts.SLUG_MAX, 64)

    def test_id_words_max_is_exactly_8(self):
        self.assertEqual(concepts.ID_WORDS_MAX, 8)

    def test_default_cap_is_exactly_10(self):
        self.assertEqual(discover.DEFAULT_CAP, 10)

    def test_min_text_chars_is_exactly_40(self):
        self.assertEqual(htmlconvert.MIN_TEXT_CHARS, 40)


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


class RelatedMinBehaviour(unittest.TestCase):
    """Non-generic token overlap of exactly 2/20 = 0.10 links two concepts
    sharing a source; 2/22 = 0.0909 does not. A one-step move of
    RELATED_MIN in either direction flips one of the two."""

    SHARED_URL = "https://s.example/shared"

    def _concept(self, tokens):
        return {"facts": [{"content": " ".join(tokens),
                           "sources": [{"url": self.SHARED_URL}]}]}

    def test_overlap_exactly_010_links(self):
        mine = self._concept([f"tok{i:02d}" for i in range(1, 11)])   # 10
        other = self._concept(["tok01", "tok02"]                      # 2 shared
                              + [f"tok{i:02d}" for i in range(11, 21)])  # +10
        # intersection 2, union 20 -> exactly 0.10: on the boundary it links
        self.assertEqual(publisher.related_concepts(
            "mine", mine, {"mine": mine, "other": other}), ["other"])

    def test_overlap_0909_does_not_link(self):
        mine = self._concept([f"tok{i:02d}" for i in range(1, 11)])
        other = self._concept(["tok01", "tok02"]
                              + [f"tok{i:02d}" for i in range(11, 23)])  # +12
        # intersection 2, union 22 -> 0.0909: just below, no link
        self.assertEqual(publisher.related_concepts(
            "mine", mine, {"mine": mine, "other": other}), [])


class RelatedMaxBehaviour(unittest.TestCase):
    def test_exactly_5_related_links_for_6_qualifying_concepts(self):
        """Six other concepts each share the source and overlap highly; the
        tie is broken by id ascending and the list is cut at exactly 5.
        A max of 4 returns 4 and a max of 6 returns 6 — both fail."""
        base = [f"tok{i:02d}" for i in range(1, 21)]
        target = {"facts": [{"content": " ".join(base),
                             "sources": [{"url": "https://s.example/x"}]}]}
        snapshot = {"target": target}
        for n in range(1, 7):
            snapshot[f"other-{n}"] = {"facts": [
                {"content": " ".join(base + [f"uq{n}0"]),
                 "sources": [{"url": "https://s.example/x"}]}]}
        self.assertEqual(
            publisher.related_concepts("target", target, snapshot),
            ["other-1", "other-2", "other-3", "other-4", "other-5"])


class SlugMaxBehaviour(unittest.TestCase):
    THIRTEEN = ["kane", "luna", "miro", "nova", "onyx", "peri",
                "quet", "rhos", "silo", "tarn", "ulmo", "vorl", "wren"]

    def test_slug_of_exactly_64_chars_is_kept_whole(self):
        # 13 four-letter words: 13*4 + 12 hyphens = 64 chars exactly — at
        # the cap nothing is truncated. SLUG_MAX 63 would cut it to 59.
        slug = concepts.readable_slug(" ".join(self.THIRTEEN))
        self.assertEqual(slug, "-".join(self.THIRTEEN))
        self.assertEqual(len(slug), 64)

    def test_slug_of_65_chars_is_truncated_to_a_word_boundary(self):
        # 12 four-letter words (59 chars) + a 5-letter word = 65 > 64: the
        # trailing word is dropped at the boundary, leaving the 12-word join.
        # SLUG_MAX 65 would keep all 65 characters.
        twelve = self.THIRTEEN[:12]
        joined = "-".join(twelve)
        self.assertEqual(len(joined), 59)
        slug = concepts.readable_slug(" ".join(twelve) + " braid")
        self.assertEqual(slug, joined)


class IdWordsMaxBehaviour(unittest.TestCase):
    def _fact(self, n_words):
        words = [f"word{i:02d}" for i in range(1, n_words + 1)]
        return {"content": " ".join(words), "confidence_score": 0.5,
                "topic_hint": None}

    def test_exactly_8_informative_words_are_all_kept(self):
        # 8 words -> an 8-word id. ID_WORDS_MAX 7 would keep only 7.
        self.assertEqual(concepts.coining_slug([self._fact(8)]),
                         "-".join(f"word{i:02d}" for i in range(1, 9)))

    def test_9_informative_words_are_cut_to_8(self):
        # 9 words -> the first 8. ID_WORDS_MAX 9 would keep all 9.
        self.assertEqual(concepts.coining_slug([self._fact(9)]),
                         "-".join(f"word{i:02d}" for i in range(1, 9)))


class DefaultCapBehaviour(unittest.TestCase):
    """11 new in-scope candidates from one cached seed page: exactly 10 are
    returned, in first-seen order. DEFAULT_CAP 9 returns 9 and 11 returns
    11 — both fail."""

    SEED = "https://advertising.amazon.com/API/docs/en-us"
    T0 = "2026-09-26T12:00:00+00:00"

    def test_eleven_in_scope_candidates_are_capped_at_exactly_10(self):
        def no_search(query, cap):
            raise AssertionError("search seam must not run for this test")

        links = "\n".join(
            f"[page {i}](https://advertising.amazon.com/cap-{i})"
            for i in range(1, 12))
        page = f"# Amazon Ads API docs\n\n{links}\n"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            state = base / "fetch_state.json"
            cache = base / "cache"
            cache.mkdir()
            state.write_text(json.dumps(
                {self.SEED: {"status": "ok", "fetched_at": self.T0,
                             "sha256": hashlib.sha256(
                                 page.encode()).hexdigest(),
                             "strategy": "tvly-basic"}}), encoding="utf-8")
            (cache / f"{hashlib.sha256(page.encode()).hexdigest()}.md"
             ).write_text(page, encoding="utf-8")
            report = discover.discover([self.SEED], state_path=state,
                                       cache_path=cache, search=no_search)
            self.assertEqual(len(report["candidates"]), 10)
            self.assertEqual(
                report["candidates"],
                [f"https://advertising.amazon.com/cap-{i}" for i in range(1, 11)])


class MinTextCharsBehaviour(unittest.TestCase):
    def test_exactly_40_visible_chars_converts(self):
        # 40 chars of visible text: at the boundary the page IS convertible.
        # MIN_TEXT_CHARS 41 would refuse it.
        self.assertTrue(htmlconvert.convertible(f"<p>{'x' * 40}</p>"))

    def test_39_visible_chars_stays_html(self):
        # 39 chars: below the boundary the page is an honest html shell.
        # MIN_TEXT_CHARS 39 would convert it.
        self.assertFalse(htmlconvert.convertible(f"<p>{'x' * 39}</p>"))


if __name__ == "__main__":
    unittest.main()
