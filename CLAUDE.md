# Amazon Ads Knowledge Acquisition System

## Purpose
Continuously discover, extract, validate, merge, and publish knowledge about
Amazon Ads (APIs, features, docs, guides, MCP servers) into a single,
trustworthy Open Knowledge Format (OKF) bundle under `knowledge/`.

This is NOT a one-shot scrape. It is a system that must be **safe to re-run**:
running it twice must not duplicate content — only real changes should be
applied.

## Pipeline
1. **Fetch** — download each source, fingerprint it, detect change
   (new / changed / unchanged). Unchanged sources stop here; nothing
   downstream runs. A new hash is staged as *pending* and committed only
   after publication succeeds, so a failed run is always retried.
2. **Extract** — pull discrete factual claims out of each source. Every claim
   must carry a source URL, a fetch timestamp, and a verbatim supporting
   quote.
3. **Adapter** — deterministic reshaping of claims into Validator facts
   (source typing, dates, stability signals).
4. **Validate** — score each fact with fixed trust arithmetic; stamp it
   valid / valid_low_confidence / rejected. This scores the current batch
   only; comparison against the maintained bundle happens in Merge.
5. **Merge** — fold new facts into CONCEPTS by matching them against the
   existing knowledge documents (deterministic candidate filtering first,
   the LLM seam only for ambiguous matches). One concept per topic: if N
   sources describe the same concept, they contribute facts and sources to
   ONE concept — never duplicate documents. Conflicts are resolved by
   fixed precedence (official > newer > majority) with the losing value
   retained, dated, and attributed — never silently erased; complete ties
   keep both sides, stamped `unresolved_conflict`.
6. **Publish** — write/update one OKF concept document per concept in
   `knowledge/`, update the index (`knowledge/INDEX.md`) and change log
   (`knowledge/CHANGELOG.md`) in the same atomic batch.

Discovery (finding candidate URLs for a topic) exists as the optional Scout
agent and is NOT wired into this flow — the pipeline starts at Fetch with
URLs the user provides.

## OKF document format
Every document in `knowledge/` is plain markdown with YAML frontmatter:

```yaml
---
id: sponsored-products-overview      # stable concept slug, never changes once assigned
title: Sponsored Products Overview
type: concept                        # required by OKF v0.1; the only type in this bundle
sources:
  - https://advertising.amazon.com/...
confidence: high                     # low | medium | high
status: official                     # official | community | inferred
last_checked: 2026-09-26             # ISO date, only updated when content changes
---
```

Body structure:
- `## Details` → `### Facts` — each fact verbatim, with per-fact provenance
  (confidence_score, status, resolution, first_seen, sources with fetch
  dates). A concept holds multiple complementary facts.
- `### Conflicts` — superseded facts, kept with their provenance and what
  superseded them (only present when a conflict occurred).
- `## Sources` — each source URL with type, fetch date, and what it
  confirmed.
- `## Related` — cross-links to other concepts, generated only from real
  evidence (shared source plus topical overlap), e.g.
  `[Sponsored Display](./sponsored-display.md)`.

**Identity rule**: the `id` is the concept's identity. It is assigned when
the concept is first coined and adopted thereafter via concept matching —
it must NEVER be derived from the final sentence wording (rewording a
source must update the same document, not create a new one).

## Safe-to-re-run contract (hard requirement)
- Before writing anything, the pipeline matches new facts against the
  existing bundle by concept identity (`id`) — never by guessing a filename
  and never by hashing the sentence wording.
- If the newly extracted content is unchanged from what's already recorded,
  **skip the write entirely** — do not touch the file, do not bump
  `last_checked`.
- If content changed, update the existing concept document in place and
  record what changed in `knowledge/CHANGELOG.md` (date, concept id, what
  changed, sources).
- Never create a second document for a concept that already has one. Always
  merge into the existing one — reworded extractions and changed values
  included (changed values become dated, attributed conflicts).

## Division of labor: code vs. Claude judgment
- **Code (deterministic, testable)**: fetching pages/URLs, hashing content to
  detect real changes, file I/O, OKF frontmatter validation, concept
  candidate filtering, conflict precedence, generating the index and
  changelog.
- **Claude (fuzzy judgment, isolated behind three mockable seams)**:
  extracting facts from raw text (`pipeline/extractor.py`), classifying
  fact-pair relationships (`pipeline/merger.py`), deciding ambiguous
  concept matches (`pipeline/concepts.py`). Claude never decides winners,
  scores, identities, or file writes.

## Subagents
`.claude/agents/` contains exactly ONE runnable Claude Code agent:
- `scout` (`Discovery_Agent.md`) — optional source discovery for a topic.
  It is NOT part of the production flow, which starts at Fetch with URLs
  the user provides via `claude -p "ingest <url>, update the bundle"`.

The pipeline stages themselves are Python modules under `pipeline/`, not
agents; their Claude-facing parts are the three LLM seams listed above,
each invoked as a one-shot `claude -p` subprocess and each fakeable in
tests. Stage documentation lives in `.claude/skills/`.

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
