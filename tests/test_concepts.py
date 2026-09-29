"""Tests for the concept layer (pipeline/concepts.py).

These pin the identity behaviors the external review found missing:
  - reworded extraction resolves to the SAME concept
  - a changed VALUE (MIT-0 -> Apache-2.0) updates the same concept,
    never spawns an unrelated second one
  - many sources describing one concept contribute to ONE concept
"""

import tempfile
import unittest
from pathlib import Path

from pipeline import concepts
from pipeline.concepts import (
    canonical_tokens,
    check_concept,
    check_fact,
    coining_slug,
    hint_tokens,
    humanize,
    jaccard,
    load_bundle,
    parse_body,
    parse_fact_block,
    readable_slug,
    render_body,
)


def fact(content, url="https://advertising.amazon.com/x", **overrides):
    base = {
        "content": content,
        "confidence_score": 0.6,
        "status": "valid",
        "resolution": "single_source",
        "first_seen": "2026-09-27",
        "sources": [{"url": url, "date": "2026-09-27T00:00:00+00:00",
                     "source_type": "official"}],
        "topic_hint": None,
    }
    base.update(overrides)
    return base


def never_llm(new_claim, title, existing_claims):
    raise AssertionError("LLM seam must not be called for this case")


def llm_says_yes(new_claim, title, existing_claims):
    return True


def llm_says_no(new_claim, title, existing_claims):
    return False


class CanonicalizationTests(unittest.TestCase):
    def test_stemming_collapses_word_forms(self):
        self.assertEqual(canonical_tokens("supports"),
                         canonical_tokens("supported"))

    def test_stopwords_dropped_numbers_kept(self):
        self.assertEqual(canonical_tokens("The API v3 of the API"),
                         canonical_tokens("API v3 API"))

    def test_reworded_review_example_is_token_identical(self):
        a = canonical_tokens("Amazon Ads API supports X.")
        b = canonical_tokens("X is supported by the Amazon Ads API.")
        self.assertEqual(a, b)

    def test_humanize(self):
        self.assertEqual(humanize("amazon-ads-api-access"),
                         "Amazon Ads API Access")

    def test_readable_slug_drops_stopwords_without_stemming(self):
        # Slugs keep the words as given (no stemming) — only stopwords drop.
        self.assertEqual(readable_slug("The Amazon Ads API!"), "amazon-ads-api")


