# Fetcher

> Reference documentation for the Fetch stage — not a runnable agent. The
> code lives in `pipeline/fetch.py` and `pipeline/state.py`.

## Purpose

The Fetcher downloads a web page, takes its fingerprint (a *hash* — same
content always produces the same fingerprint), and remembers it. This lets
the system answer a cheap question before any expensive AI work: "has this
page changed since last time?"

## When To Use

Refer to this document when explaining or debugging anything about
downloading, caching, fingerprints, or the new/changed/unchanged verdicts —
including why an unchanged URL stops the whole pipeline early.

## Input

One or more web addresses (URLs), e.g.
`https://advertising.amazon.com/about-api`.

## Process

1. **Retrieve** the page through a chain of methods, best first: a
   clean-text service (Tavily basic, then Tavily advanced), then a plain
   download as last resort.
2. **Normalize** it into readable plain text (Markdown).
3. **Calculate the content fingerprint (hash)** of the text.
4. **Compare** with the fingerprint stored from the previous run.
5. **Determine the verdict:** `new` (first sighting), `changed`
   (fingerprint differs), `unchanged` (fingerprint matches), or `error`.
6. **Cache** the content in `state/cache/` under its fingerprint, so every
   historical version is kept and old copies are never overwritten.

## Rules

* Never fabricates content: if only raw HTML code (not readable text) can
  be downloaded, that is reported as-is and the next stage refuses it.
* A failed download keeps the last good fingerprint, so change detection
  survives temporary outages.
* The Fetcher itself never decides to skip later stages — the orchestrator
  reads its verdict and decides. `unchanged` means everything after Fetch
  can stop, saving the expensive Claude extraction.

## Output

The saved content file, a recorded fingerprint and fetch time in
`state/fetch_state.json`, and the verdict.

## Failure Behavior

All methods fail → verdict `error`, the URL is reported honestly, and the
pipeline stops for that URL only (other URLs continue). Unusable HTML →
reported as such; never converted by guessing.

## Example

Today: fingerprint `e3a9ef20...` recorded. Tomorrow: same page → same
fingerprint → verdict `unchanged` → Claude extraction skipped entirely.

## Implementation

`pipeline/fetch.py` (download chain and verdicts),
`pipeline/state.py` (persistent fingerprint records and change
classification), `state/cache/` (content-addressed copies).
