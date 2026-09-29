"""Publisher stage: write merged CONCEPTS into knowledge/ as OKF documents.

Deterministic by construction: no LLM, no network, no RNG. The only
non-deterministic input is the injected `now` (timezone-aware datetime) used
for the report's last_run and for a document's last_checked — and
last_checked is only ever written when a document's actual content changes,
so identical Merger output produces byte-identical knowledge documents
across runs even though last_run differs. last_run never feeds identity.

Input: Merger output {"concepts": [...], "rejected": [...]}.

IDENTITY vs FILENAME (deliberately one and the same, stably):
  identity   the concept's stable slug (`id`, established by the concept
             layer — never the sentence wording, never the clock). It is
             what updates are keyed by: the same concept from any source,
             in any wording, updates ONE document.
  filename   f"{id}.md" — the id is already a readable kebab-case slug, so
             filenames are human-friendly by construction and can never
             drift away from identity.

Document mapping (per concept, one OKF document):
  id            the stable concept slug — never the filename, never wording
  title         humanized from the id (stable with it)
  type          "concept" (the single OKF document type in this bundle)
  sources       sorted, de-duplicated source URLs across current facts
  confidence    level of the WEAKEST current fact (a chain is as strong as
                its weakest link): >=0.60 high | >=0.30 medium | else low
  status        official if any contributing source is official, else community
  last_checked  preserved on unchanged documents; bumped only on real change
  body          Facts (verbatim content + per-fact provenance), Conflicts
                (superseded facts with their provenance and superseded_by),
                Sources, Related — rendered by pipeline/concepts.py

Related links are generated ONLY where a relationship is actually supported:
concepts sharing at least one source URL AND >= RELATED_MIN token overlap.
Both directions of support are required — never invented, never decorative.

Idempotency (CLAUDE.md safe-to-re-run contract): unchanged documents are not
rewritten at all; knowledge/INDEX.md is rebuilt from the final snapshot and
written only when its text actually changes; knowledge/CHANGELOG.md is
appended to only when something was published or updated.

Atomicity (review Part 8): documents, INDEX and CHANGELOG are computed in
full first, the complete bundle text is validated (every rendered document
must re-parse as OKF), and ONLY then is everything staged as <name>.tmp and
atomically renamed in ONE batch. There is no intermediate on-disk state in
which documents exist without their catalog entries or vice versa.

CLI: python3 -m pipeline.publisher MERGED_CONCEPTS.json   ("-" reads stdin)
Prints the publication report as JSON; exit 2 on invalid input.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys
from pathlib import Path

from pipeline import concepts
from pipeline import okf

logger = logging.getLogger("pipeline.publisher")  # stable name when run as -m

DEFAULT_KNOWLEDGE_PATH = Path(__file__).resolve().parents[1] / "knowledge"
INDEX_NAME = "INDEX.md"
CHANGELOG_NAME = "CHANGELOG.md"

# Confidence bands — MUST match the Validator's status bands (tested).
HIGH_MIN = 0.60
MEDIUM_MIN = 0.30

# Related-concept links: shared source AND at least this token overlap.
RELATED_MIN = 0.20
RELATED_MAX = 5

INDEX_HEADER = ["# Knowledge Index", "",
                "| Concept | Title | Type | Status | Confidence | Last checked |",
                "|---------|-------|------|--------|------------|--------------|"]


class PublisherError(ValueError):
    """Merger output violates the Publisher contract; nothing is written."""


# --------------------------------------------------------------------------
# Contract validation — centralized; nothing downstream re-checks shapes.
# --------------------------------------------------------------------------

def _check_contract(merged: object) -> tuple[list[dict], list[dict]]:
    """Split merger output into (concepts, rejected). Raises PublisherError
    on malformed input — never repairs it."""
    if isinstance(merged, list):  # tolerate a bare concepts array
        merged = {"concepts": merged, "rejected": []}
    if not isinstance(merged, dict) \
            or not isinstance(merged.get("concepts"), list):
        raise PublisherError(
            "input must be Merger output: {'concepts': [...], 'rejected': [...]}")
    rejected = merged.get("rejected", [])
    if not isinstance(rejected, list):
        raise PublisherError("'rejected' must be a list")
    seen: set[str] = set()
    for i, concept in enumerate(merged["concepts"]):
        try:
            concepts.check_concept(concept)
        except concepts.ConceptError as exc:
            raise PublisherError(f"concepts[{i}]: {exc}") from exc
        if concept["id"] in seen:
            raise PublisherError(
                f"concepts[{i}]: duplicate id {concept['id']!r} — one "
                f"document per topic, always")
        seen.add(concept["id"])
    return merged["concepts"], rejected


# --------------------------------------------------------------------------
# Deterministic document building
# --------------------------------------------------------------------------

def confidence_level(score: float) -> str:
    """Map a numeric score onto OKF levels using the Validator's bands."""
    if score >= HIGH_MIN:
        return "high"
    if score >= MEDIUM_MIN:
        return "medium"
    return "low"