class NoSingleNoDuplicateTests(unittest.TestCase):
    """Review step 2: one LLM 'no' on a paraphrase must not create a second
    near-duplicate document.

    Three defenses, in order:
      1. overlap >= AUTO_SAME (the documented deterministic cutoff, 0.80):
         merge with NO LLM call at all — a no-answering seam cannot split it;
      2. ambiguous band [CANDIDATE_MIN, AUTO_SAME): the seam is asked twice —
         first with a bounded preview, then, on 'no', with the concept's FULL
         fact list; only TWO 'no' answers reject the candidate;
      3. every candidate rejected -> genuinely new concept.
    """

    def test_paraphrase_merges_even_when_seam_answers_no(self):
        """The documented cutoff: a reworded fact with >= AUTO_SAME overlap
        adopts the existing concept deterministically. The stub seam answers
        'no' — and is never even consulted."""
        existing = {"ads-api-reporting": {
            "id": "ads-api-reporting", "title": "Ads Api Reporting",
            "facts": [fact("The Amazon Ads API supports asynchronous report "
                           "requests.")],
        }}
        calls = []

        def always_no(new_claim, title, existing_claims):
            calls.append(new_claim)
            return False

        reword = fact("Asynchronous report requests are supported by the "
                      "Amazon Ads API.")
        self.assertGreaterEqual(
            jaccard(canonical_tokens(reword["content"]),
                    canonical_tokens(existing["ads-api-reporting"]["facts"][0]
                                     ["content"])),
            concepts.AUTO_SAME)
        facts, new = concepts.assign_concepts([reword], existing,
                                              match_llm=always_no)
        self.assertEqual(facts[0]["concept_id"], "ads-api-reporting")
        self.assertEqual(new, {})
        self.assertEqual(calls, [])  # the seam was never consulted

    def test_ambiguous_band_second_ask_with_full_fact_list_rescues_merge(self):
        """A fact in the ambiguous band whose best paraphrase evidence sits in
        a LATE fact of the concept: the preview ask says no, the full-list
        retry says yes -> merge. Only two 'no's create a new concept."""
        late_fact = ("Asynchronous report requests are supported at scale by "
                     "the Amazon Ads API.")
        existing = {"reporting-api": {
            "id": "reporting-api", "title": "Reporting Api",
            "facts": [fact("Export APIs replace the deprecated snapshots APIs.")
                      for _ in range(concepts.MATCH_PREVIEW)]
                      + [fact(late_fact)],
        }}
        asks = []

        def no_on_preview_yes_on_full(new_claim, title, existing_claims):
            asks.append(len(existing_claims))
            return len(existing_claims) > concepts.MATCH_PREVIEW

        new_fact = fact("Asynchronous report requests are supported at scale.",
                        url="https://b.example/y")
        best = jaccard(canonical_tokens(new_fact["content"]),
                       canonical_tokens(late_fact))
        self.assertLess(best, concepts.AUTO_SAME)      # ambiguous band...
        self.assertGreaterEqual(best, concepts.CANDIDATE_MIN)  # ...but a candidate
        facts, new = concepts.assign_concepts([new_fact], existing,
                                              match_llm=no_on_preview_yes_on_full)
        self.assertEqual(facts[0]["concept_id"], "reporting-api")
        self.assertEqual(new, {})
        self.assertEqual(asks, [concepts.MATCH_PREVIEW,
                                len(existing["reporting-api"]["facts"])])

    def test_ambiguous_band_two_no_answers_create_new_concept(self):
        existing = {"reporting-api": {
            "id": "reporting-api", "title": "Reporting Api",
            "facts": [fact("Export APIs replace the deprecated snapshots APIs.")],
        }}
        calls = []

        def always_no(new_claim, title, existing_claims):
            calls.append((new_claim[:30], len(existing_claims)))
            return False

        # ~0.5-0.6 overlap: inside the LLM band, below AUTO_SAME
        borderline = fact("Asynchronous report requests are supported at "
                          "scale by the API.")
        facts, new = concepts.assign_concepts([borderline], existing,
                                              match_llm=always_no)
        self.assertNotEqual(facts[0]["concept_id"], "reporting-api")
        self.assertIn(facts[0]["concept_id"], new)
        # exactly two asks: preview, then full list — both answered no
        self.assertEqual(calls, [(borderline["content"][:30], 1),
                                  (borderline["content"][:30], 1)])

    def test_retry_uses_the_full_fact_list_not_the_preview(self):
        existing = {"github-repos": {
            "id": "github-repos", "title": "Github Repos",
            "facts": [fact(f"Repository fact number {i} about open source.")
                      for i in range(concepts.MATCH_PREVIEW + 4)],
        }}
        seen_sizes = []

        def record(new_claim, title, existing_claims):
            seen_sizes.append(len(existing_claims))
            return False

        borderline = fact("Repository facts exist about open source code.",
                          url="https://b.example/y")
        concepts.assign_concepts([borderline], existing, match_llm=record)
        self.assertEqual(seen_sizes,
                         [concepts.MATCH_PREVIEW,
                          len(existing["github-repos"]["facts"])])

    def test_clearly_unrelated_fact_still_creates_new_concept(self):
        existing = {"license": {
            "id": "license", "title": "License",
            "facts": [fact("The repository is licensed under MIT-0.")],
        }}
        facts, new = concepts.assign_concepts(
            [fact("Bulksheets is a spreadsheet-based tool for sponsored ads "
                  "campaigns.", url="https://advertising.amazon.com/y")],
            existing, match_llm=llm_says_yes)
        self.assertNotEqual(facts[0]["concept_id"], "license")
        self.assertEqual(len(new), 1)


