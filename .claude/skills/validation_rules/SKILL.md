---
name: validation_rules
description: Reference for the Validator stage's scoring rules, status decisions, conflict heuristics, and its acceptance-test scenarios. Use when writing or reviewing code that calls pipeline.validator.validate_facts, when tuning its constants, when debugging an unexpected valid/rejected verdict, or when adding new Validator test scenarios.
---

# Validator Rules & Test Scenarios

Implementation: `pipeline/validator.py` (function `validate_facts(facts) -> list[dict]`).
Tests: `tests/test_validator.py` — class `SpecScenarioTests` mirrors the acceptance
checklist one-for-one; `tests/test_validator_thresholds.py` pins every
threshold boundary with literal expected values (0.60/0.30 bands, +0.15
corroboration step, +0.30 cap, +0.10 stability, 3-people minimum, 0.8/0.5
similarity bands) so a mutated constant fails the suite. Run:
`python3 -m unittest discover -s tests`.

The Validator is **fully deterministic**: no LLM, no network, no clock reads.
Identical input → identical output. Extractor's `confidence_pct` is **never read**.

## Input fact

```json
{"url": "...", "date": "ISO 8601", "confidence_pct": 75, "content": "claim text",
 "is_changed": "Y|N", "last_run": "ISO 8601|null", "source_type": "official|community",
 "community_agree_count": 0, "topic_id": "optional"}
```

Output: same dict + `confidence_score` (0.0–1.0), `status`, and `reason`
(required for `rejected` and `valid_low_confidence`; omitted for clean `valid`).
No fact is ever dropped; input dicts are never mutated.

## 1. Scoring (computed in integer hundredths, emitted as x.xx)

| Component | Value | Constant |
|---|---|---|
| Base: official source | `0.60` | `BASE_OFFICIAL` |
| Base: community, `community_agree_count >= 3` | `0.30` | `BASE_COMMUNITY` |
| Base: community, `< 3` people | `0.15` | `BASE_COMMUNITY_LOW` |
| Corroboration: per additional independent agreeing URL | `+0.15` | `CORROBORATION_STEP` |
| Corroboration cap | `+0.30` max | `CORROBORATION_MAX` |
| Stability: unchanged across the last 2 runs | `+0.10` | `STABILITY_BONUS` |

```text
confidence_score = clamp(base + corroboration + stability, 0.0, 1.0)
```

- `confidence_pct` from the Extractor is informational only — never in the math.
- **Stability condition**: `is_changed == "N"` AND `last_run` present. The
  Fetcher's `unchanged` verdict already means current content == prior stored
  run for that URL; `last_run` proves a prior run existed.
- **Independence** = distinct URLs. Two facts from the same URL never corroborate
  each other. Facts that contradict each other do not corroborate each other.

### Documented spec-gap decision

The base score for community sources with `<3` people is undefined in the
original spec. `0.15` was chosen so that low-quality community agreement stays
inside the `valid_low_confidence` band (max natural score 0.55), exactly as the
quality rule requires. Consequence: with shipped constants **no natural raw
score falls in 0.50–0.59**, so stability can never naturally tip a supported
fact from low-confidence into `valid` (the tipping test patches
`STABILITY_BONUS=15` to make it observable). Raising `BASE_COMMUNITY_LOW` to
`0.20` would make natural tipping possible (0.20+0.30+0.10=0.60).

## 2. Status thresholds

```text
score >= 0.60       -> valid
0.30 <= score < 0.60 -> valid_low_confidence    (0.30 is inclusive)
score <  0.30        -> rejected
```

Thresholds are evaluated on the clamped score; `0.30` exactly is
`valid_low_confidence`, not `rejected`.

## 3. Rule layers (first match wins; only ever lower status, never raise)

| # | Rule | Trigger | Effect |
|---|---|---|---|
| 1 | `official-overrides-community` | community fact contradicts any official fact | `rejected`, even with a raw score ≥ 0.6 and a community majority. No majority vote against official. |
| 2 | `support-required` | no official backing AND <2 independent community URLs agree AND `community_agree_count < 3` | `rejected` |
| 3 | `score-below-minimum` | score < 0.30 | `rejected` |
| 4 | `official-cross-page-contradiction` | official fact contradicts another official fact | capped at `valid_low_confidence` regardless of score (both sides — a deterministic validator cannot pick which official page is right; the Merger notes the disagreement) |
| 5 | `low-quality-community-cap` | agreeing group is community-only, every source `<3` people, no official backing, ≥2 community URLs | capped at `valid_low_confidence` — can **never** be `valid`, even if the raw score is ≥ 0.6 |
| — | `score-band` | none of the above | status from thresholds in §2 |

"Official backing" = the fact is official, or any agreeing fact in its group is
official. "≥2 independent community URLs" counts distinct community URLs in the
fact's agreement set (including its own). A lone community fact with
`community_agree_count >= 3` counts as supported (3 independent people).

