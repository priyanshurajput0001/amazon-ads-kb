# Amazon Ads Knowledge Acquisition System

## Purpose
Continuously discover, extract, validate, merge, and publish knowledge about
Amazon Ads (APIs, features, docs, guides, MCP servers) into a single,
trustworthy Open Knowledge Format (OKF) bundle under `knowledge/`.

This is NOT a one-shot scrape. It is a system that must be **safe to re-run**:
running it twice must not duplicate content — only real changes should be
applied.

## Pipeline
0. **Discover** (optional first stage, `--discover`) — read links from the
   cached pages of the seed URLs (plus a `tvly search` when
   TAVILY_API_KEY is set), keep only in-scope hosts, drop URLs already in
   fetch state, and hand the capped candidate list to Fetch. Without the
   flag the run is exactly the user-provided URLs.
1. **Fetch** — download each source, fingerprint it, detect change
   (new / changed / unchanged). Unchanged sources stop here; nothing
   downstream runs. A new hash is staged as *pending* and committed only
   after publication succeeds, so a failed run is always retried. HTML
   pages convert to Markdown (stdlib html.parser) when they carry real
   text; a JS shell keeps the honest `html` verdict and stops.
2. **Extract** — pull discrete factual claims out of each source. Every claim
   must carry a source URL, a fetch timestamp, and a verbatim supporting
   quote.
3. **Relevance gate** — drop claims that are not about Amazon Ads /
   advertising APIs / seller advertising tooling: deterministic keyword
   lists first, ONE Claude yes/no only for borderline claims, the verdict
   cached in `state/gate_cache.json` keyed by the claim text and replayed
   on later runs (a shipped file the pipeline regenerates; it currently
   holds 13 verdicts while 9 recorded borderline claims are not yet
   cached, so a fresh-clone rebuild still needs live gate calls for
   those), every drop logged with its reason to `state/dropped.json`.
   Fail-open: a broken seam keeps the claim.
4. **Adapter** — deterministic reshaping of claims into Validator facts
   (source typing, dates, stability signals).
5. **Validate** — score each fact with fixed trust arithmetic; stamp it
   valid / valid_low_confidence / rejected. This scores the current batch
   only; comparison against the maintained bundle happens in Merge.
6. **Merge** — every fact first routes to a fixed TOPIC from the taxonomy
   (`pipeline/topics.py`): deterministic keyword-phrase routing, one
   Claude topic choice only for ties. The topic slug IS the concept id —
   never derived from claim wording. Within a topic, fact pairs are
   classified (deterministic bands first; the LLM seam only for
   undecided plausible pairs, capped per concept). One concept per topic:
   if N sources describe the same topic, they contribute facts and
   sources to ONE document — never duplicates. A topic over 12 facts
   splits by its declared sub-topics. Conflicts are resolved by
   fixed precedence (official > newer > majority) with the losing value
   retained, dated, and attributed — never silently erased; complete ties
   keep both sides, stamped `unresolved_conflict`.
7. **Publish** — write/update one OKF concept document per topic in
   `knowledge/`, update the index (`knowledge/INDEX.md`) and change log
   (`knowledge/CHANGELOG.md`) in the same atomic batch.

The pipeline starts with URLs the user provides (optionally extended by
Discover). Automated discovery reads only what was already fetched — it
never crawls.

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
- **Code (deterministic, testable)**: fetching pages/URLs, HTML→Markdown
  conversion, hashing content to detect real changes, file I/O, OKF
  frontmatter validation, relevance keyword lists, topic keyword-phrase
  routing, concept candidate filtering, conflict precedence, generating
  the index and changelog.
- **Claude (fuzzy judgment, isolated behind five mockable seams)**:
  extracting facts from raw text (`pipeline/extractor.py`), relevance
  yes/no for borderline claims (`pipeline/relevance.py`), choosing a
  topic for keyword-ambiguous claims (`pipeline/topics.py`), classifying
  fact-pair relationships (`pipeline/merger.py`), deciding ambiguous
  concept matches (`pipeline/concepts.py`). Claude never decides winners,
  scores, identities, or file writes.

## Agents, skills and the lint hook
- `.claude/agents/` defines three READ-ONLY judgment agents — `extractor`,
  `merge-judge`, `topic-router` — whose JSON-only contracts match the seam
  prompts. The Python stages pin them headlessly
  (`claude -p --agent <name>`); the relevance-gate and concept-match
  seams are plain one-shot prompts. Every seam is fakeable, so tests run
  fully offline.
- `.claude/skills/` holds eleven loadable SKILL.md folders: a reference
  guide for every stage (discover, fetcher, extractor, relevance-gate,
  adapter, validator, merger, publisher, orchestrate), a validation-rules
  reference, and the `ingest <url>, update the bundle` command
  definition.
- `.claude/settings.json` registers a PreToolUse hook (Write|Edit) that
  runs `scripts/lint_bundle.py --pretooluse`: a write into `knowledge/`
  is applied to a copy of the bundle and blocked (exit 2) if the copy
  fails the OKF/concept lint. Plain `python3 scripts/lint_bundle.py`
  lints the whole bundle, INDEX consistency included.

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