class AssignConceptsTests(unittest.TestCase):
    def test_reworded_fact_adopts_existing_concept_without_llm(self):
        existing = {"ads-api-reporting": {
            "id": "ads-api-reporting", "title": "Ads Api Reporting",
            "facts": [fact("The Amazon Ads API supports X.")],
        }}
        facts, new = concepts.assign_concepts(
            [fact("X is supported by the Amazon Ads API.")],
            existing, match_llm=never_llm)
        self.assertEqual(facts[0]["concept_id"], "ads-api-reporting")
        self.assertEqual(new, {})

    def test_changed_value_same_concept_via_llm_band(self):
        existing = {"license": {
            "id": "license", "title": "License",
            "facts": [fact("The repository is licensed under MIT-0.")],
        }}
        calls = []

        def spy(new_claim, title, existing_claims):
            calls.append((new_claim, title))
            return True

        facts, new = concepts.assign_concepts(
            [fact("The repository is licensed under Apache-2.0.")],
            existing, match_llm=spy)
        self.assertEqual(facts[0]["concept_id"], "license")
        self.assertEqual(new, {})
        self.assertEqual(len(calls), 1)

    def test_below_candidate_band_is_different_concept_no_llm(self):
        existing = {"license": {
            "id": "license", "title": "License",
            "facts": [fact("The repository is licensed under MIT-0.")],
        }}
        facts, new = concepts.assign_concepts(
            [fact("Sponsored Brands campaigns support video creative.",
                  url="https://advertising.amazon.com/y")],
            existing, match_llm=never_llm)
        # Not the license concept; exactly one genuinely new concept coined.
        self.assertNotEqual(facts[0]["concept_id"], "license")
        self.assertEqual(len(new), 1)
        self.assertIn(facts[0]["concept_id"], new)

    def test_five_sources_one_concept(self):
        facts = [fact(f"Source number {i} describes the Amazon Ads API "
                      f"reporting endpoints.", url=f"https://s{i}.example/x")
                 for i in range(5)]
        assigned, new = concepts.assign_concepts(facts, {}, match_llm=llm_says_yes)
        ids = {f["concept_id"] for f in assigned}
        self.assertEqual(len(ids), 1)
        self.assertEqual(len(new), 1)

    def test_identical_topic_hints_group_deterministically(self):
        # Complementary, low-overlap claims sharing one Extractor hint.
        a = fact("Applications may be submitted by advertisers and partners.",
                 topic_hint="api-access")
        b = fact("Approval may take 1 business day.",
                 topic_hint="api-access")
        assigned, new = concepts.assign_concepts([a, b], {}, match_llm=never_llm)
        self.assertEqual(assigned[0]["concept_id"],
                         assigned[1]["concept_id"])

    def test_llm_failure_keeps_facts_separate(self):
        def broken(new_claim, title, existing_claims):
            raise concepts.ConceptError("LLM returned invalid JSON")

        # ~0.5 overlap: inside the LLM band, below AUTO_SAME.
        a = fact("The reporting API offers asynchronous requests.")
        b = fact("Reports can be requested asynchronously at scale.")
        self.assertLess(jaccard(canonical_tokens(a["content"]),
                                canonical_tokens(b["content"])), 0.8)
        assigned, new = concepts.assign_concepts([a, b], {}, match_llm=broken)
        self.assertEqual(len(new), 2)  # fail-safe: separate, never guessed

    def test_candidate_order_is_deterministic(self):
        # Two concepts with equal overlap: candidates visited id-ascending.
        # Each candidate is now asked twice (preview, then full list) — a
        # 'no' must be confirmed before the candidate is rejected.
        existing = {
            "bbb-concept": {"id": "bbb-concept", "title": "Bbb",
                            "facts": [fact("The API supports bulk sheet "
                                           "operations for campaigns.")]},
            "aaa-concept": {"id": "aaa-concept", "title": "Aaa",
                            "facts": [fact("The API supports bulk sheet "
                                           "operations for campaigns.")]},
        }
        seen = []

        def spy(new_claim, title, existing_claims):
            seen.append(title)
            return False

        # Reworded so overlap lands in the LLM band, not the deterministic one.
        new = fact("Campaign bulk operations with sheets are supported at scale.")
        concepts.assign_concepts([new], existing, match_llm=spy)
        self.assertEqual(seen, ["Aaa", "Aaa", "Bbb", "Bbb"])

    def test_new_id_coined_from_unanimous_hint(self):
        a = fact("Approval may take 1 business day.", topic_hint="api-access")
        b = fact("Applications may be submitted by advertisers.",
                 topic_hint="api-access")
        _, new = concepts.assign_concepts([a, b], {}, match_llm=never_llm)
        self.assertEqual(list(new), ["api-access"])

    def test_new_id_collision_gets_suffix(self):
        existing = {"api-access": {"id": "api-access", "title": "Api Access",
                                   "facts": [fact("Something else entirely "
                                                  "about pricing models.")]}}
        a = fact("Approval may take 1 business day.", topic_hint="api-access")
        # The matching hint makes it a candidate; the LLM says different topic.
        _, new = concepts.assign_concepts([a], existing, match_llm=llm_says_no)
        self.assertEqual(list(new), ["api-access-2"])

    def test_coining_slug_prefers_representative_fact(self):
        hi = fact("Bulk operations manage campaigns in spreadsheets.",
                  confidence_score=0.9)
        lo = fact("Fees apply.", confidence_score=0.3)
        slug = coining_slug([lo, hi])
        self.assertEqual(slug, "bulk-operations-manage-campaigns-spreadsheets")


