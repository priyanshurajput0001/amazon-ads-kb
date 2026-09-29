# Extractor

> Reference documentation for the Extract stage — not a runnable agent. The
> code lives in `pipeline/extractor.py`.

## Purpose

The Extractor reads one downloaded page and pulls out the individual
factual statements it makes. Splitting a wall of text (mixed with menus and
boilerplate) into clean single facts requires language understanding — this
is one of the two places in the pipeline where Claude is used.

## When To Use

Refer to this document when explaining what a "claim" is, how facts are
born, what the `state/claims/` files contain, or why an extraction was
rejected.

## Input

The saved readable text (Markdown) of one page and its URL. The page must
already be cached by the Fetcher and its verdict must be `new` or
`changed` (unchanged pages never reach the Extractor).

## Process

1. Sends the page text to **Claude** with strict instructions: list only
   facts the page actually states.
2. Claude returns a list of **claims**, each with:
   * `claim` — the fact in one sentence
   * `quote` — the exact supporting words, copied word-for-word from the page
   * `topic_hint` — a suggested concept slug (advisory; the concept
     layer uses it as one matching signal)
   * `confidence` — how clearly the page states it (low/medium/high)
3. Deterministic Python then **verifies** the answer before saving: every
   field present, and every quote found verbatim in the saved page.
4. Verified claims are stored in `state/claims/` — one file per content
   version. If claims already exist for this exact version, Claude is not
   called again.

## Rules

* Claude extracts what the source says — it must not invent information
  that is not supported by the source.
* A claim without a verbatim supporting quote is rejected — no exceptions.
* The Extractor never decides whether a fact is trustworthy, never compares
  facts across pages, and never touches the knowledge base.

## Output

A saved claims file for that page version (e.g. 17 claims from the docs
overview page).

## Failure Behavior

Unreadable AI output or a failed quote check → the extraction is marked
failed, **nothing is saved**, and the pipeline reports the error for that
URL. No fake facts are ever created.

## Example

Page: "The Amazon Ads MCP server is in open beta." → Claim: that same
sentence, with itself as the verbatim quote.

## Implementation

`pipeline/extractor.py` — the Claude seam (`claude_cli_extract`) plus the
deterministic grounding validation (`validate_extraction`); results in
`state/claims/<hash>.json`.
