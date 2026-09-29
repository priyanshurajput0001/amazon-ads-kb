# Publisher

> Reference documentation for the Publish stage — not a runnable agent. The
> code lives in `pipeline/publisher.py`, the document body format in
> `pipeline/concepts.py`.

## Purpose

The Publisher turns merged CONCEPTS into the human-readable knowledge base
in `knowledge/` — carefully, so that re-running it never creates
duplicates, never leaves a stale catalog behind, and never writes
half-finished state to disk.

## When To Use

Refer to this document when explaining the knowledge files, concept
identity, file names, INDEX/CHANGELOG updates, cross-links, or idempotency
("if nothing changed, running the same update again does not create
another copy").

## Input

The Merger's concept records: a stable id, a title, current facts (each
with its sources, dates, trust score, status, resolution) and retained
conflicts.

## Process

1. **Validates the input contract** — anything malformed stops publication
   *before anything is written*.
2. **Publishes one document per concept**: the file name is the concept's
   stable id (`api-access.md`), so readable names and identity cannot drift
   apart. Identity never depends on the sentence wording or the clock.
3. **Writes the document**: every fact word-for-word with per-fact
   provenance (score, status, resolution, first-seen date, sources with
   fetch times), a Conflicts section for superseded facts, a Sources
   section, and — only where evidence supports it — Related links to other
   concepts (shared source AND topical overlap).
4. **Maintains the bookkeeping**: `INDEX.md` (table of contents) and
   `CHANGELOG.md` (dated history) are computed from the complete final
   bundle state and written in the SAME atomic batch as the documents —
   there is no intermediate state where documents exist without their
   catalog entries or vice versa.
5. **Writes atomically**: everything is rendered and validated first (every
   document must re-parse as valid OKF with `type: concept`), then staged
   as temp files and swapped in one batch. A failure cleans up and leaves
   `knowledge/` untouched.

## Rules

* Deterministic Python — no LLM. Exact, repeatable files and state.
* Updates existing documents in place (same concept → same document);
  never a second document for the same concept.
* Unchanged documents are not touched at all — not even their check-date.
* Cross-links are generated only from real evidence (shared sources plus
  topical overlap); relationships are never invented.
* Rejected facts are skipped and counted, never published.

## Output

Concept documents in `knowledge/`, an up-to-date INDEX and CHANGELOG, and a
JSON report (published / updated / unchanged counts).

## Failure Behavior

Malformed input → clear error, nothing written. A failure mid-write →
temporary files cleaned up, no partial documents. Identical input
re-published — even with a different clock — produces byte-identical files.

## Example

The "Amazon Ads API access" concept gains a second fact from a new source:
the same `api-access.md` is updated in place, its Sources section grows,
INDEX's row is refreshed, CHANGELOG records the update. Re-running with
nothing new: `unchanged`, nothing written.

## Implementation

`pipeline/publisher.py` (`publish_concepts`), the OKF frontmatter format in
`pipeline/okf.py`, the body format in `pipeline/concepts.py`
(`render_body`/`parse_body`), output in `knowledge/`.
