# Phase 5 — Clean-clone reproducibility

**Recorded from the clean-clone verification run performed during the
remediation session (2026-09-29) and packaged into this file afterwards.**
The clone was **not** re-run for this document; every number below is the
one recorded at the time of the run. The run used the then-current
repository state (the remediation working tree that became commit
`5869271`, before commit `491e054` introduced content-evidence source
classification and before the later P1 tightening of that rule — neither
affects this run, whose single source is an `advertising.amazon.com` page
classified official by the unchanged official-host rule).

## Setup (as documented in README.md)

- Fresh `git clone` of the repository, plus an exact mirror of the
  then-current working tree (`rsync --delete`), so the clone matched the
  state that would be committed.
- `python3 -m venv .venv` and `pip install -r requirements.txt`
  (a no-op by design: the pipeline is stdlib-only).
- `claude` CLI and `tvly` (Tavily) CLI available on `PATH` per the
  documented prerequisites.
- **260/260 tests passed inside the clean clone's venv** at that point
  (the suite count then; it is 270 after the later test additions).
  > Annotated 2026-09-30: these figures were captured at the times stated
  > above and are not refreshed by later test additions; the suite at the
  > current HEAD runs 399 tests.

## One-source ingest via the documented user-facing command

Executed in the clone, with the clone's own `state/` and `knowledge/`:

```bash
.venv/bin/python -m pipeline.orchestrate --phrase \
  "ingest https://advertising.amazon.com/API/docs/en-us/guides/reporting/v3/get-started, update the bundle"
```

## Recorded result

- Fetch verdict: **`new`** (`tvly-basic`, Markdown).
- All six stages executed: Fetch → Extract → Adapter → Validator →
  Merger → Publisher.
- **32 claims** extracted → **32 valid facts** (0 rejected).
- Merge: **14 concepts**; publication: **12 published, 2 updated** — the 2
  updates are existing concepts from the bundled knowledge base being
  matched and extended in place, proving bundle participation in a fresh
  clone.
- **0 LLM timeouts.**
- Fetch state committed only after publication succeeded.

## Bundle checks in the clone

- Bundle lint: **clean** (49 concept documents — the 37 bundled plus 12
  new).
- Stable readable concept IDs only; **no `kb-<hash>` IDs**.
- `INDEX.md` matched the documents exactly.
- Multi-source provenance present (facts confirmed by 2–3 source URLs
  after the merge).

## Conclusion

A developer following the documented setup can clone, install, and ingest
a real source through the documented user-facing command form, producing a
lint-clean concept bundle with stable identities and full provenance.

## Limitation (stated plainly)

This evidence was packaged after the fact from the session's recorded run
report (kept at `/tmp/kb-sub-clone/ingest-report.json` during the session);
the clone itself was not re-executed when this file was written.
