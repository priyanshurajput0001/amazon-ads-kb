---
name: fetch-update-bundle
description: Run the knowledge pipeline for one or more URLs. Use when the user says "fetch <url>, update the bundle" or asks to fetch URL(s) and update/publish the Amazon Ads knowledge bundle. Runs Fetch → Extract → Adapter → Validator → Merger → Publisher via one driver command, with unchanged sources stopped right after Fetch.
---

# Fetch URL(s) and update the knowledge bundle

You are the orchestrator. All pipeline logic lives in deterministic Python
under `pipeline/` — do NOT reimplement stage logic, do NOT run stages
manually one by one, and do NOT edit `knowledge/` or `state/` yourself (the
Publisher owns `knowledge/`). Run the single driver command and relay its
report.

## Steps

1. Parse every `http(s)://…` URL from the user's request. If there is none,
   ask for one and stop.
2. Run exactly one command:

   ```bash
   python3 -m pipeline.orchestrate URL [URL ...]
   ```

   The driver fetches with persistent change detection and per-URL:
   - `unchanged` → stops right after Fetch (the Extractor is never invoked);
   - failed fetch / HTML-only cache → stops and reports the reason;
   - `new`/`changed` markdown → Extract → Adapter → Validator → Merger →
     Publisher. Extraction and pair classification use the real `claude` CLI
     internally and can take several minutes for changed sources — just wait.

3. Read the JSON report and summarize it for the user, quoting its numbers:
   - per URL: fetch verdict (`new`/`changed`/`unchanged`/`error`), the stages
     that ran for it, and if it stopped early, `stopped_at` + `error`;
   - totals: extracted claim counts, adapter fact count, validation counts
     (valid / valid_low_confidence / rejected), merge input → output,
     publication counts (published / updated / unchanged), and whether the
     knowledge bundle was modified.

## Failure rules

- Report failures exactly as the driver reports them — name the stage and the
  error. HTML-only caches and failed fetches are real outcomes; never
  fabricate Markdown, facts, or publications.
- One URL failing never blocks the others; say which URLs succeeded.
- If `stage_failed` appears (validator/merger/publisher), the knowledge bundle
  was NOT modified — say so explicitly.