def doc_status(concept: dict) -> str:
    return "official" if any(s["source_type"] == "official"
                             for f in concept["facts"]
                             for s in f["sources"]) else "community"


def concept_urls(concept: dict) -> list[str]:
    return sorted({s["url"] for f in concept["facts"] for s in f["sources"]})


def build_document(concept: dict, last_checked: str, related: list[str]) -> str:
    """Render one concept as a full OKF document. Content is never rewritten."""
    concept = dict(concept)
    if not concept.get("title"):
        concept["title"] = concepts.humanize(concept["id"])
    meta = {
        "id": concept["id"],
        "title": concept["title"],
        "type": concepts.CONCEPT_DOC_TYPE,
        "sources": concept_urls(concept),
        "confidence": confidence_level(
            min(float(f["confidence_score"]) for f in concept["facts"])),
        "status": doc_status(concept),
        "last_checked": last_checked,
    }
    body = concepts.render_body(concept).rstrip("\n")
    if related:
        lines = ["## Related", ""]
        lines.extend(f"- [{concepts.humanize(r)}](./{r}.md)" for r in related)
        body += "\n\n" + "\n".join(lines)
    return okf.serialize(meta, body)


def _doc_tokens(concept: dict) -> frozenset[str]:
    tokens = concepts.canonical_tokens(concept.get("title") or "")
    for f in concept.get("facts", []):
        tokens |= concepts.canonical_tokens(f["content"])
    return tokens


def related_concepts(cid: str, concept: dict,
                     snapshot: dict[str, dict]) -> list[str]:
    """Deterministic, evidence-backed cross-links: a concept is related to
    another when they share at least one source URL AND their material
    overlaps by >= RELATED_MIN. Top RELATED_MAX by (overlap desc, id asc)."""
    mine_urls = {s["url"] for f in concept.get("facts", [])
                 for s in f["sources"]}
    mine_tokens = _doc_tokens(concept)
    scored = []
    for other_id, other in snapshot.items():
        if other_id == cid:
            continue
        other_urls = {s["url"] for f in other.get("facts", [])
                      for s in f["sources"]}
        if not (mine_urls & other_urls):
            continue  # no shared evidence -> no claimed relationship
        overlap = concepts.jaccard(mine_tokens, _doc_tokens(other))
        if overlap >= RELATED_MIN:
            scored.append((overlap, other_id))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [cid2 for _, cid2 in scored[:RELATED_MAX]]


# --------------------------------------------------------------------------
# INDEX / CHANGELOG
# --------------------------------------------------------------------------

