# Phase 4 — Genuinely changed source (controlled seam)

**Verified during the remediation session (2026-09-29), against the CURRENT
concept-based architecture, in an isolated sandbox** (`/tmp/kb-phase4/`).
Production files untouched.

## Method (declared up front)

Real websites cannot be mutated on demand, so this phase uses a
**controlled/synthetic fetcher seam**: the only faked component is the
page content served to the pipeline. Extraction, pair classification, and
concept matching used the real `claude` LLM seams; adapter, validator,
merger, publisher, and fetch-state logic were the real code.

The source URL (`https://github.com/amzn/demo-widget`) is synthetic; its
`github.com/amzn` prefix makes it classify `official` by the pipeline's
fixed rule.

## Input

- Run 1 (2026-09-28): a three-claim page, including
  "The demo-widget repository is licensed under MIT-0."
- Run 2 (2026-09-29): the same source with (a) the license value changed to
  Apache-2.0, (b) the reporting claim reworded
  ("Asynchronous report requests are supported by the demo-widget API."),
  (c) the approval claim restated identically.

## Result

- Run 2 fetch verdict: `changed`; all stages re-ran; extraction skipped
  because claims for that content version were already cached.
- **Document count: 3 before, 3 after — no duplicate documents.**
- Concept ids identical across both runs: `demo-widget-license`,
  `demo-widget-api-overview`, `demo-widget-approval-time`.
- **Changed value**: `demo-widget-license` now carries
  "…licensed under Apache-2.0." as its current fact
  (`resolution: conflict_resolved_by_recency`); the MIT-0 fact is RETAINED
  under `### Conflicts` with `first_seen: 2026-09-28`, its original fetch
  date, and `superseded_by:` pointing at the new value — nothing erased.
- **Reworded claim**: `demo-widget-api-overview` kept the ORIGINAL wording
  verbatim (`resolution: duplicate_merged`), gained the refreshed source
  date, kept `first_seen: 2026-09-28` — rewording created no new concept.
- **Identical claim**: `demo-widget-approval-time`, same id,
  `duplicate_merged`, date refreshed.
- Sandbox bundle lint after both runs: **clean**.

## Conclusion

A changed source updates the concept it already belongs to: new value
becomes current by deterministic precedence, the old value survives as a
dated, attributed conflict, and neither rewording nor restatement creates
duplicate documents.

## Limitation (stated plainly)

This demonstrates the changed-source machinery through the pipeline's own
injection seam, not against a real website whose content changed — real
sites cannot be mutated on demand. The production bundle consequently
contains no conflict entries yet; the conflict path is exercised here and
by unit tests, not by production data.
