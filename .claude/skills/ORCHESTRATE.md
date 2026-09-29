# Orchestrator

> Reference documentation for the orchestration driver — not a runnable
> agent. The code lives in `pipeline/orchestrate.py`, the user-facing
> command definition in
> [`ingest-update-bundle/SKILL.md`](./ingest-update-bundle/SKILL.md).

## Purpose

The Orchestrator is the conductor of the whole pipeline. When the user says
`"ingest <url>, update the bundle"`, it runs the existing stages in the right
order and stops where stopping is correct — it contains **no pipeline logic
of its own**, only sequencing.

## When To Use

Refer to this document when explaining how the user command works, why an
unchanged URL stops after Fetch, how several URLs are handled
independently, or what the JSON report contains.

## Input

One or more URLs from the user, plus the pipeline's saved state
(`state/fetch_state.json` — the memory of what was fetched before).

## Process

1. **Fetch** all URLs through the existing Fetch stage (with change
   detection).
2. For each URL, route by the fetch verdict:
   * `unchanged` → **stop immediately** — the Extractor is never invoked,
     and no later stage runs for that URL
   * fetch failed, or the page is only unreadable HTML → **stop** and
     report the reason honestly
   * `new`/`changed` readable content → continue
3. **Extract** the claims from that URL's content (real Claude).
4. **Adapt** the claims into Validator facts (plain code).
5. **Validate** all adapted facts together (plain rules).
6. **Merge** new facts INTO the existing concepts (Claude classifies
   pairs and concept matches; rules resolve).
7. **Publish** the result into `knowledge/`.
8. Produce one JSON report covering every URL and stage.

## Rules

* Claude (the orchestrator assistant) parses the request and reports the
  result; this Python driver does the sequencing — no stage logic is
  duplicated inside it.
* Unchanged content NEVER reaches the Extractor — that is the main
  money-and-time saving.
* URLs are isolated: one URL failing never blocks the others.
* A failure in a shared stage (Validate/Merge/Publish) aborts the run
  BEFORE publishing, so a half-finished run can never corrupt the knowledge
  bundle.
* Fetch fingerprints are committed only AFTER publication succeeds, so a
  failed run is retried on the next one instead of being skipped.
* The user-facing command is `ingest`; the downloading stage is still
  named Fetch internally.
* Extraction and pair-classification use the real Claude model by default;
  tests inject fakes through the same seams.

## Output

One JSON report: per URL — fetch verdict, stages executed, where it stopped
and why; plus totals — facts, validation counts, merge input → output,
publication counts, and whether the knowledge bundle changed.

## Failure Behavior

* Fetch fails / HTML-only cache → URL stopped at Fetch, reason reported.
* Extract fails → URL stopped at Extract, nothing published, no fake facts.
* Validator/Merger/Publisher raises → run aborts, the report names the
  stage and error, and `knowledge/` stays untouched (the Publisher is
  atomic).

## Example

`claude -p "ingest https://advertising.amazon.com/about-api, update the
bundle"` on an unchanged page → report says: fetch `unchanged`,
`stopped_at: fetch`, stages `[fetch]`, knowledge bundle modified: `false`.
On a changed page → all six stages run and the report shows the new
documents published.

## Implementation

`pipeline/orchestrate.py` (`orchestrate`, `main`); the command definition in
[`.claude/skills/ingest-update-bundle/SKILL.md`](./ingest-update-bundle/SKILL.md);
the permission allow-list in `.claude/settings.json`. The driver accepts
plain `URL` arguments or the full user phrase via
`--phrase "ingest <url>, update the bundle"` (every `http(s)://…` URL is
parsed out of the phrase deterministically). Tests:
`tests/test_orchestrate.py`.
