# Validator

> Reference documentation for the Validation stage — not a runnable agent.
> The code lives in `pipeline/validator.py`. Full scoring details also live
> in the [validation_rules skill](./validation_rules/SKILL.md).

## Purpose

The Validator gives every fact a trust score from 0.0 to 1.0 using fixed
arithmetic — no AI — and stamps it `valid`, `valid_low_confidence`
(keep but flag uncertain), or `rejected`. Trust decisions must be
identical for everyone and auditable, so they are written-down rules, not
opinions.

## When To Use

Refer to this document when explaining a fact's `confidence_score` or
`status`, why a fact was rejected, or why an official source beat a
community majority.

## Input

The Adapter's facts: each carries its sentence, source (URL, official or
community), how many independent people back a community source, and
whether its content was stable across fetches. The Validator scores ONE
batch at a time — it does not read `knowledge/`; comparison against the
maintained bundle is the Merger's job.

## Process

1. **Score each fact:**
   * starting points — official 0.60; community with 3+ independent people
     0.30; weaker community 0.15
   * corroboration — +0.15 per *other* web address stating the same fact
     (capped at +0.30)
   * stability — +0.10 when unchanged across two consecutive runs
   * final score clamped to 0.0–1.0
2. **Compare facts pairwise** with word-overlap rules plus two tripwires:
   a negation flip ("requires" vs "does not require") and a numbers flip
   ("1 day" vs "5 days") — to spot agreement or contradiction between
   sources on the same claim.
3. **Stamp status** by score band (≥0.60 valid; 0.30–0.59
   valid_low_confidence; <0.30 rejected).
4. **Apply safety rules:** an official source beats a community majority in
   a disagreement; official pages that contradict each other are BOTH
   capped at valid_low_confidence (a deterministic validator cannot pick
   between official pages — the Merger is where the tie is resolved or
   explicitly kept); agreement among only weak community sources can never
   be `valid`.

## Rules

* No LLM — the Validator does not use Claude to decide validity.
* It deliberately ignores the Extractor's own confidence opinion; trust is
  recomputed, never inherited.
* Every fact appears in the output — rejected facts stay, with a written
  reason; nothing is silently dropped.
* The thresholds (0.60 / 0.30, the +0.15 corroboration step, the +0.10
  stability bonus, the 0.8 / 0.5 similarity bands) are pinned by explicit
  boundary tests in `tests/test_validator_thresholds.py`.

## Output

Every input fact, plus `confidence_score`, `status`, and a `reason` for
rejections and low-confidence results.

## Failure Behavior

Malformed input is rejected up front with a clear error and nothing is
scored. Every decision is logged with the rule responsible, for later
auditing.

## Example

The same fact on two different official pages: 0.60 + 0.15 = **0.75,
valid**. A lone blog rumor: 0.15, no official backing → **rejected**.

## Implementation

`pipeline/validator.py` (`validate_facts`); the complete rules and test
scenarios are documented in
[`.claude/skills/validation_rules/SKILL.md`](./validation_rules/SKILL.md);
the boundary tests live in `tests/test_validator_thresholds.py`.
