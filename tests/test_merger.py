"""Tests for pipeline.merger — offline; both LLM seams are faked.

Focus: the behaviors the external review demanded —
  - new facts merge INTO existing concepts (the bundle participates)
  - conflicts keep provenance; losers are retained, never silently dropped
  - duplicates collapse to one fact with all sources
  - complementary facts coexist (no run-on space-joined sentences)
  - unresolved ties keep both sides, stamped unresolved_conflict
"""

import unittest

from pipeline.merger import (
    LlmError,
    MergerError,
    _classify_deterministic,
    _needs_llm,
    merge_facts,
)
from pipeline.concepts import canonical_tokens

URL = "https://advertising.amazon.com/API/docs/en-us/test"
URL2 = "https://github.com/amzn/some-repo"
D1 = "2026-09-26T00:00:00+00:00"
D2 = "2026-09-27T00:00:00+00:00"
D3 = "2026-09-28T00:00:00+00:00"


def fact(content, url=URL, date=D2, source_type="official", score=0.6,
         status="valid", hint=None, **extra):
    base = {
        "url": url, "date": date, "content": content, "is_changed": "Y",
        "last_run": None, "source_type": source_type,
        "community_agree_count": 0, "topic_id": hint, "topic_hint": hint,
        "confidence_score": score, "status": status,
    }
    base.update(extra)
    return base


def bundle_fact(content, url=URL, date=D1, source_type="official",
                score=0.6, resolution="single_source", first_seen=None):
    """A fact as it comes back out of an existing concept document."""
    return {
        "content": content,
        "confidence_score": score,
        "status": "valid",
        "resolution": resolution,
        "first_seen": first_seen or (date or "")[:10],
        "sources": [{"url": url, "date": date, "source_type": source_type}],
    }


def existing_concept(cid, title, facts, conflicts=()):
    return {"id": cid, "title": title, "type": "concept",
            "facts": list(facts), "conflicts": list(conflicts)}


def llm_yes(a, b):
    return "complementary"


def llm_match_no(new_claim, title, existing_claims):
    return False


def llm_match_yes(new_claim, title, existing_claims):
    return True


class ContractTests(unittest.TestCase):
    def test_rejects_non_list(self):
        with self.assertRaises(MergerError):
            merge_facts({"not": "a list"}, classify_llm=llm_yes,
                        match_llm=llm_match_no)

    def test_rejects_missing_status(self):
        with self.assertRaises(MergerError):
            merge_facts([{"url": URL, "content": "x",
                          "source_type": "official"}],
                        classify_llm=llm_yes, match_llm=llm_match_no)

    def test_rejected_facts_pass_through_unmerged(self):
        out = merge_facts([fact("boo", status="rejected")],
                          classify_llm=llm_yes, match_llm=llm_match_no)
        self.assertEqual(out["concepts"], [])
        self.assertEqual(len(out["rejected"]), 1)


class DeterministicClassificationTests(unittest.TestCase):
    def test_value_flip_is_conflict(self):
        a = canonical_tokens("License is MIT-0 for the repo.")
        b = canonical_tokens("License is Apache-2.0 for the repo.")
        self.assertEqual(
            _classify_deterministic(a, b), "conflicting")

    def test_negation_flip_is_conflict(self):
        a = canonical_tokens("The API requires approval first.")
        b = canonical_tokens("The API does not require approval first.")
        self.assertEqual(_classify_deterministic(a, b), "conflicting")

    def test_reworded_duplicate_is_duplicate(self):
        a = canonical_tokens("Amazon Ads API supports X.")
        b = canonical_tokens("X is supported by the Amazon Ads API.")
        self.assertEqual(_classify_deterministic(a, b), "duplicate")

    def test_low_overlap_is_undecided(self):
        a = canonical_tokens("Approval may take one business day.")
        b = canonical_tokens("Bulk sheets export campaign statistics.")
        self.assertIsNone(_classify_deterministic(a, b))

    def test_needs_llm_gates_generic_only_pairs(self):
        a = canonical_tokens("The Amazon Ads API dashboard exists.")
        b = canonical_tokens("Amazon Ads API fees are standard.")
        # only generic words shared -> no LLM call
        self.assertFalse(_needs_llm(a, b))

    def test_needs_llm_accepts_informative_pairs(self):
        a = canonical_tokens("Report requests are asynchronous.")
        b = canonical_tokens("Asynchronous reporting is supported.")
        self.assertTrue(_needs_llm(a, b))