class BodyRoundTripTests(unittest.TestCase):
    def Concept(self, **overrides):
        base = {
            "id": "api-access", "title": "Api Access",
            "facts": [
                {"content": "Approval may take 1 business day.",
                 "confidence_score": 0.75, "status": "valid",
                 "resolution": "duplicate_merged", "first_seen": "2026-09-26",
                 "sources": [
                     {"url": "https://a.example/x", "source_type": "official",
                      "date": "2026-09-27T01:00:00+00:00"},
                     {"url": "https://b.example/y", "source_type": "community",
                      "date": "2026-09-28T02:00:00+00:00"}]},
            ],
            "conflicts": [
                {"content": "Approval may take 5 business days.",
                 "confidence_score": 0.60, "status": "valid",
                 "resolution": "conflict_resolved_by_recency",
                 "first_seen": "2026-09-25",
                 "superseded_by": "Approval may take 1 business day.",
                 "sources": [
                     {"url": "https://a.example/x", "source_type": "official",
                      "date": "2026-09-25T01:00:00+00:00"}]},
            ],
        }
        base.update(overrides)
        return base

    def test_render_parse_round_trip(self):
        concept = self.Concept()
        body = render_body(concept)
        facts, conflicts = parse_body(body)
        self.assertEqual(facts, concept["facts"])
        self.assertEqual(conflicts, concept["conflicts"])

    def test_multi_fact_round_trip(self):
        # Regression (found by the rebuild's bundle validation): consecutive
        # fact blocks separated by blank lines must parse as separate facts.
        concept = self.Concept(facts=[
            {"content": "Applications may be submitted by advertisers.",
             "confidence_score": 0.6, "status": "valid",
             "resolution": "single_source", "first_seen": "2026-09-26",
             "sources": [{"url": "https://a.example/x",
                          "source_type": "official",
                          "date": "2026-09-26T01:00:00+00:00"}]},
            {"content": "Approval may take 1 business day.",
             "confidence_score": 0.75, "status": "valid",
             "resolution": "duplicate_merged", "first_seen": "2026-09-25",
             "sources": [
                 {"url": "https://a.example/x", "source_type": "official",
                  "date": "2026-09-26T01:00:00+00:00"},
                 {"url": "https://b.example/y", "source_type": "community",
                  "date": None}]},
            {"content": "Test accounts exist for development.",
             "confidence_score": 0.6, "status": "valid",
             "resolution": "single_source", "first_seen": "2026-09-26",
             "sources": [{"url": "https://a.example/x",
                          "source_type": "official",
                          "date": "2026-09-26T01:00:00+00:00"}]},
        ])
        concept["conflicts"] = []
        facts, conflicts = parse_body(render_body(concept))
        self.assertEqual(facts, concept["facts"])
        self.assertEqual(conflicts, [])

    def test_multi_conflict_round_trip(self):
        concept = self.Concept(conflicts=[
            {"content": "The limit is 100 campaigns.",
             "confidence_score": 0.6, "status": "valid",
             "resolution": "conflict_resolved_by_recency",
             "first_seen": "2026-09-25",
             "superseded_by": "The limit is 500 campaigns.",
             "sources": [{"url": "https://a.example/x",
                          "source_type": "official",
                          "date": "2026-09-25T01:00:00+00:00"}]},
            {"content": "The limit is 50 campaigns.",
             "confidence_score": 0.6, "status": "valid",
             "resolution": "conflict_resolved_by_majority",
             "first_seen": "2026-09-24",
             "superseded_by": "The limit is 500 campaigns.",
             "sources": [{"url": "https://b.example/y",
                          "source_type": "community",
                          "date": "2026-09-24T01:00:00+00:00"}]},
        ])
        facts, conflicts = parse_body(render_body(concept))
        self.assertEqual(conflicts, concept["conflicts"])

    def test_rendered_sources_section_lists_urls(self):
        body = render_body(self.Concept())
        self.assertIn("## Sources", body)
        self.assertIn("https://a.example/x — official", body)
        self.assertIn("(confirmed 1 fact)", body)

    def test_parse_rejects_garbled_block(self):
        with self.assertRaises(concepts.ConceptError):
            parse_fact_block(["nonsense"])

    def test_unknown_date_round_trips_as_none(self):
        concept = self.Concept()
        concept["facts"][0]["sources"][0]["date"] = None
        facts, _ = parse_body(render_body(concept))
        self.assertIsNone(facts[0]["sources"][0]["date"])


class BundleLoadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.kdir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write_doc(self, name, doc_id, body):
        (self.kdir / name).write_text(
            f"---\nid: {doc_id}\ntitle: T {doc_id}\ntype: concept\n"
            f"sources:\n  - https://a.example/x\nconfidence: high\n"
            f"status: official\nlast_checked: 2026-09-27\n---\n\n{body}\n",
            encoding="utf-8")

    def test_load_bundle_reads_concepts_and_skips_bookkeeping(self):
        body = render_body({"id": "one", "title": "One", "facts": [
            fact("A claim about something.")]})
        self.write_doc("one.md", "one", body)
        (self.kdir / "INDEX.md").write_text("# Knowledge Index\n", encoding="utf-8")
        (self.kdir / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
        bundle = load_bundle(self.kdir)
        self.assertEqual(list(bundle), ["one"])
        self.assertEqual(bundle["one"]["facts"][0]["content"],
                         "A claim about something.")

    def test_load_bundle_skips_non_concept_docs(self):
        (self.kdir / "legacy.md").write_text(
            "---\nid: legacy\ntitle: Legacy\nsources:\n  - https://a.example/x\n"
            "confidence: high\nstatus: official\nlast_checked: 2026-09-27\n"
            "---\n\nbody\n", encoding="utf-8")
        self.assertEqual(load_bundle(self.kdir), {})

    def test_load_missing_dir_is_empty(self):
        self.assertEqual(load_bundle(self.kdir / "nope"), {})


class ContractTests(unittest.TestCase):
    def test_check_fact_rejects_multiline_content(self):
        with self.assertRaises(concepts.ConceptError):
            check_fact(fact("line one\nline two"), "x")

    def test_check_fact_rejects_bad_score(self):
        with self.assertRaises(concepts.ConceptError):
            check_fact(fact("ok", confidence_score=1.5), "x")

    def test_check_fact_rejects_bullet_content(self):
        with self.assertRaises(concepts.ConceptError):
            check_fact(fact("- looks like a bullet"), "x")

    def test_check_concept_requires_facts(self):
        with self.assertRaises(concepts.ConceptError):
            check_concept({"id": "x", "facts": []})

    def test_check_concept_rejects_bad_id(self):
        with self.assertRaises(concepts.ConceptError):
            check_concept({"id": "Not A Slug!", "facts": [fact("c")]})


if __name__ == "__main__":
    unittest.main()
