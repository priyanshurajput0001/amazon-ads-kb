# Phase 3 — Exact rerun / idempotency

**Verified during the remediation session (2026-09-29), against the CURRENT
concept-based architecture, in the same isolated sandbox as Phase 2**
(`/tmp/kb-phase2/`); production files untouched.

## Input

The exact same three source URLs as Phase 2, ingested again through the
same driver. The extractor LLM seam was wrapped in a counting wrapper so
"the Extractor was not called" is measured, not assumed. SHA-256 checksums
of every file in the sandbox knowledge bundle were taken before the rerun.

## Result

- All 3 sources: fetch verdict **`unchanged`**, `stopped_at: fetch`.
- Stages executed: `['fetch']` — Validator, Merger, and Publisher never ran.
- **Extractor LLM calls: 0** (measured by the wrapper).
- `knowledge_bundle_modified: false`.

## Byte-identical proof

- Files in bundle: **9 before, 9 after** (7 concept documents + INDEX.md +
  CHANGELOG.md).
- SHA-256 of every file identical before and after the rerun:
  **byte-identical, yes**.

## Conclusion

An unchanged source costs one fetch and nothing else: no AI calls, no
catalog churn, and a provably byte-identical knowledge bundle — the
safe-to-re-run contract holds on real sources.
