# Design: Amazon Ads Knowledge Acquisition System

Written 2026-09-30 against the code as it is (`pipeline/`, `tests/`,
`.claude/`), not as it might be. Sections: architecture, the code-vs-Claude
line, change detection, merge & conflict policy, trade-offs, limits, what
I'd improve, how Claude Code was used, and two retrospective sections.

## 1. Architecture and data flow

One deterministic driver sequences eight stages (Discover optional); every
stage is a Python module under `pipeline/`, every LLM judgment is an
isolated one-shot seam.

```
discover (optional)   python3 -m pipeline.orchestrate --discover URL...
   |  links from cached seed pages (+ tvly search behind TAVILY_API_KEY);
   |  host allowlist; drop URLs already in fetch state; cap 10
   v
fetch       pipeline/fetch.py — tvly extract (basic, advanced) -> direct
   |        HTTP; HTML converts to Markdown (stdlib html.parser) when it
   |        has >= 40 chars of visible text, else honestly stops
   |        verdict per URL: new | unchanged | changed | error
   |        unchanged -> STOP that URL here; nothing downstream runs
   v
extract     pipeline/extractor.py — claude -p --agent extractor:
   |        page Markdown -> discrete claims, each with a verbatim quote;
   |        one claims file per content version: state/claims/<sha>.json
   v
relevance gate  pipeline/relevance.py — drop-tokens / allow-list keywords
   |        first; ONE cached claude yes/no for borderline claims;
   |        drops logged to state/dropped.json (idempotent)
   v
adapter     pipeline/adapter.py — claims + fetch state -> Validator facts
   |        (source typing, change derivation, provenance passthrough)
   v
validate    pipeline/validator.py — fixed trust arithmetic; valid /
   |        valid_low_confidence / rejected; nothing dropped silently
   v
merge       pipeline/topics.py + concepts.py + merger.py — topic identity
   |        first (deterministic keyword phrases; ONE claude topic choice
   |        for ties), then within-topic pair classification
   |        (deterministic bands; claude --agent merge-judge only for
   |        undecided plausible pairs, capped), then conflict precedence
   v
publish     pipeline/publisher.py — one OKF document per topic concept,
            INDEX + CHANGELOG, atomic staged batch; only then does the
            orchestrator commit the fetch state
```

The knowledge bundle is a *participant*, not an output only: Merge matches
every new fact against the live `knowledge/` documents, so a changed value
(MIT-0 → Apache-2.0) updates the concept it already belongs to, with the
loser retained as a dated conflict entry.

## 2. The code-vs-Claude line (and why)

Claude does exactly five kinds of fuzzy judgment, each behind a mockable
one-shot seam, each returning ONLY JSON:

| Seam | Module | Question it answers | Guardrails |
|---|---|---|---|
| extract | `extractor.py` → agent `extractor` | what does this page claim? | quote must appear verbatim; whole extraction discarded otherwise |
| gate | `relevance.py` | is this claim about Amazon Ads? | keyword lists decide most claims; verdicts cached; fail-open |
| choose topic | `topics.py` → agent `topic-router` | which taxonomy topic? | deterministic phrase scoring first; must pick a listed slug |
| classify pair | `merger.py` → agent `merge-judge` | duplicate / conflicting / complementary? | deterministic bands first; same-extraction pairs skipped; ≤ 60 calls per concept |
| match concept | `concepts.py` | same concept as this document? | ≥ 0.8 token overlap merges with NO call; a "no" is asked twice (preview, then full fact list) before it counts |

Everything else — identities, scores, winners, file writes — is Python:
deterministic, offline-testable, reproducible. Claude never decides a
winner, a confidence number, a document id, or a write. The division is
enforced by construction: the LLM functions are pure `(input) -> JSON` and
every caller validates their shape; a seam failure degrades to keeping
facts separate (never to inventing structure).

Why this line: identity and scoring must be *auditable* — the same claims
must produce the same bundle every run — while "is this sentence about
Sponsored Display?" is exactly what language models are for and what
keyword code gets wrong.

## 3. Change detection: committed vs pending hash

