# Verification Evidence

**Project:** Amazon Ads Knowledge Acquisition System (`/Users/priyanshurajput/amazon-ads-kb`)
**Verification date:** 2026-09-27 (UTC) · **Python:** 3.14.7 · **claude CLI:** 2.1.282 · **tvly:** 0.1.8
**Repository state after verification:** no tracked file modified (git clean except this untracked `evidence/` directory); production `state/` and `knowledge/` untouched — all experiments ran in `/tmp/kb-evidence/` sandboxes.

Method note: no implementation or test file was modified at any point. The only
code written for this verification was driver scripts (embedded verbatim at the
top of each output file) that call the pipeline's public functions and
documented injection seams — the same seams `tests/` uses. One bug in a
verification driver itself was found and fixed (Phase 5, noted in its output
header); project code was never touched.

---

## 1. Full Test Suite
- Command: `python3 -m unittest discover -s tests -v` (run from the repo root)
- Total: **192**
- Passed: **192**
- Failed: **0**
- Skipped: **0**
- Duration: **0.151 s** (suite-reported; offline, no network)
- Warnings/errors: none. The `WARNING`/`INFO` lines and one argparse usage
  message visible in the output are expected stage-logging output from tests
  that exercise failure paths; every test result line reads `ok`.
- Evidence: `evidence/test-suite-output.txt` (full verbose run; final summary
  line `Ran 192 tests in 0.151s` / `OK` at the end of the file)

## 2. Safe Re-run Evidence
- Run 1 result: verdict `new` → full pipeline (`fetch, adapter, validator,
  merger, publisher`). Real claude CLI extraction produced **6 claims**,
  adapter 6 facts, validator 6/6 valid, merger 6 → 5 (one real LLM
  classification produced a `complementary_merge`), publisher created **5
  documents + INDEX.md + CHANGELOG.md** in the sandbox. Duration 96.5 s
  (dominated by the real LLM call).
- Run 2 result (exact same command, same unchanged content): verdict
  `unchanged`, `stopped_at: "fetch"`, `stages_executed: ["fetch"]`,
  `knowledge_bundle_modified: false`. Duration 0.0 s.
- Hash comparison: run 1 sha256 `7ed29293dfd8a66f…` == run 2 sha256
  `7ed29293dfd8a66f…` → identical (unchanged source correctly detected).
- Extraction skipped: **yes** (`"extract"` absent from the run 2 entry;
  `extraction_skipped: true` in VERDICTS)
- Merge skipped: **yes** (`merger` not in run 2 `stages_executed`)
- Publishing skipped: **yes** (`publisher` not in run 2 `stages_executed`)
- New documents on second run: **0**
- Duplicate documents: **0**
- Existing files changed: **none** — full knowledge-tree SHA-256 snapshot
  (docs + INDEX + CHANGELOG) byte-identical after run 2
  (`knowledge_tree_byte_identical: true`, `doc_ids_identical: true`)
- Evidence: `evidence/rerun-output.txt` (VERDICTS JSON at the end; per-run
  reports, state-file dumps, and file-hash snapshots in the body)
- Caveat (documented in the same file, Section A): the live `tvly` extractor
  was DNS-failing during the verification window, so the transport replays
  **real production internet content** (the actual github.com/amzn markdown
  this pipeline fetched from the real internet on 2026-09-27, on disk in
  `state/cache/`) through the fetcher injection seam. Every downstream stage
  (extraction LLM, adapter, validator, merger LLM, publisher) is the real
  production one. A live-network attempt with the real default fetcher is
  captured too: it honestly stopped at fetch with `"cache is HTML, not
  Markdown; extraction unsupported"` and `knowledge_bundle_modified: false`.

