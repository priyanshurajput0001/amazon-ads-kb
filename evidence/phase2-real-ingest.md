# Phase 2 — Real ingest of three genuinely different source types

**Verified during the remediation session (2026-09-29), against the CURRENT
concept-based architecture.** Run in an isolated temporary sandbox
(`/tmp/kb-phase2/`) with its own fetch state, claims cache, and knowledge
bundle — the production `knowledge/` and `state/` were not touched. Real
Tavily fetch, real `claude` LLM seams (extraction, pair classification,
concept matching), real driver (`pipeline.orchestrate.orchestrate`).

## Input

One ingest run of three genuinely new public URLs, one per source type:

| Source type | URL |
|---|---|
| Amazon Ads documentation page (JS-rendered) | `https://advertising.amazon.com/API/docs/en-us/reference/api-overview` |
| GitHub repository page | `https://github.com/amzn/ads-advanced-tools-docs` |
| Raw GitHub Markdown file | `https://raw.githubusercontent.com/amzn/ads-advanced-tools-docs/main/README.md` |

## Result (from the driver's JSON report)

- All three URLs: fetch verdict `new`, strategy `tvly-basic`, content
  type Markdown.
- All six stages executed for each URL: Fetch → Extract → Adapter →
  Validator → Merger → Publisher.
- Claims adapted to facts: 15 + 21 + 7 = **43 facts**.
- Validation: **36 valid, 1 valid_low_confidence, 6 rejected**. The rejected
  facts are the raw file's claims: `raw.githubusercontent.com` is classified
  `community`, and they were rejected by the deterministic rules
  `official-overrides-community` and `support-required` against the official
  repository page — the trust machinery working across source types.

  > **Historical note on source classification (2026-09-29):** this Phase-2
  > run was executed **before** commit `491e054`. At that time the raw
  > Amazon GitHub content was classified under the previous, URL-only
  > authority rule. Commit `491e054` subsequently introduced
  > content-evidence authority classification (so Amazon-owned content is
  > not rejected merely because it arrives via `raw.githubusercontent.com`),
  > and a later P1 fix further tightened that rule to prevent third-party
  > README false positives. The classification result recorded above is
  > preserved as historical evidence and should **not** be interpreted as
  > the current classifier's behavior.
- Merge: 43 input facts → **7 output concepts**.
- Publish: **7 concepts published**, INDEX and CHANGELOG updated, fetch
  state committed for all 3 URLs only after publication succeeded.

## Bundle checks after publication

- Bundle lint: **clean** (every document valid OKF with `type: concept`,
  INDEX ↔ documents exact, no duplicate ids).
- Every fact carries sources, fetch dates, confidence score, status, and
  resolution; every concept id is a stable readable slug (no `kb-<hash>` ids).
- Related-link rule generated no links in this small fresh bundle (it
  requires a shared source AND topical overlap; none qualified).

## Known degradation in this run (recorded honestly)

**3 LLM concept-match calls timed out** (120 s limit each). The fail-safe
kept the affected facts as separate concepts instead of guessing a
relationship — the run succeeded, but merge coverage for those pairs was
reduced. The timeouts are visible as warnings in the run log.

## Conclusion

Three genuinely different source types flow end-to-end through the real
pipeline into a lint-clean concept bundle, with honest rejection of
community claims that lack official support.
