# Amazon Ads Knowledge Acquisition System

This project automatically turns information from Amazon Ads sources into a
clean, searchable knowledge base. It checks whether a source changed, uses
Claude (an AI model) where human-like interpretation is needed, applies
fixed rules where precision matters, and publishes the result as readable
Markdown concept documents.

No coding knowledge is needed to use it — you give it a web address, it
does the rest.

## The Problem

Amazon Ads documentation, APIs, announcements, and code repositories change
over time. Checking them by hand is repetitive: open the page, see if it
changed, re-read it, update your notes. This system automates all of that —
and, importantly, it does the boring parts with plain code and only spends
AI effort where judgment is genuinely needed.

## How It Works

Main flow (what runs today):

```
User ("ingest <url>, update the bundle")
        ↓
Claude Orchestrator
        ↓
Fetch      — download the page, fingerprint it, detect change
        ↓
Extract    — Claude lists the factual claims the page makes
        ↓
Adapter    — plain-code reshaping into Validator facts
        ↓
Validate   — fixed trust rules: valid / uncertain / rejected
        ↓
Merge      — new facts are matched against the EXISTING knowledge base
             and folded into concepts; Claude classifies relationships,
             rules resolve conflicts (losers kept, dated, attributed)
        ↓
Publish    — write readable concept documents, INDEX, and CHANGELOG
        ↓
Knowledge Base (the knowledge/ folder)
```

Optional path (available, **not** wired into the automatic flow):

```
User gives a topic
        ↓
Scout (finds candidate source URLs)
        ↓
Candidate sources (the user then passes them to the command above)
```

Scout is available for source discovery, but the current production flow
starts when the user provides URLs.

## The Most Important Design Decision

**Let code handle rules. Let Claude handle ambiguity.**

Some steps must be exact every single time (downloading, fingerprinting,
scoring, writing files). Other steps need language understanding (what does
this page say? are these two statements the same claim?). The system splits
the work accordingly:

| Part                             | Who handles it | Why                    |
| -------------------------------- | -------------- | ---------------------- |
| Fetching                         | Python         | predictable            |
| Hash/change detection            | Python         | exact                  |
| Claim extraction                 | Claude         | language understanding |
| Claim transformation             | Python         | predictable            |
| Validation                       | Python         | fixed rules            |
| Concept candidate filtering      | Python         | cheap, deterministic   |
| Ambiguous concept matches        | Claude         | semantic judgment      |
| Fact relationship classification | Claude         | semantic judgment      |
| Merge policy & conflict winners  | Python         | controlled rules       |
| Publishing                       | Python         | exact files/state      |

A *hash* is a fingerprint of downloaded content — if the fingerprint stays
the same, the content has not changed. *Deterministic* means given the same
input, the code follows the same rules and produces the same result, every
time.

## Prerequisites

Install these first, from a fresh clone of this repository:

1. **Python 3.11 or newer** (the pipeline uses only the standard library —
   no pip packages are needed; `requirements.txt` documents this).

   ```bash
   python3 --version      # must report 3.11+
   pip3 install -r requirements.txt   # no-op; verifies stdlib-only claim
   ```

2. **Node.js 18 or newer** — needed only to install the two CLI tools
   below.

   ```bash
   node --version         # must report v18+
   ```