def _rebuild_index(kdir: Path, snapshot: dict[str, dict],
                   written: dict[str, dict]) -> str:
    """Regenerate INDEX.md from the final bundle state: every concept in the
    snapshot or written this run gets exactly one canonical row; rows for
    unparseable legacy files are preserved verbatim so nothing vanishes from
    the catalog before the migration rebuild removes them for real."""
    index_path = kdir / INDEX_NAME
    preserve: dict[str, str] = {}
    if index_path.exists():
        for line in index_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("| [") and "](./" in stripped:
                preserve[stripped.split("](./", 1)[1].split(".md)", 1)[0]] = line

    final = dict(snapshot)
    final.update(written)
    rows = []
    listed_stems: set[str] = set()
    for cid in sorted(final):
        meta = {
            "title": final[cid].get("title") or concepts.humanize(cid),
            "status": final[cid].get("status") or doc_status(final[cid]),
            "confidence": final[cid].get("confidence")
            or confidence_level(min(float(f["confidence_score"])
                                    for f in final[cid]["facts"])),
            "last_checked": final[cid].get("last_checked", ""),
        }
        title = meta["title"].replace("|", "\\|")
        rows.append(f"| [{cid}](./{cid}.md) | {title} | concept "
                    f"| {meta['status']} | {meta['confidence']} "
                    f"| {meta['last_checked']} |")
        listed_stems.add(cid)
    for path in sorted(kdir.glob("*.md")):
        if path.name in (INDEX_NAME, CHANGELOG_NAME) or path.stem in listed_stems:
            continue
        try:
            meta, _ = okf.parse(path.read_text(encoding="utf-8"))
        except (okf.OkfError, OSError):
            if path.stem in preserve:
                rows.append(preserve[path.stem])  # legacy row, kept verbatim
            continue
        if meta.get("type") == concepts.CONCEPT_DOC_TYPE:
            continue  # parseable concept not in snapshot: stale duplicate
        if path.stem in preserve:
            rows.append(preserve[path.stem])
    return "\n".join(INDEX_HEADER + rows) + "\n"


def _render_changelog(kdir: Path, entries: list[tuple[str, dict, str]],
                      today: str) -> str:
    """Append today's section with created/updated bullets (sorted by id)."""
    path = kdir / CHANGELOG_NAME
    text = path.read_text(encoding="utf-8") if path.exists() else "# Changelog\n"
    bullets = []
    for action, concept, _ in sorted(entries, key=lambda e: e[1]["id"]):
        resolutions = sorted({f["resolution"] for f in concept["facts"]} |
                             {c["resolution"] for c in concept["conflicts"]})
        urls = ", ".join(concept_urls(concept))
        bullets.append(
            f"- **{concept['id']}** — {action}. {len(concept['facts'])} fact"
            f"{'s' if len(concept['facts']) != 1 else ''}, "
            f"{len(concept['conflicts'])} recorded conflict"
            f"{'s' if len(concept['conflicts']) != 1 else ''}. "
            f"resolutions: {', '.join(resolutions)}.\n"
            f"  Sources: {urls}")
    heading = f"## {today}"
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == heading:
            insert_at = i + 1
            while insert_at < len(lines) and not lines[insert_at].startswith("## "):
                insert_at += 1
            lines[insert_at:insert_at] = bullets + [""]
            return "\n".join(lines).rstrip("\n") + "\n"
    return text.rstrip("\n") + "\n\n" + heading + "\n" + "\n".join(bullets) + "\n"


# --------------------------------------------------------------------------
# Publication
# --------------------------------------------------------------------------

def _stage_and_replace(writes: dict[Path, str]) -> None:
    """Two-phase atomic write: stage every <name>.tmp, then rename them all.
    Any failure removes the temp files and leaves knowledge/ untouched."""
    staged: list[tuple[Path, Path]] = []
    try:
        for path, text in sorted(writes.items()):
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            staged.append((tmp, path))
        for tmp, path in staged:
            tmp.replace(path)
    except OSError:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)
        raise


