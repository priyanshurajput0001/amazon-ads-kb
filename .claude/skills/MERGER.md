# Merger

> Reference documentation for the Merge stage — not a runnable agent. The
> code lives in `pipeline/merger.py`.

## Purpose

When two facts discuss the same topic, they may repeat each other,
contradict each other, or add different details. The Merger produces one
clean fact per piece of knowledge. It is the second of the two AI-assisted
stages: Claude judges the *relationship*, fixed rules decide the *outcome*.

## When To Use

Refer to this document when explaining duplicates, conflicts and who won,
`complementary_merge`, `unresolved_conflict`, or why two similar sentences
became one document.

## Input

The Validator's surviving facts (valid and valid_low_confidence), grouped
by topic. Rejected facts pass straight through, untouched.

## Process

1. For every pair of facts in the same topic group, **Claude answers one
   question only** — how are these two related?
   * `duplicate` — same claim, different wording
   * `conflicting` — same subject, contradictory values or information
   * `complementary` — same subject, different non-conflicting details
2. **Deterministic rules** then act on the label:
   * duplicates → collapse into one fact: one original sentence kept
     word-for-word, all contributing sources attached, strongest score kept
   * complementary → join both sentences word-for-word; skipped if any pair
     between the two sides is conflicting
   * conflicting → a fixed-priority duel: **authority** (official beats
     community, regardless of numbers) → **recency** (newer source date) →
     **majority** (more independent URLs). Loser dropped with a logged
     reason; a complete tie keeps **both** versions as `unresolved_conflict`.
3. Every outcome is labeled (`duplicate_merged`,
   `conflict_resolved_by_authority`, `complementary_merge`, ...) so the
   decision stays visible downstream.

## Rules

* Claude only classifies the relationship — it never picks winners and
  never writes merged text.
* Original sentences are never paraphrased; nothing is invented.
* Community majorities never outvote an official source.

## Output

The final fact list — one fact per piece of knowledge with all sources —
ready for the Publisher.

## Failure Behavior

An unreadable or invalid Claude label → the pair is reported and both facts
are kept separate (never a guessed relationship). Rejected facts are never
merged or deleted.

## Example

"The new Amazon Ads reporting API is in open beta." + "...offers
multi-dimensional, cross-account reporting." → Claude: `complementary` →
rules join both sentences into one fact with both sources. Two official
pages disagreeing ("1 business day" vs "5 business days") → Claude:
`conflicting` → rules resolve by recency/majority, or keep both if tied.

## Implementation

`pipeline/merger.py` — the Claude seam (`claude_cli_classify`) for
classification only; clustering, conflict precedence, and combination are
deterministic (`merge_facts`).
