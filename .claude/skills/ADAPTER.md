# Adapter

> Reference documentation for the Adapter component — **not an AI agent**.
> It is deterministic glue between the Extractor and the Validator. The
> code lives in `pipeline/adapter.py`.

## Purpose

The Extractor and the Validator were built separately and speak slightly
different "languages." The Adapter translates claims into facts — pure
reshaping of already-recorded information, with no interpretation.

## When To Use

Refer to this document when explaining where `source_type`, `is_changed`,
or fact fields come from, or when a URL fails with "no claims extracted for
current content version".

## Input

The Extractor's saved claims plus the Fetcher's stored records (fetch
times, fingerprints) for the same URL.

## Process

1. Converts each claim into a **fact** with every field the Validator
   needs.
2. Derives fields from recorded information only — never invents:
   * `url`, `date` — from the fetch record
   * `source_type` — fixed rule: `advertising.amazon.com` pages and the
     `github.com/amzn` organization are `official`; everything else is
     `community`
   * `is_changed` — `N` (unchanged) only when a fetch *after* the claims
     were created re-saw the same fingerprint; otherwise `Y`
   * `community_agree_count` — always `0` today (no stage records
     people-agreement data; it is never made up)
3. Copies the claim's `topic_hint` into the `topic_id` grouping field,
   keeping the original hint alongside.
4. Passes through the supporting quote and provenance unchanged.

## Rules

* Deterministic: same input, same rules, same result — every time.
* No LLM, no network, no clock reads.
* Missing information is reported clearly (error per URL), never filled in.

## Output

One fact per claim, ready for the Validator (e.g. "22 facts, 2 of 3 URLs
ok, 1 failed with reason").

## Failure Behavior

URL never fetched, no claims for the current page version, or a corrupt
claims file → a clear error entry for that URL and zero facts from it.
Other URLs continue.

## Example

Claim "The Amazon Ads MCP server is in open beta." from the docs page →
Fact with the same sentence, its URL, fetch date, `source_type: official`,
`is_changed: Y`, and its topic group.

## Implementation

`pipeline/adapter.py` — `build_facts` (pure transformation) and
`adapt_url` (lookup + honest error reporting).
