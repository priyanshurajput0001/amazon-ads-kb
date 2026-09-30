---
name: relevance-gate
description: Reference guide for the Relevance gate stage (pipeline/relevance.py): deterministic drop-tokens and allow-list keywords first, exactly ONE cached Claude yes/no call for borderline claims (state/gate_cache.json), every drop logged with its reason to state/dropped.json, and fail-open behavior when the seam is down.
---

# Relevance Gate

> Reference skill for the Relevance gate stage — not a runnable agent.
> The code lives in `pipeline/relevance.py`.

## Purpose

Sits between Extract and Adapter and drops claims that are not about
Amazon Ads, its advertising APIs, or seller/vendor advertising tooling. A
GitHub organization page yields genuinely off-topic claims (an Alexa SDK,
a computer-vision-in-Excel repo); without the gate they would flow into
the bundle as concepts nobody asked for.

## When To Use

Refer to this document when explaining or debugging why a claim was
dropped (check `state/dropped.json`), why a borderline claim was kept
without a Claude call (check `state/gate_cache.json`), or what
`python3 -m pipeline.relevance CLAIMS_JSON` does.

## Input

One Extractor claims document (the JSON with `source_url` and `claims`).

## Process — decision order per claim

1. **DROP tokens** (deterministic): any hit means dropped — named
   off-topic artifacts from the ingested corpus (alexa, echo, excel,
   pecos, smoke, amazon pay, kindle, twitch, ...). Extend this list, never
   the LLM, when a new off-topic artifact appears.
2. **STRONG tokens** (deterministic): any hit means kept — ads-specific
   words (advertising, sponsored, campaign, bulksheet, reporting,
   seller, ...) are decisive evidence of relevance.
3. **Borderline → ONE Claude yes/no call** (`claude -p`, plain one-shot
   prompt, JSON answer `{"in_scope": true|false}`). The verdict is cached
   in `state/gate_cache.json` keyed by the claim text's SHA-256 and
   replayed on later runs, so the gate stays deterministic ACROSS runs —
   a rebuild never re-asks a claim that was already judged.

Both keyword lists are normalized through the shared stemmer at import
time: a human writes surface forms ("pecos", "advertising") while
matching stays canonical (stem("pecos") == "peco").

## Rules

* Every dropped claim is logged to `state/dropped.json` with its reason
  and decider, keyed by sha256(url|claim). Re-filtering the same claims
  leaves the log byte-identical: the FIRST record wins, and its date is
  the provenance that matters.
* **Fail-open**: if the Claude seam is unavailable or unparseable, the
  claim is KEPT with `decided_by: error`. Dropping knowledge because a
  helper was down would lose content silently; a later healthy run can
  still drop it.
* The gate never affects the unchanged-source short-circuit: it runs
  after Extract, which only runs for new/changed content.

## Output

`(kept_claims, dropped_records)` — the claims document itself is never
mutated; the raw extraction stays the record of what the page said.

## Failure Behavior

A broken seam keeps the claim (see fail-open above); an unreadable cache
or drop log is logged and worked around (cache ignored, log recreated).

## Example

A claim mentioning "an Alexa voice skill" hits the `alexa` drop token and
is rejected with `decided_by: drop-tokens` — no Claude call, and the
reason is written to `state/dropped.json`.

## Implementation

`pipeline/relevance.py` (keyword lists, gate order, seam, cache, audit
log), `state/gate_cache.json` (borderline verdicts), `state/dropped.json`
(drop audit log).
