---
name: ingest-update-bundle
description: Run the knowledge pipeline for one or more URLs. Use when the user says "ingest <url>, update the bundle" or asks to ingest URL(s) and update/publish the Amazon Ads knowledge bundle. Runs Fetch → Extract → Adapter → Validator → Merger → Publisher via one driver command, with unchanged sources stopped right after Fetch.
---

# Ingest URL(s) and update the knowledge bundle

You are the orchestrator. All pipeline logic lives in deterministic Python
under `pipeline/` — do NOT reimplement stage logic, do NOT run stages
manually one by one, and do NOT edit `knowledge/` or `state/` yourself (the
Publisher owns `knowledge/`). Run the single driver command and relay its
report.

## Steps

1. Parse every `http(s)://…` URL from the user's request. If there is none,
   ask for one and stop. (The driver can also do this for you with
   `--phrase "ingest <url>, update the bundle"`.)
2. Run exactly one command:

   ```bash
   python3 -m pipeline.orchestrate URL [URL ...]
   ```

   The driver fetches with persistent change detection and per-URL:
   - `unchanged` → stops right after Fetch (the Extractor is never invoked);
   - failed fetch / HTML-only cache → stops and reports the reason;
   - `new`/`changed` markdown → Extract → Adapter → Validator → Merger
     (matched against the existing knowledge bundle) → Publisher.
     Extraction, concept matching and pair classification use the real
     `claude` CLI internally and can take several minutes for changed
     sources — just wait.

   The pipeline stage that downloads pages is still named **Fetch**
   internally; only the user-facing command is `ingest`.

   The AI judgment inside that command runs as three read-only Claude Code
   agents (`.claude/agents/`), pinned per seam by the Python stages:

   - **extractor** — claim extraction per fetched page
     (`pipeline/extractor.py` -> `claude -p --agent extractor`);
   - **merge-judge** — duplicate/conflicting/complementary labels for fact
     pairs (`pipeline/merger.py`);
   - **topic-router** — topic choice for keyword-ambiguous claims
     (`pipeline/topics.py`).

   The relevance gate and the concept-match seam use one-shot prompts
   without an agent file (single yes/no questions). A PreToolUse hook
   (`.claude/settings.json` -> `scripts/lint_bundle.py --pretooluse`)
   blocks any Write/Edit that would leave `knowledge/` invalid.

3. Read the JSON report and summarize it for the user, quoting its numbers:
   - per URL: fetch verdict (`new`/`changed`/`unchanged`/`error`), the stages
     that ran for it, and if it stopped early, `stopped_at` + `error`;
   - totals: extracted claim counts, adapter fact count, validation counts
     (valid / valid_low_confidence / rejected), merge input facts → output
     concepts, publication counts (published / updated / unchanged), and
     whether the knowledge bundle was modified.

## Failure rules

- Report failures exactly as the driver reports them — name the stage and
  the error. HTML-only caches and failed fetches are real outcomes; never
  fabricate Markdown, facts, or publications.
- One URL failing never blocks the others; say which URLs succeeded.
- If `stage_failed` appears (validator/merger/publisher), the knowledge bundle
  was NOT modified — say so explicitly. The source's new content hash is
  also not committed in that case, so the next run retries it automatically.
