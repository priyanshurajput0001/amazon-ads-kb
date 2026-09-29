---
name: merger
description: Reference guide for the Merge stage (pipeline/merger.py + pipeline/concepts.py): topic-routed concept identity, deterministic-first pair classification with the same-extraction skip and per-concept LLM cap, conflict precedence (authority > recency > majority), and loser retention.
---

# Merger

> Reference skill for the Merge stage — not a runnable agent. The
> code lives in `pipeline/merger.py`, the concept layer it builds on in
> `pipeline/concepts.py`.

## Purpose

The Merger folds newly validated facts into the knowledge base's CONCEPTS.
It is where the maintained bundle participates in maintenance: new facts
are matched against the existing concept documents, so a source that
rewords or changes a value updates the concept it already belongs to
instead of spawning a duplicate. Claude judges the fuzzy relationships;
fixed rules decide every outcome.

## When To Use

Refer to this document when explaining duplicates, conflicts and who won,
concept identity, `complementary_merge`, `unresolved_conflict`, or how a
changed source value ends up in the same document as the old one.

## Input

The Validator's surviving facts (valid and valid_low_confidence) plus the
existing bundle loaded from `knowledge/`. Rejected facts pass straight
through, untouched.

## Process

1. **Concept assignment** (`pipeline/concepts.py`): every new fact is
   matched against the existing concepts (and the other new facts):
   * near-identical wording (token-set agreement ≥ 0.8) adopts the existing
     concept deterministically — no AI call;
   * clearly unrelated wording (< 0.30 overlap) starts a new concept;
   * the ambiguous band in between is decided by a one-shot Claude call
     ("same concept?") — candidates are found by deterministic overlap
     first, so the whole bundle is never sent to the model.
2. **Within each concept**, pairs involving a new fact are classified:
   * deterministic tripwires first — a negation or numeric flip at ≥ 0.5
     overlap is a conflict; ≥ 0.8 overlap is a duplicate;
   * plausible-but-undecided pairs go to the Claude seam
     (duplicate | conflicting | complementary);
   * pairs sharing no informative words simply coexist.
3. **Deterministic rules** then act on the labels:
   * duplicates → collapse into one fact: one original sentence kept
     word-for-word (an existing fact's wording preferred, so rewording
     never churns documents), all contributing sources attached, trust
     recomputed with the Validator's corroboration arithmetic;
   * complementary → the facts coexist in the concept as separate facts
     (sentences are never space-joined into run-on hybrids);
   * conflicting → the fixed-priority duel: **authority** (official beats
     community) → **recency** (newer source date) → **majority** (more
     independent URLs). The LOSING fact is retained in the concept's
     Conflicts section with its provenance, the rule, and what superseded
     it — never silently dropped. A complete tie keeps **both** facts
     current, each stamped `unresolved_conflict`.

## Rules

* Claude only classifies relationships and concept matches — it never picks
  winners and never writes merged text.
* Original sentences are never paraphrased; nothing is invented.
* Community majorities never outvote an official source.
* Contradictory information is never erased: it is surfaced, dated, and
  attributed.

## Output

Concept records — id, title, current facts with full provenance, retained
conflicts — one per concept, ready for the Publisher.

## Failure Behavior

An unreadable or invalid Claude answer → that pair (or match) is reported
and the facts stay separate (never a guessed relationship). Rejected facts
are never merged or deleted.

## Example

A GitHub page changes "licensed under MIT-0" to "licensed under
Apache-2.0": the new fact matches the license CONCEPT (same subject),
conflicts with the old fact, loses nothing — the newer value becomes
current, the MIT-0 fact moves to Conflicts with its original date and
`superseded_by` pointing at the new value.

## Implementation

`pipeline/merger.py` (`merge_facts`) with `pipeline/concepts.py`
(`assign_concepts`); Claude seams: `claude_cli_classify`,
`claude_cli_concept_match`.