def publish_concepts(
    merged: dict | list,
    knowledge_dir: str | Path = DEFAULT_KNOWLEDGE_PATH,
    now: datetime.datetime | None = None,
) -> dict:
    """Publish Merger concept output into knowledge/. Returns the report.

    `now` (timezone-aware) only stamps the report's last_run and last_checked
    of genuinely changed documents — it never influences identity.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    if now.tzinfo is None:
        raise PublisherError("'now' must be timezone-aware")
    today = now.date().isoformat()

    publishable, skipped = _check_contract(merged)
    kdir = Path(knowledge_dir)
    kdir.mkdir(parents=True, exist_ok=True)

    snapshot = concepts.load_bundle(kdir)

    counts = {"published": 0, "updated": 0, "unchanged": 0}
    writes: dict[Path, str] = {}
    written_concepts: dict[str, dict] = {}
    changelog_entries: list[tuple[str, dict, str]] = []

    # Pass 1: render every document (with preserved last_checked) and decide
    # published / updated / unchanged by byte comparison.
    drafts: dict[str, tuple[str, str]] = {}  # cid -> (text_with_old_date, old_date)
    for concept in publishable:
        cid = concept["id"]
        path = kdir / f"{cid}.md"
        prior_date = snapshot.get(cid, {}).get("last_checked")
        drafts[cid] = (build_document(concept, prior_date or today, []), prior_date)

    # Related links are computed against the FINAL snapshot (existing docs
    # updated with this run's concepts), then baked into the rendered docs.
    final_snapshot = dict(snapshot)
    for concept in publishable:
        final_snapshot[concept["id"]] = {**concept, "last_checked": today}
    for concept in publishable:
        cid = concept["id"]
        related = related_concepts(cid, concept, final_snapshot)
        path = kdir / f"{cid}.md"
        prior_date = drafts[cid][1]
        current = None
        if path.exists():
            current = path.read_text(encoding="utf-8")
        candidate = build_document(concept, prior_date or today, related)
        if current is not None and candidate == current:
            counts["unchanged"] += 1
            logger.info("unchanged %s", cid)
            continue
        final_text = build_document(concept, today, related)
        writes[path] = final_text
        written_concepts[cid] = concept
        if current is None:
            counts["published"] += 1
            changelog_entries.append(("created", concept, today))
            logger.info("published %s (%d facts)", cid, len(concept["facts"]))
        else:
            counts["updated"] += 1
            changelog_entries.append(("updated", concept, today))
            logger.info("updated %s (%d facts)", cid, len(concept["facts"]))

    for fact in skipped:
        logger.info("skipped rejected fact: %s", str(fact.get("content"))[:80])

    # Pass 2: complete-bundle validation BEFORE anything is staged. Every
    # rendered document must re-parse as valid OKF; INDEX rows must exist.
    for path, text in writes.items():
        try:
            okf.parse(text, path)
        except okf.OkfError as exc:
            raise PublisherError(f"rendered document failed OKF validation: "
                                 f"{exc}") from exc

    index_written = False
    changelog_written = False
    if writes:
        new_index = _rebuild_index(kdir, snapshot, written_concepts)
        index_path = kdir / INDEX_NAME
        old_index = index_path.read_text(encoding="utf-8") \
            if index_path.exists() else None
        if new_index != old_index:
            writes[index_path] = new_index
            index_written = True
        if changelog_entries:
            writes[kdir / CHANGELOG_NAME] = _render_changelog(
                kdir, changelog_entries, today)
            changelog_written = True
        # One atomic batch: documents + INDEX + CHANGELOG together.
        _stage_and_replace(writes)
    else:
        logger.info("nothing published or updated; knowledge/ untouched")

    report = {
        "input": len(publishable),
        "published": counts["published"],
        "updated": counts["updated"],
        "unchanged": counts["unchanged"],
        "skipped": len(skipped),
        "index_updated": index_written,
        "changelog_updated": changelog_written,
        "documents": sorted(str(p) for p in writes
                            if p.name not in (INDEX_NAME, CHANGELOG_NAME)),
        "last_run": now.isoformat(timespec="seconds"),
    }
    logger.info("report: %d concepts -> %d published, %d updated, %d unchanged, "
                "%d rejected", report["input"], report["published"],
                report["updated"], report["unchanged"], report["skipped"])
    return report


# Backwards-compatible alias: the stage still publishes Merger output; only
# the shape changed from raw facts to concepts.
publish_facts = publish_concepts


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None,
         knowledge_dir: str | Path = DEFAULT_KNOWLEDGE_PATH) -> int:
    """CLI entry point: python3 -m pipeline.publisher MERGED_CONCEPTS_JSON

    MERGED_CONCEPTS_JSON is a file with Merger output (or a bare concepts
    array, or "-" for stdin). Prints the publication report as JSON.
    Exit 0 on success; 2 on invalid input.
    """
    parser = argparse.ArgumentParser(
        description="Publish merged concepts into knowledge/ as OKF "
                    "documents (deterministic; no LLM).")
    parser.add_argument("concepts", metavar="MERGED_CONCEPTS_JSON",
                        help="Merger output JSON, or '-' for stdin")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    raw = sys.stdin.read() if args.concepts == "-" \
        else Path(args.concepts).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON input: {exc}", file=sys.stderr)
        return 2
    try:
        report = publish_concepts(data, knowledge_dir=knowledge_dir)
    except PublisherError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