class MergeIntoExistingConceptTests(unittest.TestCase):
    def test_changed_value_updates_same_concept_and_keeps_loser(self):
        old = fact("The repository is licensed under MIT-0.", URL2, D1)
        out1 = merge_facts([old], classify_llm=llm_yes, match_llm=llm_match_no,
                           today="2026-09-26")
        concept1 = out1["concepts"][0]
        existing = existing_concept(concept1["id"], concept1["title"],
                                    concept1["facts"])

        new = fact("The repository is licensed under Apache-2.0.", URL2, D3)
        out2 = merge_facts([new], existing={concept1["id"]: existing},
                           classify_llm=llm_yes, match_llm=llm_match_yes,
                           today="2026-09-28")
        concept2 = out2["concepts"][0]
        self.assertEqual(concept2["id"], concept1["id"])  # SAME concept
        self.assertEqual(len(concept2["facts"]), 1)
        winner = concept2["facts"][0]
        self.assertIn("Apache-2.0", winner["content"])
        self.assertEqual(winner["resolution"], "conflict_resolved_by_recency")
        # The MIT-0 fact is retained with provenance, marked superseded.
        self.assertEqual(len(concept2["conflicts"]), 1)
        loser = concept2["conflicts"][0]
        self.assertIn("MIT-0", loser["content"])
        self.assertEqual(loser["superseded_by"], winner["content"])
        self.assertEqual(loser["sources"][0]["date"], D1)  # date preserved

    def test_reworded_fact_same_concept_duplicate_merged(self):
        old = fact("The Amazon Ads API supports asynchronous report requests.",
                   URL, D1)
        out1 = merge_facts([old], classify_llm=llm_yes, match_llm=llm_match_no)
        concept1 = out1["concepts"][0]
        existing = existing_concept(concept1["id"], concept1["title"],
                                    concept1["facts"])

        reword = fact("Asynchronous report requests are supported by the "
                      "Amazon Ads API.", URL2, D3)
        out2 = merge_facts([reword], existing={concept1["id"]: existing},
                           classify_llm=llm_yes, match_llm=llm_match_yes)
        concept2 = out2["concepts"][0]
        self.assertEqual(concept2["id"], concept1["id"])
        self.assertEqual(len(concept2["facts"]), 1)
        self.assertEqual(concept2["facts"][0]["resolution"], "duplicate_merged")
        urls = {s["url"] for s in concept2["facts"][0]["sources"]}
        self.assertEqual(urls, {URL, URL2})
        # Existing wording is kept verbatim — no churn on rewording.
        self.assertEqual(concept2["facts"][0]["content"], old["content"])

    def test_complementary_facts_coexist_without_joining(self):
        a = fact("Applications may be submitted by advertisers and partners.",
                 URL, D1, hint="api-access")
        b = fact("Approval may take 1 business day.", URL2, D2,
                 hint="api-access")
        out = merge_facts([a, b], classify_llm=llm_yes,
                          match_llm=llm_match_yes)
        self.assertEqual(len(out["concepts"]), 1)
        contents = [f["content"] for f in out["concepts"][0]["facts"]]
        self.assertEqual(sorted(contents), sorted([a["content"], b["content"]]))

    def test_conflict_authority_official_beats_community(self):
        official = fact("The limit is 100 campaigns.", URL, D1,
                        source_type="official")
        community = fact("The limit is 500 campaigns.",
                         "https://blog.example/limits", D3,
                         source_type="community")
        out = merge_facts([official, community],
                          classify_llm=lambda a, b: "conflicting",
                          match_llm=llm_match_yes)
        concept = out["concepts"][0]
        self.assertEqual(len(concept["facts"]), 1)
        self.assertIn("100 campaigns", concept["facts"][0]["content"])
        self.assertEqual(concept["facts"][0]["resolution"],
                         "conflict_resolved_by_authority")
        self.assertIn("500 campaigns", concept["conflicts"][0]["content"])

    def test_unresolved_tie_keeps_both(self):
        a = fact("The limit is 100 campaigns.", URL, D1)
        b = fact("The limit is 500 campaigns.", URL2, D1)
        out = merge_facts([a, b],
                          classify_llm=lambda a, b: "conflicting",
                          match_llm=llm_match_yes)
        concept = out["concepts"][0]
        self.assertEqual(len(concept["facts"]), 2)
        self.assertTrue(all(f["resolution"] == "unresolved_conflict"
                            for f in concept["facts"]))
        self.assertEqual(concept["conflicts"], [])

    def test_five_sources_one_fact_all_provenance(self):
        facts = [fact("The Amazon Ads API supports asynchronous report "
                      "requests.", f"https://s{i}.example/x", f"2026-09-2{i}")
                 for i in range(5)]
        out = merge_facts(facts,
                          classify_llm=lambda a, b: "duplicate",
                          match_llm=llm_match_yes)
        self.assertEqual(len(out["concepts"]), 1)
        fact_out = out["concepts"][0]["facts"][0]
        self.assertEqual(len(fact_out["sources"]), 5)
        self.assertEqual(fact_out["resolution"], "duplicate_merged")
        # Corroboration recompute: 5 independent URLs lifts the score.
        self.assertGreater(fact_out["confidence_score"], 0.6)

    def test_existing_existing_pairs_never_reclassified(self):
        calls = []

        def spy(a, b):
            calls.append((a["content"], b["content"]))
            return "complementary"

        settled_a = "Approval may take 1 business day."
        settled_b = "Approval may take 5 business days."
        existing = existing_concept("api-access", "Api Access", [
            bundle_fact(settled_a, URL, D1),
            bundle_fact(settled_b, URL2, D1),
        ])
        # New value for the same subject: conflicts with BOTH settled facts
        # (deterministic numeric flips), wins both by recency.
        new = fact("Approval may take 3 business days.", URL2, D3,
                   hint="api-access")
        out = merge_facts([new], existing={"api-access": existing},
                          classify_llm=spy, match_llm=llm_match_yes)
        # The settled pair (1 day vs 5 days) is never re-compared — the LLM
        # seam sees no pair at all (every kept pair was deterministic).
        self.assertEqual(calls, [])
        concept = out["concepts"][0]
        self.assertEqual([f["content"] for f in concept["facts"]],
                         [new["content"]])
        self.assertEqual({c["content"] for c in concept["conflicts"]},
                         {settled_a, settled_b})

    def test_llm_label_error_keeps_facts_separate(self):
        def broken(a, b):
            raise LlmError("LLM returned invalid JSON")

        a = fact("Report requests are asynchronous everywhere.")
        b = fact("Asynchronous reporting is supported at scale.")
        out = merge_facts([a, b], classify_llm=broken, match_llm=llm_match_no)
        self.assertEqual(len(out["concepts"]), 2)


if __name__ == "__main__":
    unittest.main()