`state/fetch_state.json` stores, per URL, the last *committed* content
hash and — after a fetch of new/changed content — a *pending* hash. The
verdict compares only against the committed hash, so a run that fails
anywhere downstream leaves the source looking unprocessed; the next run
retries it. The orchestrator promotes pending → committed **only after the
publisher succeeded** (`commit_state` in `pipeline/state.py`, called at the
end of `orchestrate()`). Unchanged content short-circuits at Fetch: the
extractor is never invoked, the bundle is byte-identical.

## 4. Merge and conflict policy

- **Identity**: the topic taxonomy fixes concept ids (`pipeline/topics.py`,
  15 topics + declared sub-topics). Slugs come from topic names, never from
  claim wording; a reworded extraction routes to the same topic and updates
  the same document.
- **Catch-all cap**: a concept with more than 12 facts splits by its
  declared sub-topics (taxonomy data, never sentence structure).
- **Pairs**: deterministic first — ≥ 0.8 token overlap is a duplicate; a
  numeric flip at ≥ 0.5 is a conflict (a value changed); a negation flip
  conflicts only when the claims are otherwise identical. Audience-split
  facts ("a guide for sellers" vs "a separate guide for advertisers that
  do not sell on Amazon") are complementary by rule and by prompt.
- **Precedence**: authority (official > community) > recency > majority,
  in pure Python. The loser is retained with full provenance and
  `superseded_by`; a complete tie keeps both facts, stamped
  `unresolved_conflict`.
- **Conflicts are never erased** — superseded values stay in the document's
  `### Conflicts` section with dates and sources.

## 5. Trade-offs

- **Topic taxonomy is hand-curated data.** Routing quality is exactly as
  good as the keyword phrases; ambiguous claims cost one LLM call each.
  The alternative (pure LLM clustering) would not be reproducible.
- **The 60-call pair cap** means a very large topic's tail pairs coexist
  unclassified rather than stalling the run. Deterministic order keeps
  runs reproducible; some duplicate detection is deferred to a later run.
- **Fail-open gate**: if the relevance seam is down, borderline claims are
  kept (they can be dropped by a later healthy run). Losing knowledge
  silently is worse than briefly keeping an off-topic claim.
- **Two-phase fetch state** doubles the state bookkeeping in exchange for
  crash-safe retry semantics.
- **Stdlib-only** (html.parser converter, no pip packages) keeps the
  pipeline runnable anywhere; the HTML converter is best-effort and a
  JS-shell page still stops honestly at Fetch (tvly must serve it).

## 6. Known limitations

- Sources without JS rendering and without tvly access cannot be ingested
  (the direct+HTML path only converts server-rendered pages).
- Discovery reads only already-cached seed pages (plus optional tvly
  search); it does not crawl.
- The catch-all cap only splits topics that DECLARE sub-topics. TWO
  shipped topics declare none and currently hold 13 facts each — one over
  the 12-fact cap: `api-access-and-onboarding` and
  `github-repos-and-sdks`. `split_over_cap` can only log a warning for
  them. Splitting (or re-capping) them is a pending taxonomy decision,
  not a code fix.
- `community_agree_count` is always 0 — no stage records people-agreement
  data yet; the scoring supports it when one does.
- The validator's similarity bands are honest heuristics, not
  understanding; ties in keyword routing rely on one LLM call.
- Every numeric decision constant in `pipeline/` is value-pinned: the
  mutation check in `evidence/threshold-mutation-check.txt` mutates all
  27 constants one step in each direction (54 mutations) and every one
  turns the suite red.
- The release-notes sources were attempted but NOT ingested: the
  release-notes index page (a large page — the run log records it at
  roughly 707 KB, and its converted copy was not retained in
  `state/cache/`) and the ad-api RSS feed (70 KB converted markdown;
  the conversion is shown in `evidence/html-conversion.txt`, the cached
  HTML is 65,590 bytes) both fetched and converted fine, but the
  extractor seam timed out at 300 s on each — both timeouts are recorded
  verbatim in `evidence/third-source-run.txt`. Neither ever published.
  The raw GitHub README is the third source kind actually shipped.
  Ingesting those pages needs chunked extraction or a longer seam
  timeout — both unplanned.
- The gate verdict cache (`state/gate_cache.json`) is not yet complete:
  it holds 13 cached verdicts, but 9 borderline claims recorded in
  `state/claims/` are not cached, so a fresh-clone rebuild still needs
  live gate calls for those 9 — gate determinism across runs is not yet
  fully offline-replayable.
- Historical claims files accumulate forever in `state/claims/` (one per
  content version) — intentional provenance, but unbounded.

## 7. What I'd improve next

1. Cache pair-classification verdicts by (sha_a, sha_b) in state so
   re-merges replay without LLM calls, like the gate cache already does.
2. Teach the publisher to emit a machine-readable diff (facts added /
   superseded) alongside CHANGELOG for programmatic review.
3. Add a `--dry-run` to orchestrate that stops before publish and prints
   the would-be writes.
4. Promote the remaining two single-fact topics when their sources grow —
   blocked for `api-release-notes` until release-notes ingestion works
   (see the limitation above: both release-notes pages currently exceed
   the extractor seam's 300 s timeout).

## 8. How Claude Code was used

- **Agents** (`.claude/agents/`, read-only tools, JSON-only output):
  `extractor`, `merge-judge`, `topic-router` — the three judgment seams the
  Python stages pin via `claude -p --agent <name>`. The gate and
  concept-match seams are single yes/no prompts without agent files.
- **Skills** (`.claude/skills/`): `ingest-update-bundle` (the user-facing
  command definition) plus nine stage reference guides (discover, fetcher,
  extractor, relevance-gate, adapter, validator, merger, publisher,
  orchestrate) and a validation-rules reference — eleven loadable SKILL.md
  folders.
- **Hook**: PreToolUse on Write|Edit runs `scripts/lint_bundle.py
  --pretooluse`, which applies the proposed write to a copy of `knowledge/`
  and blocks it if the copy fails the OKF/concept lint (frontmatter, type,
  id == filename, sources, link resolution, duplicate id/title). The plain
  mode lints the whole bundle including INDEX consistency.
- **Headless orchestration**: one command (`python3 -m pipeline.orchestrate`)
  drives a full run; Claude Code (or a human) only supplies URLs and relays
  the JSON report.

## 9. What surprised me

- The 37-document bundle was not an extraction problem but an *identity*
  problem: sentence-derived ids made every rewording a new document. One
  fixed topic taxonomy collapsed it to 15 documents without losing a fact —
  the same 76 facts, redistributed (the bundle has since grown to 80 facts
  as the raw GitHub source merged in).
- The biggest false conflict came from my own deterministic tripwire: a
  negation inside an audience phrase ("advertisers that do not sell on
  Amazon") looked exactly like "X vs not X" to a token comparison.
  Tightening the *deterministic* rule fixed what prompting alone could not.
- The migration stalled for 20+ minutes on O(N²) pair calls inside one
  30-fact topic — the seam was "working perfectly". Capping judgment calls
  per concept and skipping pairs from the same extraction (distinct by
  construction) cut the run to minutes. Bounding fuzzy work is a design
  requirement, not an optimization.
- A single LLM "no" was enough to mint a duplicate document. The fix was
  to make "no" expensive: ask twice, the second time with the full fact
  list.

## 10. What I learned

- Deterministic-first design makes LLM failures cheap: every seam can
  fail and the pipeline degrades to "facts stay separate", never to
  corruption. That property survived every refactor.
- Idempotency needs to be *total*: not just skip-if-unchanged, but
  byte-identical INDEX, first-seen dates in the drop log, cached gate
  verdicts — anywhere time or LLM variance leaks in, re-runs churn.
- Tests that fake the seams catch logic bugs but not *cost* bugs; the
  hermetic suite passed while the real run stalled. The production run is
  a test too — evidence files in `evidence/` are how it stays honest.
- Writing the taxonomy as data (not code) made "adjust to what the sources
  actually cover" a 20-line edit instead of a refactor.
