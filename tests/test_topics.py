"""Tests for the topic taxonomy (pipeline/topics.py) and topic-level
concept identity (review step 3).

Pinned behaviors:
  - deterministic keyword routing with a strict winner (no LLM call)
  - ambiguous claims make exactly ONE seam call, choosing from the candidate
    list; seam failure falls back to the general topic, never to wording
  - slug stability: a reworded claim routes to the SAME topic slug
  - the catch-all cap splits an over-grown topic by declared sub-topics
    (taxonomy slugs only — never sentence-derived)
  - topic identity: routed facts join the existing document of that slug
"""

import unittest

from pipeline import topics
from pipeline.merger import merge_facts
from pipeline.concepts import assign_concepts

URL = "https://advertising.amazon.com/API/docs/en-us/test"
D1 = "2026-09-26T00:00:00+00:00"


def never_llm(claim, candidates):
    raise AssertionError("LLM seam must not be called for deterministic cases")


def fact(content, hint=None, **extra):
    base = {
        "url": URL, "date": D1, "content": content, "is_changed": "Y",
        "last_run": None, "source_type": "official",
        "community_agree_count": 0, "topic_id": hint, "topic_hint": hint,
        "confidence_score": 0.6, "status": "valid",
    }
    base.update(extra)
    return base


class RoutingTests(unittest.TestCase):
    def test_stream_claim_routes_deterministically_without_llm(self):
        slug, how = topics.route_fact(
            "Amazon Marketing Stream provides hourly campaign metrics and "
            "notifications of campaign changes in near real time.",
            choose_llm=never_llm)
        self.assertEqual((slug, how),
                         ("amazon-marketing-cloud-and-stream", "keywords"))

    def test_bulksheets_claim_routes_deterministically(self):
        slug, how = topics.route_fact(
            "Bulksheets is a spreadsheet-based tool used to manage sponsored "
            "ads campaigns.", choose_llm=never_llm)
        self.assertEqual(slug, "bulksheets-and-bulk-operations")
        self.assertEqual(how, "keywords")

    def test_subtopic_slugs_resolve_titles(self):
        self.assertEqual(topics.topic_title("github-repos-and-sdks"),
                         "GitHub Repositories and SDKs")
        self.assertEqual(topics.topic_title("login-with-amazon"),
                         "Login with Amazon")
        self.assertEqual(
            topics.topic_title("selling-partner-api-docs-and-models"),
            "Selling Partner API Docs and Models")
        self.assertIsNone(topics.topic_title("not-a-topic"))

    def test_reporting_beta_claim_now_routes_deterministically(self):
        # With phrase matching this claim has exactly one matching phrase
        # (reporting) and no tie — no LLM call needed anymore.
        slug, how = topics.route_fact(
            "The new Amazon Ads reporting API is in open beta.",
            choose_llm=never_llm)
        self.assertEqual((slug, how), ("reporting-api", "keywords"))

    def test_ambiguous_claim_makes_exactly_one_seam_call(self):
        calls = []

        def seam(claim, candidates):
            calls.append([slug for slug, _ in candidates])
            return "reporting-api"

        # Genuine tie: reporting-api and amazon-marketing-stream both match
        # two phrases, no third topic scores as high.
        slug, how = topics.route_fact(
            "The reporting API and Amazon Marketing Stream both deliver "
            "metrics.", choose_llm=seam)
        self.assertEqual((slug, how), ("reporting-api", "llm"))
        self.assertEqual(len(calls), 1)
        self.assertIn("amazon-marketing-cloud-and-stream", calls[0])
        self.assertIn("reporting-api", calls[0])

    def test_seam_failure_falls_back_to_general_topic(self):
        def broken(claim, candidates):
            raise ValueError("LLM returned invalid JSON")

        slug, how = topics.route_fact(
            "The reporting API and Amazon Marketing Stream both deliver "
            "metrics.", choose_llm=broken)
        self.assertEqual(slug, topics.FALLBACK_TOPIC)
        self.assertEqual(how, "fallback")

    def test_seam_returning_unknown_slug_is_rejected(self):
        def liar(claim, candidates):
            return "not-a-topic"

        slug, how = topics.route_fact(
            "The reporting API and Amazon Marketing Stream both deliver "
            "metrics.", choose_llm=liar)
        self.assertEqual(how, "fallback")


class SlugStabilityTests(unittest.TestCase):
    """Identity comes from the TOPIC, never from the claim's wording."""

    REWORD_A = ("Amazon Ads documentation includes a guide for creating a "
                "Sponsored Display campaign aimed at Amazon sellers or "
                "vendors.")
    REWORD_B = ("A separate getting-started guide exists for advertisers "
                "that do not sell on Amazon and want to create Sponsored "
                "Display campaigns.")

    def test_reworded_claims_route_to_the_same_slug(self):
        slug_a, _ = topics.route_fact(self.REWORD_A, choose_llm=never_llm)
        slug_b, _ = topics.route_fact(self.REWORD_B, choose_llm=never_llm)
        self.assertEqual(slug_a, "sponsored-display")
        self.assertEqual(slug_a, slug_b)

    def test_routed_fact_joins_existing_document_of_that_slug(self):
        existing = {"sponsored-display": {
            "id": "sponsored-display",
            "title": topics.topic_title("sponsored-display"),
            "type": "concept",
            "facts": [{"content": self.REWORD_A, "confidence_score": 0.6,
                       "status": "valid", "resolution": "single_source",
                       "first_seen": "2026-09-26",
                       "sources": [{"url": URL, "date": D1,
                                    "source_type": "official"}]}],
            "conflicts": [],
        }}
        assigned, new = assign_concepts(
            [fact(self.REWORD_B, hint="sponsored-display-get-started")],
            existing, match_llm=never_llm,
            route=lambda f: "sponsored-display")
        self.assertEqual(assigned[0]["concept_id"], "sponsored-display")
        self.assertEqual(new, {})  # no new document coined


