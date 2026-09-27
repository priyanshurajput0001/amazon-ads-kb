# Amazon Ads Knowledge Acquisition System

This project automatically turns information from Amazon Ads sources into a
clean, searchable knowledge base. It checks whether a source changed, uses
Claude (an AI model) where human-like interpretation is needed, applies
fixed rules where precision matters, and publishes the result as readable
Markdown documents.

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
User ("fetch <url>, update the bundle")
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
Merge      — Claude classifies fact relationships, rules resolve them
        ↓
Publish    — write readable documents, INDEX, and CHANGELOG
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
| Fact relationship classification | Claude         | semantic judgment      |
| Merge policy                     | Python         | controlled rules       |
| Publishing                       | Python         | exact files/state      |

A *hash* is a fingerprint of downloaded content — if the fingerprint stays
the same, the content has not changed. *Deterministic* means given the same
input, the code follows the same rules and produces the same result, every
time.

## Meet the Stages

Plain-English guides to each part, including exactly where AI is and is not
used. The guides live in `.claude/skills/` — see the distinction below.

> **Two different things live under `.claude/`:**
>
> * `.claude/agents/` — **runnable Claude Code agent definitions.** This is
>   where actual executable agents are declared. The only one today is
>   `Discovery_Agent.md` — the real Scout agent (registered under the name
>   `scout`).
> * `.claude/skills/` — **reusable instructions and reference
>   documentation** for the pipeline. These files describe how the system
>   works; they are not runnable agents.

* [Scout](./.claude/skills/SCOUT.md) — finds candidate source URLs for a
  topic (available; not part of the automatic URL flow; its runnable
  definition is `.claude/agents/Discovery_Agent.md`)
* [Orchestrator](./.claude/skills/ORCHESTRATE.md) — the conductor that runs
  the stages in order when you say "fetch …, update the bundle"
* [Fetcher](./.claude/skills/FETCHER.md) — downloads pages and detects
  change
* [Extractor](./.claude/skills/EXTRACTOR.md) — Claude reads a page and
  lists its facts
* [Validator](./.claude/skills/VALIDATOR.md) — fixed trust rules
* [Merger](./.claude/skills/MERGER.md) — Claude classifies relationships,
  rules merge the facts
* [Publisher](./.claude/skills/PUBLISHER.md) — writes the knowledge base
* [Adapter](./.claude/skills/ADAPTER.md) — internal deterministic glue
  between Extractor and Validator (not an AI agent)

## How To Run It

One command, run from the project folder, replacing `<url>` with a real web
address:

```bash
claude -p "fetch https://advertising.amazon.com/about-api, update the bundle"
```

Several URLs at once also work — the system processes each independently:

```bash
claude -p "fetch <url1>, <url2>, <url3>, update the bundle"
```

The command runs the whole pipeline and reports, per URL: whether the page
was new, changed, or unchanged; how many facts were extracted; validation
results; merge results; and what (if anything) was published. If something
fails — a page cannot be downloaded or only arrives as unreadable code —
that URL is reported honestly and skipped; nothing is invented.

## What Happens When I Run It Again?

This is the system's most important everyday behavior.

First run (new or changed page):

```
URL → Fetch → content is new/changed → Extract → Adapter → Validate
    → Merge → Publish → knowledge base updated
```

Second run, nothing changed on the page:

```
URL → Fetch → same fingerprint → unchanged → STOP
```

So on the second run: Claude extraction is skipped, all later stages are
skipped, no unnecessary processing happens, and **no duplicate knowledge
document is created**. If the page *did* change, only the difference flows
through, and existing documents are updated in place rather than duplicated.

## Knowledge Base

The `knowledge/` folder is the finished product:

* one readable Markdown file per piece of knowledge, with a human-friendly
  file name (e.g. `amazon-ads-mcp-server-open-beta.md`)
* `INDEX.md` — a table of contents listing every document
* `CHANGELOG.md` — a dated history of everything created or updated

Each document records the fact word-for-word, its sources with fetch dates,
a trust score, and how it was merged. A small example:

```markdown
---
id: kb-9b266759b1a5212f          ← stable identity, never changes
title: The Amazon Ads MCP server is in open beta.
sources:
  - https://advertising.amazon.com/API/docs/en-us
confidence: high
status: official
last_checked: 2026-09-27
---

## Details

The Amazon Ads MCP server is in open beta.

- confidence_score: 0.60
- resolution: single_source
- status: valid

## Sources
- https://advertising.amazon.com/API/docs/en-us — official, fetched 2026-09-27
```

