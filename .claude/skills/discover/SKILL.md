---
name: discover
description: Reference guide for the optional Discover stage (pipeline/discover.py): reading candidate URLs out of the cached pages of seed URLs, optional tvly search behind TAVILY_API_KEY, the in-scope host rules, and the deterministic de-duplicated, capped candidate list handed to Fetch.
---

# Discover

> Reference skill for the optional Discover stage — not a runnable agent.
> The code lives in `pipeline/discover.py`.

## Purpose

Find NEW candidate URLs from what the pipeline already fetched. Discovery
reads only cached pages (plus an optional search) — it never crawls. It
widens the source list without ever leaving the operator's scope rules.

## When To Use

Refer to this document when explaining or debugging where candidate URLs
come from, why a URL was skipped as out of scope or already known, or what
`python3 -m pipeline.orchestrate --discover URL ...` does before Fetch.

## Input

One or more seed URLs whose pages were fetched on an earlier run (their
content must be in the cache, keyed by committed content hash).

## Process

1. **Read the links** out of each seed URL's cached page: markdown link
   syntax plus bare URLs for `.md` caches; `href` attributes plus bare URLs
   for `.html` caches, in page order.
2. **Optionally enrich** with `tvly search --json` — only when
   `TAVILY_API_KEY` is set and the `tvly` CLI exists (a seam; faked in
   tests, never called otherwise).
3. **Keep only in-scope URLs**: the official Amazon Ads docs host
   (`advertising.amazon.com`), Amazon's GitHub org
   (`github.com/amzn...`, `raw.githubusercontent.com/amzn...`), plus any
   host the operator already deliberately ingested (hosts present in fetch
   state are treated as past scope decisions).
4. **Drop URLs already known** to `state/fetch_state.json` — committed OR
   pending.
5. **Return the candidates**: de-duplicated (first sighting wins), in a
   deterministic order (seed order, then position within the page),
   capped (default 10).

## Rules

* Deterministic by construction: same caches + same state give the same
  candidate list. No LLM anywhere in this stage.
* Seeds without a cached page are skipped and reported, never fetched.
* The search seam is optional and never fatal: any failure is logged and
  discovery continues with the page links only.

## Output

A JSON report: `candidates` (the capped list), `seeds_scanned`,
`search_used`, `skipped` counts (`already_in_state`, `out_of_scope`),
and the `cap`. The orchestrator hands the candidates to Fetch; without
`--discover` the run is exactly the user-provided URLs.

## Failure Behavior

Unreadable state raises a `DiscoverError` (exit 2 from the CLI). A missing
cache entry, a missing `tvly`, or a broken search seam degrades to fewer
candidates — never to a wrong URL.

## Example

`python3 -m pipeline.discover https://advertising.amazon.com/about-api`
prints the report; the orchestrator's `--discover` flag wires the same
call in as an optional first stage.

## Implementation

`pipeline/discover.py` (link extraction, scope rules, cap, report),
`pipeline/state.py` (fetch-state read), `state/cache/` (the cached pages
that are actually scanned).