## 3. De-duplication Evidence
- Test scenario: the same logical fact ("default rate limit of 10 requests
  per second per account") stated on **two different URLs in different
  wording**, run end-to-end through the real pipeline (real claude
  extraction → adapter → validator → real claude pair-classification →
  publisher).
- Duplicate detected: **yes.** The real extractor independently assigned both
  claims the topic hint `api-rate-limits`; the real merger LLM classified the
  pair and the merger logged
  `merge group of 2 -> resolution=duplicate_merged status=valid sources=2`.
  A direct probe of the classifier seam returned the raw answer
  `>>> RAW claude_cli_classify ANSWER FOR THE PAIR: 'duplicate'`.
- Merge/classification result: 2 validated facts → **1 merged fact**
  (`resolution: duplicate_merged`), keeping the highest-scoring member's
  content verbatim and carrying **both source URLs** on the merged fact.
- Final document count: **1** document (`kb-729977e2290f9316`), whose
  frontmatter `sources:` lists both URLs and whose body records
  `resolution: duplicate_merged`.
- Duplicate documents created: **0** (`second_copy_created: false` in both
  Part A and Part B verdicts)
- Evidence: `evidence/deduplication-output.txt` (Part A end-to-end report,
  extracted claims with quotes/hints, final document text; Part B raw
  classification answer, merged fact JSON, publisher report)

## 4. Change Detection Evidence
Two controlled experiments (fixture content swapped between runs through the
project's fetcher seam; all stages real):

**Experiment 4b — distinct-topic claims (the update-in-place path):**
- Original hash (v1): `1eb9e0a7fde848d3…` — New hash (v2): `2d73d4cc7ac6715…`
- Change detected: **yes** (`run3_verdict: "changed"`, `hash_changed: true`)
- Pipeline stages executed on change: all — `fetch, adapter, validator,
  merger, publisher` (extraction re-ran: 3 claims vs 2 before)
- Existing document updated: **yes, in place.** Publisher report:
  `published: 1, updated: 2`. The two existing documents kept their ids
  (`kb-716187a0a45e1d6b`, `kb-35495514217fb208`) and filenames; unified
  diffs in the output show the only changed line is the refreshed
  `fetched <timestamp>` source line. Only the genuinely new fact became a new
  document (`kb-fbc1c11683ec3c65`). `duplicate_ids: false`,
  doc count 2 → 3.
- UNCHANGED → SKIP (interleaved run 2): verdict `unchanged`,
  `stages_executed: ["fetch"]`, `bundle_modified: false`.
- Evidence: `evidence/change-detection-output.txt`, EXPERIMENT 4b section
  (per-file unified diffs + VERDICTS JSON)

**Experiment 4a — same-topic claims (designed behavior when merged content
itself changes):**
- Original hash: `c1785da74bc8e268…` — New hash: `fdd87438f59dc08a…` →
  changed; run 2 unchanged → stopped after fetch (`run2_bundle_unchanged:
  true`, byte-snapshot compared); run 3 re-ran all stages.
- All three fixture sentences shared one topic hint, so the merger combined
  them into a single fact whose joined content changed → the publisher
  created a **new content-addressed document** (`kb-6abb34035fb8a4bd`, with
  the documented deterministic filename-collision suffix `-b8a4bd`) and left
  the old document untouched. No duplicate ids (`duplicate_ids: false`);
  both versions coexist as distinct facts.
- Evidence: `evidence/change-detection-output.txt`, EXPERIMENT 4a section

Proven overall: **UNCHANGED → SKIP** and **CHANGED → PROCESS → UPDATE**
(update in place when facts persist; new content-addressed version when the
merged fact's content itself changes; never a duplicate id).

## 5. Knowledge Bundle Integrity
Checked the real `knowledge/` directory with the project's own OKF validator
(`pipeline.okf.parse`):
- Valid documents: **50 / 50**
- Invalid documents: **0**
- Missing source URLs: **0** (51 source URL slots, all http(s), 4 unique
  URLs)
- Missing IDs: **0** (every document has a stable `kb-<16-hex>` id)
- Confidence/status fields valid: 50/50 (`confidence` ∈ low/medium/high,
  `status` ∈ official/community/inferred)
- Duplicate IDs: **0** · Duplicate titles: **0**
- INDEX consistency: **consistent both directions** — 50 rows ↔ 50 documents,
  no dangling links, no unlisted documents; INDEX "Last checked" column
  agrees with every document's frontmatter `last_checked`
- CHANGELOG consistency: **covers all 50 current document IDs** (50/50
  mentioned; 2 dated sections, latest 2026-09-27; 0 stale id mentions)
- Evidence: `evidence/file-integrity-output.txt` (checks [1]–[7] and final
  JSON; first run of the driver crashed on a bug in the driver itself,
  disclosed in the file header and fixed before this passing run)

## 6. Limitations / Failures
Nothing in the project failed. Limitations of what this evidence proves:

1. **Live-network end-to-end was not demonstrable in this window.** The
   `tvly` extraction service failed DNS resolution throughout the
   verification window (raw errors captured in `rerun-output.txt` Section A
   discussion and this transcript). The `direct` HTTP fallback returns HTML,
   which the pipeline correctly refuses to extract (shown live), and raw HTML
   differs byte-wise between back-to-back fetches (dynamic page elements), so
   it could not serve as a stable real source either. Phase 2 therefore
   replays real production internet content through the documented fetcher
   seam. Everything downstream of transport is real, but a from-scratch live
   `tvly` fetch was not re-demonstrated today.
2. **Environment artifact:** the nested `claude` CLI prints a stderr warning
   (`[claude-code:unrecognized_model] {"model":"claude-auto",...}`) because
   this verification session routes through a LiteLLM proxy; the pipeline
   parses stdout only, and every LLM call succeeded (exit 0).
3. **De-dup scope:** semantic dedup is demonstrated for facts that share a
   topic hint (the system's designed grouping boundary). Cross-hint
   near-duplicates are deliberately not merged by design; that boundary
   behavior was not adversarially probed here.
4. **LLM nondeterminism:** extraction/classification used the real model; a
   different run could assign different topic hints (4a vs 4b show both
   outcomes). All verdicts above describe the captured runs, whose full
   outputs are in the evidence files.
5. **Driver-side notes (not project defects):** 4b's `docs_unchanged` field
   was computed tautologically due to a driver bug and is NOT used as
   evidence (run-2 non-modification is evidenced by the report's
   `knowledge_bundle_modified: false` / `stages_executed: ["fetch"]` and by
   the properly computed byte-snapshots in 4a and Phase 2); the Phase 5
   driver crash described above; the 4b header decoration line failed a zsh
   glob (cosmetic, output intact).
6. **Not exercised in this pass:** conflicting-fact resolution and
   community-source validation paths live only in the unit suite (they pass
   there); Scout stage; multi-hundred-page scale; publisher failure
   mid-batch (covered by unit tests only).

## 7. Conclusion
Factually, on 2026-09-27, against commit-clean `main` with no modifications
to implementation or tests: the complete 192-test suite passes offline in
0.151 s; the safe-to-re-run contract holds under a live-real-stage
experiment (unchanged source → stop immediately after fetch, zero stages
after, knowledge tree byte-identical; re-ingestion creates no duplicates);
cross-source semantic de-duplication works on the demonstrated case with the
real claude classifier returning `duplicate` and the deterministic merger
collapsing two differently-worded facts into one two-source document; change
detection distinguishes unchanged (skip) from changed (full reprocessing),
updating persistent documents in place and creating a new content-addressed
document only for genuinely new/changed fact content, never a duplicate id;
and the production 50-document bundle is internally consistent (frontmatter,
ids, sources, INDEX, CHANGELOG). What this evidence does NOT establish:
end-to-end operation over the live network during this particular window
(tvly outage), behavior at large scale, or adversarial/conflicting-source
scenarios beyond the unit suite. No claim of general production readiness is
made beyond these observed results.

---
Evidence files (each embeds its driver script source and full raw output):
- `evidence/test-suite-output.txt`
- `evidence/rerun-output.txt`
- `evidence/deduplication-output.txt`
- `evidence/change-detection-output.txt`
- `evidence/file-integrity-output.txt`