## Project Structure

```
.
├── .claude/
│   ├── agents/      — runnable Claude Code agent definitions (Scout lives here)
│   ├── skills/      — reference docs & instructions: stage guides + the
│   │                   "fetch …, update the bundle" command the orchestrator uses
│   └── settings.json— which commands Claude may run without asking
├── pipeline/        — the Python code for every stage
├── tests/           — automated checks (192 of them) that nothing broke
├── knowledge/       — the finished knowledge base (documents + INDEX + CHANGELOG)
├── state/           — the system's memory: fingerprints, cached pages, claims
├── CLAUDE.md        — the engineering rules of the project (for contributors)
└── README.md        — this file
```

Note the distinction inside `.claude/`: `agents/` holds *runnable* agent
definitions (today only `Discovery_Agent.md`, the Scout), while `skills/`
holds *reference documentation* like the stage guides linked above, plus
the skill that powers the `fetch <url>, update the bundle` command.

## About CLAUDE.md

`CLAUDE.md` is the rulebook of this project. Claude Code reads it
automatically at the start of every session, so the same ground rules apply
to every automated run and every human contribution without anyone having
to repeat them.

It is written as binding engineering rules, not suggestions. It covers:

* **Purpose** — the mission: continuously discover, extract, validate,
  merge, and publish Amazon Ads knowledge into the `knowledge/` folder.
  It is a system meant to be re-run, not a one-shot scrape.
* **The pipeline** — the five stages (Discover, Extract, Validate, Merge,
  Publish) and how each stage hands structured data to the next.
* **The document format** — exactly what every knowledge document must look
  like: Markdown with frontmatter carrying its stable `id`, `sources`,
  `confidence`, `status`, and `last_checked` date.
* **The safe-to-re-run contract** — the system's hardest requirement:
  running the same source twice must never create a duplicate. Unchanged
  content is skipped entirely, changed content is updated in place, and
  every topic lives in exactly one document.
* **Division of labor** — code does the exact, repeatable work (fetching,
  hashing, scoring, writing files); Claude does only the judgment work
  (reading pages, classifying relationships, writing prose).
* **Hard requirements** — no fact without a traceable source URL, no
  invented certainty (uncertain facts must be marked `confidence: low` and
  say why), and a priority order for when time is short: valid output
  first, then safe-to-re-run, then working source types, then full
  provenance tracking.

For humans, it doubles as the contributor handbook: if you change how the
pipeline behaves, CLAUDE.md is the contract your change has to keep.

## Reliability / Testing

Two kinds of evidence back this system:

* **Automated tests — 192, all passing.** They cover every deterministic
  stage: change detection, trust scoring, merge policy, publishing
  idempotency, and the orchestration sequencing. They run offline in under
  a second with `python3 -m unittest discover -s tests`.
* **Real-world demonstration runs.** The full pipeline has been exercised
  against real Amazon Ads documentation pages and the Amazon GitHub
  organization, with the real Claude model doing extraction and fact-pair
  classification (no mocked data in those runs).

## Real-World Proof

Demonstrated on real sources so far:

* multiple real sources processed in one run
* genuinely changed sources re-extracted and re-published
* unchanged sources correctly short-circuited (the AI step provably never
  ran)
* duplicate prevention — re-running never creates copies
* readable knowledge documents (50 as of this writing) with a valid INDEX
  and CHANGELOG
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

* **Agent** — a helper with one specific job in the pipeline.
* **LLM / Claude** — the AI model used to make semantic (fuzzy, judgment)
  decisions, like understanding what a page says.
* **Hash** — a fingerprint of content. Same fingerprint means the content
  is unchanged; any change produces a different fingerprint.
* **Claim** — one factual statement extracted from a page, with the exact
  supporting words quoted from it.
* **Fact** — a claim after reshaping, carrying its source information and
  ready to be scored.
* **Validator** — the stage that scores trust with fixed arithmetic and
  stamps facts valid, uncertain, or rejected.
* **Merger** — the stage that combines related facts into one, resolving
  conflicts by fixed priority rules.
* **Knowledge base** — the `knowledge/` folder of readable Markdown
  documents this system produces.
* **Idempotent** — running the same update again does not create another
  copy; if nothing changed, nothing is written.