class CatchAllCapTests(unittest.TestCase):
    def _concept(self, cid, facts):
        return {"id": cid, "title": topics.topic_title(cid) or cid,
                "type": "concept",
                "facts": [f["content"] if isinstance(f, str) else f
                          for f in facts],
                "conflicts": []}

    def test_under_cap_no_split(self):
        concept = self._concept(
            "selling-partner-api",
            [fact(f"The Selling Partner API area {i} covers partner "
                  f"operations for sellers.")
             for i in range(topics.MAX_TOPIC_FACTS)])
        self.assertEqual(topics.split_over_cap(concept), [concept])

    def test_over_cap_splits_by_declared_subtopics(self):
        contents = (
            [f"The Selling Partner API area {i} covers partner operations "
             f"for sellers." for i in range(9)]
            + ["The amzn/selling-partner-api-docs repository contains "
               "documentation for developers.",
               "The amzn/selling-partner-api-models repository contains "
               "OpenAPI models for developers.",
               "The selling-partner-api-docs repository is archived and its "
               "documentation moved."]
            + ["The selling-partner-api-sdk repository is described as the "
               "official SDK.",
               "The selling-partner-agentic-toolkit repository provides an "
               "agentic toolkit."])
        concept = {
            "id": "selling-partner-api",
            "title": "Selling Partner API", "type": "concept",
            "facts": [fact(c) for c in contents], "conflicts": []}
        self.assertGreater(len(concept["facts"]), topics.MAX_TOPIC_FACTS)
        pieces = topics.split_over_cap(concept)
        slugs = sorted(p["id"] for p in pieces)
        self.assertEqual(slugs, ["selling-partner-api",
                                 "selling-partner-api-docs-and-models",
                                 "selling-partner-api-sdks-and-tools"])
        # every fact survives exactly once
        total = sum(len(p["facts"]) for p in pieces)
        self.assertEqual(total, len(contents))
        # split slugs are taxonomy slugs — never derived from a sentence
        for piece in pieces:
            self.assertIn(piece["id"], topics.TOPIC_BY_SLUG)

    def test_topic_without_subtopics_never_splits(self):
        concept = self._concept(
            "sponsored-display",
            [fact(f"Sponsored Display fact number {i} about display "
                  f"campaigns.") for i in range(20)])
        self.assertEqual(len(topics.split_over_cap(concept)), 1)

    def test_merger_splits_overgrown_topic_end_to_end(self):
        # Distinct wording per fact so the Merger keeps them complementary
        # (token-identical claims would collapse as duplicates first).
        contents = (
            [f"The Selling Partner API area {i} covers partner operations "
             f"for sellers number {i}." for i in range(9)]
            + ["The amzn/selling-partner-api-docs repository contains "
               "documentation for developers.",
               "The amzn/selling-partner-api-models repository contains "
               "OpenAPI models for developers.",
               "The selling-partner-api-docs repository is archived and its "
               "documentation moved."]
            + ["The selling-partner-api-sdk repository is described as the "
               "official SDK.",
               "The selling-partner-agentic-toolkit repository provides an "
               "agentic toolkit."])
        out = merge_facts(
            [fact(c, hint="selling-partner-api") for c in contents],
            classify_llm=lambda a, b: "complementary", match_llm=never_llm,
            route=lambda f: "selling-partner-api")
        slugs = sorted(c["id"] for c in out["concepts"])
        self.assertEqual(slugs, ["selling-partner-api",
                                 "selling-partner-api-docs-and-models",
                                 "selling-partner-api-sdks-and-tools"])
        for concept in out["concepts"]:
            self.assertLessEqual(len(concept["facts"]), topics.MAX_TOPIC_FACTS)


class TaxonomyShapeTests(unittest.TestCase):
    def test_taxonomy_has_10_to_15_top_level_topics(self):
        self.assertGreaterEqual(len(topics.TOPICS), 10)
        self.assertLessEqual(len(topics.TOPICS), 15)

    def test_topic_slugs_are_unique_and_declared_titles_exist(self):
        slugs = [t.slug for t in topics.TOPICS]
        self.assertEqual(len(slugs), len(set(slugs)))
        for t in topics.TOPICS:
            self.assertTrue(t.title)
            self.assertTrue(t.keywords, f"{t.slug} needs keywords")

    def test_subtopic_ids_are_unique_globally(self):
        seen = set()
        for t in topics.TOPICS:
            for sub in t.subtopics:
                cid = f"{t.slug}-{sub.slug}"
                self.assertNotIn(cid, seen)
                seen.add(cid)


if __name__ == "__main__":
    unittest.main()
