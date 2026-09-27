# Publisher

> Reference documentation for the Publish stage — not a runnable agent. The
> code lives in `pipeline/publisher.py`.

## Purpose

The Publisher turns the final merged facts into the human-readable
knowledge base in `knowledge/` — carefully, so that re-running it never
creates duplicates and never leaves half-written files behind.

## When To Use

Refer to this document when explaining the knowledge files, document
identity, file names, INDEX/CHANGELOG updates, or idempotency ("if nothing
changed, running the same update again does not create another copy").

## Input

The Merger's final facts: a sentence, its sources (URL, fetch date,
official/community), a trust score, a status, and a resolution label.

## Process

1. **Validates the input contract** — anything malformed stops publication
   *before anything is written*.
2. **Assigns a stable identity**: a fingerprint (hash) of the sentence,
   stored inside the document's frontmatter (the settings block at the top
   of the file). Identity never depends on the file name or the clock.
3. **Names the file human-readably** from the fact's own words (e.g.
   `amazon-ads-mcp-server-open-beta.md`); on a name collision, a short
   piece of the stable identity is appended so nothing is silently
   overwritten.
4. **Writes the document**: the fact word-for-word, all sources, trust
   score, status, resolution. Content is never paraphrased.
5. **Maintains the bookkeeping**: `INDEX.md` (table of contents) and
   `CHANGELOG.md` (dated history), updated only when something actually
   changed.
6. **Writes atomically**: temporary files first, swapped in only when all
   writes succeeded.

## Rules

* Deterministic Python — no LLM. Exact, repeatable files and state.
* Updates existing documents in place (same identity → same document);
  never creates a second document for the same fact.
* Unchanged documents are not touched at all — not even their check-date.
* Facts marked `rejected` are never published (they are skipped and
  counted, not silently dropped).

## Output

Markdown documents in `knowledge/`, an up-to-date INDEX and CHANGELOG, and
a JSON report (published / updated / unchanged counts).

## Failure Behavior

Malformed input → clear error, nothing written. A failure mid-write →
temporary files cleaned up, no partial documents. Identical input re-published
—even with a different clock— produces byte-identical files.

## Example

"The Amazon Ads MCP server is in open beta." (official, 0.6, valid) →
`knowledge/amazon-ads-mcp-server-open-beta.md`, a row in INDEX.md, an entry
in CHANGELOG.md. Re-running the same input: `unchanged`, nothing written.

## Implementation

`pipeline/publisher.py` (`publish_facts`), the OKF document format in
`pipeline/okf.py`, output in `knowledge/`.