## 4. Grouping

1. Facts with `topic_id` bucket by it (trimmed string).
2. Topic-less facts join an existing bucket by content similarity — topic
   buckets only at `AGREE_MIN` (must be the same claim), similarity buckets at
   `CONFLICT_MIN` (so contradicting versions of one fact land together).
   Greedy first-match in input order → deterministic.
3. Same `topic_id` groups are fully independent; no cross-topic corroboration.

## 5. Pairwise classification (inside a bucket, cross-URL only)

Normalized content = lowercase alphanumeric tokens. Similarity = token-set
Jaccard. Constants: `AGREE_MIN = 0.8`, `CONFLICT_MIN = 0.5`.

| Condition | Verdict |
|---|---|
| sim < 0.5 | `unrelated` (different facts) |
| negation flip (e.g. "X" vs "X **not**") or numeric mismatch ("1 day" vs "5 days"), sim ≥ 0.5 | `contradict` — these flips are checked **before** agreement, so "require" vs "do not require" (sim 0.78–0.88) is a conflict, not corroboration |
| sim ≥ 0.8 | `agree` → corroboration |
| 0.5 ≤ sim < 0.8 | `contradict` (materially different statement of the same fact) |

These bands are honest heuristics, not understanding: they catch wording
variants, negation flips, and value flips; semantic contradictions phrased very
differently are NOT caught (that fuzzy judgment belongs to later stages).

## 6. Acceptance-test scenarios (`SpecScenarioTests`)

| # | Scenario | Test | Expected |
|---|---|---|---|
| 1 | 1 official, no contradiction, stable 2 runs | `test_basic_official_pass_stable` | 0.6+0.1=**0.70** `valid` |
| 2 | Quality community sources agree, no official | `test_basic_community_pass_two_agreeing_quality_sources` | 2 sources: 0.3+0.15=**0.45** `valid_low_confidence` |
| 2b | Same with three sources | `test_three_agreeing_quality_community_sources_cross_into_valid` | each sees **two** corroborators: 0.3+0.30(capped)=**0.60** → crosses into `valid` |
| 3 | Reject-threshold boundary at 0.30 | `test_reject_threshold_is_inclusive_at_030` | 0.30 → `valid_low_confidence` (inclusive); 0.25 → `rejected` |
| 4 | 1–2 weak community mentions, no agreement, <0.3 | `test_true_rejection_weak_mentions_without_agreement` | 0.15 each → `rejected` |
| 5 | 2 weak community agree → forced low | `test_two_weak_agreeing_sources_forced_low_confidence` + `test_quality_cap_overrides_raw_score` | natural 0.30 → low; raw forced to 0.75 (patched `BASE_COMMUNITY_LOW=60`) → **still** low |
| 6 | 1 official says A, 3 community (3 people each) say B | `test_authority_overrides_majority_of_three` | A `valid` 0.6; B raw 0.60 + majority → **still** `rejected` |
| 7 | 2 officials, same topic, disagree | `test_cross_page_contradiction_with_topic_and_high_score` | raw 0.85 (corroborated + stable) → **still** `valid_low_confidence`, both sides |
| 8 | Stability tips low → valid | `test_stability_bonus_tips_low_confidence_to_valid` | patched `STABILITY_BONUS=15`: 0.45 → **0.60** `valid`; unstable twin stays low |
| 9 | Corroboration cap: 1 official + 4 corroborators | `test_corroboration_capped_with_four_extra_sources` | 0.6+**0.30**=0.90, not 1.20 |

Supporting coverage elsewhere in `tests/test_validator.py`:
clamp at 1.0 (`test_score_never_exceeds_one`, `test_score_clamped_when_constants_would_overflow`),
stability requires both conditions (`test_stability_bonus_requires_unchanged_and_prior_run`),
same-URL non-independence, topic-less fact joining a topic bucket,
`confidence_pct` ignored, empty input, determinism, output field preservation,
rejected facts retained, per-decision log lines, malformed-input errors, CLI.

## 7. Auditing a decision

Every fact emits one INFO line:

```text
INFO pipeline.validator: decision url=<url> group=<topic:<id>|auto:<n>> \
  score=<x.xx> status=<status> rule=<rule-name> reason=<...>
```

Run the CLI with `-v` for pairwise similarity/conflict DEBUG lines:
`python3 -m pipeline.validator FACTS.json -v` (`-` reads stdin; exit 2 on bad input).

## 8. Changing the constants

All tunables are module-level constants in `pipeline/validator.py` (§1 values
plus `AGREE_MIN`, `CONFLICT_MIN`, `COMMUNITY_PEOPLE_MIN=3`). Tests reference
them via `unittest.mock.patch("pipeline.validator.<CONST>", value)`, so a
constant rename must update those patch targets. After any change re-run the
full suite — the acceptance numbers above are asserted exactly.
