# Amazon Ads Knowledge Acquisition System

## Purpose
Continuously discover, extract, validate, merge, and publish knowledge about
Amazon Ads (APIs, features, docs, guides, MCP servers) into a single,
trustworthy Open Knowledge Format (OKF) bundle under `knowledge/`.

This is NOT a one-shot scrape. It is a system that must be **safe to re-run**:
running it twice must not duplicate content — only real changes should be
applied.

## Pipeline
1. **Discover** — find candidate sources (docs pages, blog posts, API
   changelogs, repos, MCP server listings) related to Amazon Ads.
2. **Extract** — pull discrete factual claims out of each source. Every claim
   must carry a source URL and a fetch/check timestamp.
3. **Validate** — compare each extracted fact against what's already in
   `knowledge/`. Determine: new fact, confirms existing fact, contradicts
   existing fact, or no-op (unchanged since last check).
4. **Merge** — one OKF document per concept/topic. If N sources describe the
   same concept, produce ONE document referencing all N sources — never
   duplicate documents for the same topic. On conflicts, prefer official
   Amazon sources and note the disagreement explicitly in the doc.
5. **Publish** — write/update OKF markdown documents in `knowledge/`, update
   the index (`knowledge/INDEX.md`) and change log (`knowledge/CHANGELOG.md`).

## OKF document format
Every document in `knowledge/` is plain markdown with YAML frontmatter:

```yaml
---
id: sponsored-products-overview      # stable slug, never changes once assigned
title: Sponsored Products Overview
sources:
  - https://advertising.amazon.com/...
confidence: high                     # low | medium | high
status: official                     # official | community | inferred
last_checked: 2026-09-26             # ISO date, only updated when content changes
---
```

Body structure:
- Concise concept explanation (what it is, why it matters).
- `## Details` — the actual facts, organized by subtopic if needed.
- `## Sources` — list each source URL with what it specifically confirmed.
- Cross-link related concepts with relative markdown links to other `id`s
  in `knowledge/`, e.g. `[Sponsored Display](./sponsored-display.md)`.

## Safe-to-re-run contract (hard requirement)
- Before writing anything, check whether a document already exists for that
  topic by its `id` — not by guessing a filename.
- If the newly extracted content is unchanged from what's already recorded,
  **skip the write entirely** — do not touch the file, do not bump
  `last_checked`.
- If content changed, update the existing document in place and record what
  changed in `knowledge/CHANGELOG.md` (date, doc id, what changed, source).
- Never create a second document for a topic that already has one. Always
  merge into the existing one.

## Division of labor: code vs. Claude judgment
- **Code (deterministic, testable)**: fetching pages/URLs, hashing content to
  detect real changes, file I/O, OKF frontmatter validation, generating the
  index and changelog.
- **Claude / subagents (fuzzy judgment)**: deciding what counts as one
  "concept" vs. several, extracting facts from raw text, resolving
  conflicting claims between sources, writing the final prose for each doc.

## Subagents
Defined under `.claude/agents/` — one per pipeline stage:
- `scout` — Discover
- `extractor` — Extract
- `validator` — Validate
- `merger` — Merge
- `publisher` — Publish

Each agent should hand off structured data (not prose) to the next stage
wherever possible, to keep the pipeline testable and re-runnable.

## Hard requirements
- No fact enters `knowledge/` without a traceable source URL.
- No unchecked or fabricated claims — if a fact is uncertain, mark
  `confidence: low` and say why in the doc; never omit the uncertainty or
  invent certainty.
- If time-constrained, prioritize in this order:
  1. Valid OKF output
  2. Safe-to-re-run (skip-if-unchanged, at minimum)
  3. 2-3 working source types end-to-end
  4. Full provenance tracking (confidence, multi-source confirmation)