3. **Claude Code CLI** (`claude`) — runs the three AI seams (claim
   extraction, fact-pair classification, concept matching). Install per the
   official docs (https://claude.com/claude-code) and sign in once with
   `claude` so headless `claude -p` calls work:

   ```bash
   claude --version
   ```

4. **Tavily CLI** (`tvly`) + API key — web search/extraction used by Fetch
   and by the optional Scout agent. Install the CLI (see
   https://tavily.com for the package and your key) and export your key in
   your shell profile:

   ```bash
   export TAVILY_API_KEY="your-key-here"   # never commit a real key
   tvly --version
   ```

   Tavily is optional at runtime — Fetch falls back to a plain HTTP
   download when `tvly` is unavailable — but Amazon's documentation pages
   are JavaScript-rendered and in practice need Tavily's extractor to
   produce readable Markdown.

Keep real API keys out of the repository: they belong in your shell
environment only (`.claude/settings.local.json`, which is gitignored, is
for local editor permissions, not secrets).

## How To Run It

One command, run from the project folder, replacing `<url>` with a real web
address:

```bash
claude -p "ingest https://advertising.amazon.com/about-api, update the bundle"
```

Several URLs at once also work — the system processes each independently:

```bash
claude -p "ingest <url1>, <url2>, <url3>, update the bundle"
```

In plain English, that command does this: Claude reads your request, hands
the URLs to the pipeline driver (`python3 -m pipeline.orchestrate`), which
downloads each page, fingerprints it, and — only if the content is new or
changed — extracts its factual claims, scores them, folds them into the
existing concept documents, and publishes the updated knowledge base. It
then reports, per URL: whether the page was new, changed, or unchanged; how
many claims were extracted; validation results; merge results; and what (if
anything) was published. If something fails — a page cannot be downloaded
or only arrives as unreadable code — that URL is reported honestly and
skipped; nothing is invented.

## What Happens When I Run It Again?

This is the system's most important everyday behavior.

First run (new or changed page):

```
URL → Fetch → content is new/changed → Extract → Adapter → Validate
    → Merge (against the existing concepts) → Publish → bundle updated
```

Second run, nothing changed on the page:

```
URL → Fetch → same fingerprint → unchanged → STOP
```

So on the second run: Claude extraction is skipped, all later stages are
skipped, no unnecessary processing happens, and **no duplicate knowledge
document is created**. If the page *did* change, only the difference flows
through, and the existing concept documents are updated in place rather
than duplicated — a changed value (say, a license that went from MIT-0 to
Apache-2.0) becomes the concept's current fact while the old value is kept
as a dated, attributed conflict entry.

## Knowledge Base

The `knowledge/` folder is the finished product:

* one readable Markdown file per CONCEPT (e.g. `api-access.md`,
  `amazon-marketing-stream.md`), the file name being the concept's stable
  identity
* each concept holds the facts that describe it, word-for-word, each with
  its own provenance (sources, fetch dates, trust score, merge outcome)
* `INDEX.md` — a table of contents listing every concept
* `CHANGELOG.md` — a dated history of everything created or updated

A small example:

```markdown
---
id: api-access                          ← stable concept identity
title: API Access
type: concept                           ← required by the OKF format
sources:
  - https://advertising.amazon.com/...
confidence: high
status: official
last_checked: 2026-09-27
---

# API Access

## Details

### Facts

- Application approval may take 1 business day.
  - confidence_score: 0.75
  - status: valid
  - resolution: duplicate_merged
  - first_seen: 2026-09-26
  - sources: https://advertising.amazon.com/... (official, fetched 2026-09-27T...)

## Sources
- https://advertising.amazon.com/... — official, fetched ... (confirmed 1 fact)
```

## Project Structure

```
.
├── .claude/
│   ├── agents/      — runnable Claude Code agent definitions
│   │                  (only Scout lives here; it is optional)
│   ├── skills/      — reference docs & instructions: stage guides + the
│   │                  "ingest …, update the bundle" command definition
│   └── settings.json— which commands Claude may run without asking
├── pipeline/        — the Python code for every stage (stdlib only)
├── tests/           — automated checks that nothing broke
├── knowledge/       — the finished knowledge base (concepts + INDEX + CHANGELOG)
├── state/           — the system's memory: fingerprints, cached pages, claims
├── requirements.txt — documents that the pipeline needs no pip packages
├── CLAUDE.md        — the engineering rules of the project (for contributors)
└── README.md        — this file
```

Note the distinction inside `.claude/`: `agents/` holds *runnable* agent
definitions (today only Scout, which is not part of the automatic flow),
while `skills/` holds *reference documentation* like the stage guides, plus
the skill that powers the `ingest <url>, update the bundle` command.

## About CLAUDE.md

`CLAUDE.md` is the rulebook of this project. Claude Code reads it
automatically at the start of every session, so the same ground rules apply
to every automated run and every human contribution without anyone having
to repeat them.

It is written as binding engineering rules, not suggestions. It covers:

* **Purpose** — the mission: continuously discover, extract, validate,
  merge, and publish Amazon Ads knowledge into the `knowledge/` folder.
  It is a system meant to be re-run, not a one-shot scrape.
* **The pipeline** — the six stages (Fetch, Extract, Adapter, Validate,
  Merge, Publish) and how each stage hands structured data to the next.
* **The document format** — exactly what every knowledge document must look
  like: Markdown with frontmatter carrying its stable concept `id`,
  `type`, `sources`, `confidence`, `status`, and `last_checked` date.
* **The safe-to-re-run contract** — the system's hardest requirement:
  running the same source twice must never create a duplicate. Unchanged
  content is skipped entirely, changed content is folded into the existing
  concept in place, and every concept lives in exactly one document.
* **Division of labor** — code does the exact, repeatable work (fetching,
  hashing, scoring, writing files); Claude does only the judgment work
  (reading pages, classifying relationships, matching concepts).
* **Hard requirements** — no fact without a traceable source URL, no
  invented certainty (uncertain facts must be marked `confidence: low` and
  say why), and a priority order for when time is short: valid output
  first, then safe-to-re-run, then working source types, then full
  provenance tracking.

For humans, it doubles as the contributor handbook: if you change how the
pipeline behaves, CLAUDE.md is the contract your change has to keep.

## Reliability / Testing

Two kinds of evidence back this system:

* **Automated tests.** They cover every deterministic stage: change
  detection (including the two-phase fetch-state commit), trust scoring
  with pinned threshold-boundary tests, concept matching and identity,
  merge policy (including conflict retention and unresolved ties),
  publishing idempotency and atomicity, OKF conformance of the whole
  bundle, and the orchestration sequencing. They run offline in under a
  second with `python3 -m unittest discover -s tests`.
* **Real-world demonstration runs.** The full pipeline has been exercised
  against real Amazon Ads documentation pages, the Amazon GitHub
  organization, and a raw GitHub source (see `evidence/`), with the real
  Claude model doing extraction and classification (no mocked data in
  those runs).

## Real-World Proof

Demonstrated on real sources so far:

* multiple real sources of genuinely different types processed end-to-end
  (official documentation page, GitHub organization page, raw GitHub file)
* genuinely changed sources re-extracted and folded into the SAME concept
  documents, with the old values retained as dated conflicts
* unchanged sources correctly short-circuited (the AI step provably never
  ran; the bundle stayed byte-identical)
* duplicate prevention — re-running never creates copies; reworded claims
  resolve to the same concept
* readable concept documents with a valid INDEX and CHANGELOG
* end-to-end publishing through the single user command

## If You Are New To This Project

Recommended reading order:

1. **README.md** (this file) — the big picture
2. **CLAUDE.md** — the engineering ground rules and pipeline contract
3. **.claude/skills/** — plain-English guides to what each part does
   (`.claude/agents/` holds the runnable Scout definition)
4. **pipeline/** — the actual code, one file per stage
5. **tests/** — what "correct" means for each stage
6. **knowledge/** — the finished output, best read via INDEX.md

Each step adds detail to the same story told by the previous one.

## Important Terms

* **Agent** — a helper with one specific job. Only Scout is a runnable
  Claude Code agent in this project; the pipeline stages are code.
* **LLM / Claude** — the AI model used to make semantic (fuzzy, judgment)
  decisions, like understanding what a page says.
* **Hash** — a fingerprint of content. Same fingerprint means the content
  is unchanged; any change produces a different fingerprint.
* **Claim** — one factual statement extracted from a page, with the exact
  supporting words quoted from it.
* **Fact** — a claim after reshaping, carrying its source information and
  ready to be scored.
* **Concept** — one topic of knowledge (e.g. "API access"); the unit the
  knowledge base is organized by. Many sources contribute facts to one
  concept.
* **Validator** — the stage that scores trust with fixed arithmetic and
  stamps facts valid, uncertain, or rejected.
* **Merger** — the stage that folds new facts into concepts, resolving
  conflicts by fixed priority rules and keeping the losers on record.
* **Knowledge base** — the `knowledge/` folder of readable Markdown
  concept documents this system produces.
* **Idempotent** — running the same update again does not create another
  copy; if nothing changed, nothing is written.
