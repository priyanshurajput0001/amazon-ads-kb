"""Publisher stage: write merged facts into knowledge/ as OKF documents.

Deterministic by construction: no LLM, no network, no RNG. The only
non-deterministic input is the injected `now` (timezone-aware datetime) used
for the report's last_run and for a document's last_checked — and last_checked
is only ever written when a document's actual content changes, so identical
Merger output produces byte-identical knowledge documents across runs even
though last_run differs. last_run never feeds document identity.

Input: Merger output facts:

    {"content": ..., "sources": [{"url", "date", "source_type"}],
     "confidence_score": 0.7, "resolution": ..., "status": valid|valid_low_confidence}

Facts with status "rejected" (Merger passes them through unchanged) are never
published — they are skipped and counted. Malformed facts raise PublisherError
up front; nothing is silently repaired and nothing is written on failure.

IDENTITY vs FILENAME (deliberately separate):

  identity   "kb-" + sha256(content)[:16], stored in the frontmatter `id`.
             Content-only, stable forever; it is what updates are keyed by, so
             the same claim from new sources updates ONE document.
  filename   human-readable kebab-case slug derived from the fact's topic_id
             (when present) or content — stopwords dropped, capped in length,
             special characters stripped. Two different documents can produce
             the same slug; the collision is resolved deterministically by
             appending the tail of the stable id (never a silent overwrite).
             An existing document is always updated IN PLACE at its current
             filename — filenames never churn on re-publication.

The OKF frontmatter schema (pipeline/okf.py) admits exactly
id/title/sources/confidence/status/last_checked, so provenance
(confidence_score, resolution, fact status) lives in the body's Details block,
preserved verbatim.

Document mapping (per fact, one OKF document):
  id            stable content hash (see above) — never the filename
  title         the fact content, verbatim
  sources       sorted, de-duplicated source URLs
  confidence    score mapped onto the OKF levels with the Validator's bands:
                >=0.60 high | >=0.30 medium | else low
  status        official if any contributing source is official, else community
  last_checked  preserved on unchanged documents; bumped only on real change
  body          verbatim content + a provenance block + one ## Sources line
                per source with its url, date and source_type — never
                paraphrased, never summarized

Idempotency (CLAUDE.md safe-to-re-run contract): unchanged documents are not
rewritten at all; knowledge/INDEX.md is rebuilt from the directory and written
only when its text actually changes; knowledge/CHANGELOG.md is appended to
only when something was published or updated. Unrelated existing documents are
never touched. Legacy kb-<hash>.md files from earlier runs are migrated by
rename (same bytes, same id, readable filename) instead of duplicated.

Atomicity: new/changed files are first staged as <name>.tmp and atomically
renamed only after all staged writes succeeded. A failure at any point cleans
up its temp files and leaves no partial documents.

CLI: python3 -m pipeline.publisher MERGED_FACTS.json   ("-" reads stdin)
Prints the publication report as JSON; exit 2 on invalid input.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
import re
import sys
from pathlib import Path

from pipeline import okf

logger = logging.getLogger("pipeline.publisher")  # stable name when run as -m

DEFAULT_KNOWLEDGE_PATH = Path(__file__).resolve().parents[1] / "knowledge"
INDEX_NAME = "INDEX.md"
CHANGELOG_NAME = "CHANGELOG.md"

FACT_STATUSES = ("valid", "valid_low_confidence", "rejected")
RESOLUTIONS = (
    "single_source",
    "duplicate_merged",
    "conflict_resolved_by_authority",
    "conflict_resolved_by_recency",
    "conflict_resolved_by_majority",
    "complementary_merge",
    "unresolved_conflict",
)
SOURCE_TYPES = ("official", "community")
ID_PREFIX = "kb-"
ID_HASH_LEN = 16
LEGACY_RE = re.compile(rf"^{ID_PREFIX}[0-9a-f]{{{ID_HASH_LEN}}}$")

# Readable-filename slugging: common English glue words are dropped so slugs
# read like topics ("amazon-ads-reporting-api-open-beta"), not sentences.
STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "in", "on", "at", "to", "of", "for", "with", "and", "or", "by", "as",
    "from", "that", "this", "these", "those", "it", "its", "their", "can",
    "will", "shall", "may", "might", "has", "have", "had", "do", "does",
    "did", "not", "no", "nor", "so", "such", "than", "then", "there",
    "here", "when", "while", "who", "whom", "whose", "which", "what",
})
SLUG_WORD_RE = re.compile(r"[a-z0-9]+")
SLUG_MAX = 64

INDEX_HEADER = ["# Knowledge Index", "",
                "| ID | Title | Status | Confidence | Last checked |",
                "|----|-------|--------|------------|--------------|"]


class PublisherError(ValueError):
    """Merger output violates the Publisher contract; nothing is written."""


# --------------------------------------------------------------------------
# Contract validation — centralized; nothing downstream re-checks shapes.
# --------------------------------------------------------------------------

def _check_contract(facts: object) -> tuple[list[dict], list[dict]]:
    """Split input into (publishable, skipped-rejected). Raises PublisherError
    on malformed facts — never repairs them."""
    if not isinstance(facts, list):
        raise PublisherError("input must be a JSON array of fact objects")
    publishable: list[dict] = []
    skipped: list[dict] = []
    for i, fact in enumerate(facts):
        if not isinstance(fact, dict):
            raise PublisherError(f"fact[{i}] is not an object")
        if "status" not in fact:
            raise PublisherError(f"fact[{i}]: missing 'status'")
        if fact["status"] not in FACT_STATUSES:
            raise PublisherError(
                f"fact[{i}]: status must be one of {FACT_STATUSES}, "
                f"got {fact['status']!r}")
        if fact["status"] == "rejected":
            skipped.append(fact)
            continue
        content = fact.get("content")
        if not isinstance(content, str) or not content.strip():
            raise PublisherError(f"fact[{i}]: 'content' must be a non-empty string")
        if "\n" in content or "\r" in content:
            raise PublisherError(f"fact[{i}]: 'content' must be single-line "
                                 "(multi-line content breaks OKF frontmatter)")
        sources = fact.get("sources")
        if not isinstance(sources, list) or not sources:
            raise PublisherError(f"fact[{i}]: 'sources' must be a non-empty list")
        for j, source in enumerate(sources):
            if not isinstance(source, dict):
                raise PublisherError(f"fact[{i}].sources[{j}] is not an object")
            if not isinstance(source.get("url"), str) or not source["url"].strip():
                raise PublisherError(
                    f"fact[{i}].sources[{j}]: 'url' must be a non-empty string")
            if source.get("date") is not None and not isinstance(source["date"], str):
                raise PublisherError(
                    f"fact[{i}].sources[{j}]: 'date' must be a string or null")
            if source.get("source_type") not in SOURCE_TYPES:
                raise PublisherError(
                    f"fact[{i}].sources[{j}]: source_type must be one of "
                    f"{SOURCE_TYPES}, got {source.get('source_type')!r}")
        score = fact.get("confidence_score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise PublisherError(
                f"fact[{i}]: 'confidence_score' must be numeric, got {score!r}")
        if not 0.0 <= float(score) <= 1.0:
            raise PublisherError(
                f"fact[{i}]: 'confidence_score' must be within [0, 1], got {score!r}")
        if fact.get("resolution") not in RESOLUTIONS:
            raise PublisherError(
                f"fact[{i}]: resolution must be one of {RESOLUTIONS}, "
                f"got {fact.get('resolution')!r}")
        publishable.append(fact)
    return publishable, skipped


# --------------------------------------------------------------------------
# Stable identity and readable filenames
# --------------------------------------------------------------------------

def fact_id(content: str) -> str:
    """Stable document identity: content hash only. No timestamps, no RNG,
    no filename involvement."""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:ID_HASH_LEN]
    return f"{ID_PREFIX}{digest}"


def readable_slug(text: str) -> str:
    """Deterministic kebab-case slug: lowercase words, stopwords dropped,
    capped at SLUG_MAX on a word boundary. Empty input -> 'fact'."""
    words = [w for w in SLUG_WORD_RE.findall(text.lower())
             if w not in STOPWORDS]
    slug = "-".join(words)
    if len(slug) > SLUG_MAX:
        slug = slug[:SLUG_MAX].rsplit("-", 1)[0]
    return slug.strip("-") or "fact"


def _slug_base(fact: dict) -> str:
    """Prefer an explicit topic when the fact carries one; else its content."""
    for key in ("topic_id", "topic"):
        value = fact.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return fact["content"]


def _claim_stem(slug: str, stable_id: str, stems: dict[str, str | None]) -> str:
    """Reserve a unique filename stem for `stable_id`. On slug collision the
    tail of the stable id disambiguates — two documents never share a file."""
    candidates = [slug] if slug else []
    if slug:
        candidates.append(f"{slug}-{stable_id[-6:]}")
    candidates.append(stable_id)
    for candidate in candidates:
        owner = stems.get(candidate)
        if owner is None or owner == stable_id:
            stems[candidate] = stable_id
            return candidate
    raise PublisherError(f"unable to reserve a filename for {stable_id}")


# --------------------------------------------------------------------------
# Deterministic document building
# --------------------------------------------------------------------------

def confidence_level(score: float) -> str:
    """Map the numeric score onto OKF levels using the Validator's bands."""
    if score >= 0.6:
        return "high"
    if score >= 0.3:
        return "medium"
    return "low"


def doc_status(sources: list[dict]) -> str:
    return "official" if any(s["source_type"] == "official" for s in sources) \
        else "community"


def build_document(fact: dict, last_checked: str) -> str:
    """Render one fact as an OKF document. Content is never rewritten."""
    content = fact["content"]
    sources = sorted(fact["sources"], key=lambda s: s["url"])
    meta = {
        "id": fact_id(content),
        "title": content,
        "sources": sorted({s["url"] for s in sources}),
        "confidence": confidence_level(float(fact["confidence_score"])),
        "status": doc_status(sources),
        "last_checked": last_checked,
    }
    score = float(fact["confidence_score"])
    body = [
        f"# {content}",
        "",
        "## Details",
        "",
        content,
        "",
        f"- confidence_score: {score:.2f}",
        f"- resolution: {fact['resolution']}",
        f"- status: {fact['status']}",
        "",
        "## Sources",
    ]
    for source in sources:
        date = source["date"] if source["date"] else "unknown"
        body.append(f"- {source['url']} — {source['source_type']}, fetched {date}")
    return okf.serialize(meta, "\n".join(body))


# --------------------------------------------------------------------------
# knowledge/ scanning, migration, INDEX
# --------------------------------------------------------------------------

def _scan_knowledge(kdir: Path) -> dict:
    """Map the current bundle: stable id -> path, plus which stems are taken.
    Unparseable files occupy their stem but have no identity."""
    paths: dict[str, Path] = {}
    titles: dict[str, str] = {}
    stems: dict[str, str | None] = {}
    for path in sorted(kdir.glob("*.md")):
        if path.name in (INDEX_NAME, CHANGELOG_NAME):
            continue
        stems[path.stem] = None
        try:
            meta, _ = okf.parse(path.read_text(encoding="utf-8"))
        except (okf.OkfError, OSError):
            continue
        stable = meta.get("id")
        if isinstance(stable, str) and stable:
            paths[stable] = path
            titles[stable] = meta.get("title", "")
            stems[path.stem] = stable
    return {"paths": paths, "titles": titles, "stems": stems}


def _migrate_legacy(kdir: Path, scan: dict) -> list[tuple[Path, Path]]:
    """Rename opaque kb-<hash>.md files to readable slugs (same bytes, same
    id) so old and new naming schemes never coexist."""
    renames: list[tuple[Path, Path]] = []
    for stable in sorted(scan["paths"]):
        path = scan["paths"][stable]
        if not LEGACY_RE.match(path.stem):
            continue
        slug = readable_slug(scan["titles"].get(stable, stable))
        stem = _claim_stem(slug, stable, scan["stems"])
        new_path = kdir / f"{stem}.md"
        if new_path == path or new_path.exists():
            continue
        path.rename(new_path)
        scan["stems"].pop(path.stem, None)
        scan["paths"][stable] = new_path
        renames.append((path, new_path))
        logger.info("migrated %s -> %s (id %s unchanged)", path.name,
                    new_path.name, stable)
    return renames


def _rebuild_index(kdir: Path) -> str:
    """Regenerate INDEX.md from the actual directory contents. Rows for
    parseable documents are rebuilt in canonical form (links use real
    filenames); rows for unparseable files are preserved verbatim."""
    index_path = kdir / INDEX_NAME
    preserve: dict[str, str] = {}
    if index_path.exists():
        for line in index_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("| [") and "](./" in stripped:
                preserve[stripped.split("](./", 1)[1].split(".md)", 1)[0]] = line
    rows = []
    for path in sorted(kdir.glob("*.md")):
        if path.name in (INDEX_NAME, CHANGELOG_NAME):
            continue
        try:
            meta, _ = okf.parse(path.read_text(encoding="utf-8"))
        except (okf.OkfError, OSError):
            if path.stem in preserve:
                rows.append(preserve[path.stem])
            continue
        title = meta["title"].replace("|", "\\|")
        rows.append(f"| [{path.stem}](./{path.name}) | {title} "
                    f"| {meta['status']} | {meta['confidence']} "
                    f"| {meta['last_checked']} |")
    return "\n".join(INDEX_HEADER + rows) + "\n"


def _render_changelog(kdir: Path, entries: list[tuple[str, dict, str]],
                      today: str) -> str:
    """Append today's section with created/updated bullets (sorted by id)."""
    path = kdir / CHANGELOG_NAME
    text = path.read_text(encoding="utf-8") if path.exists() else "# Changelog\n"
    bullets = []
    for action, fact, _ in sorted(entries, key=lambda e: fact_id(e[1]["content"])):
        fid = fact_id(fact["content"])
        urls = ", ".join(sorted({s["url"] for s in fact["sources"]}))
        bullets.append(
            f"- **{fid}** — {action}. resolution={fact['resolution']}, "
            f"confidence_score={float(fact['confidence_score']):.2f}, "
            f"status={fact['status']}.\n  Sources: {urls}")
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


def publish_facts(
    facts: list[dict],
    knowledge_dir: str | Path = DEFAULT_KNOWLEDGE_PATH,
    now: datetime.datetime | None = None,
) -> dict:
    """Publish Merger output into knowledge/. Returns the audit report.

    `now` (timezone-aware) only stamps the report's last_run and last_checked
    of genuinely changed documents — it never influences document identity or
    filenames.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    if now.tzinfo is None:
        raise PublisherError("'now' must be timezone-aware")
    today = now.date().isoformat()

    publishable, skipped = _check_contract(facts)
    kdir = Path(knowledge_dir)
    kdir.mkdir(parents=True, exist_ok=True)

    scan = _scan_knowledge(kdir)
    renames = _migrate_legacy(kdir, scan)

    counts = {"published": 0, "updated": 0, "unchanged": 0}
    writes: dict[Path, str] = {}
    changelog_entries: list[tuple[str, dict, str]] = []

    for fact in publishable:
        stable = fact_id(fact["content"])
        # Existing documents are updated in place — the filename never churns.
        path = scan["paths"].get(stable)
        if path is None:
            stem = _claim_stem(readable_slug(_slug_base(fact)), stable,
                               scan["stems"])
            path = kdir / f"{stem}.md"
            scan["paths"][stable] = path

        current = writes.get(path)
        if current is None and path.exists():
            current = path.read_text(encoding="utf-8")

        if current is None:
            writes[path] = build_document(fact, today)
            counts["published"] += 1
            changelog_entries.append(("created", fact, today))
            logger.info("published %s at %s (%s)", stable, path.name,
                        fact["resolution"])
        else:
            try:
                meta, _ = okf.parse(current)
                prior_date = meta["last_checked"]
            except okf.OkfError:
                prior_date = None  # unparseable existing doc: rewrite it
                logger.warning("%s exists but does not parse as OKF; rewriting",
                               path.name)
            if prior_date and build_document(fact, prior_date) == current:
                counts["unchanged"] += 1
                logger.info("unchanged %s at %s", stable, path.name)
            else:
                writes[path] = build_document(fact, today)
                counts["updated"] += 1
                changelog_entries.append(("updated", fact, today))
                logger.info("updated %s at %s", stable, path.name)

    for fact in skipped:
        logger.info("skipped rejected fact: %s", str(fact.get("content"))[:80])

    index_written = False
    if writes:
        _stage_and_replace(writes)
    if writes or renames:
        index_path = kdir / INDEX_NAME
        new_index = _rebuild_index(kdir)
        old_index = index_path.read_text(encoding="utf-8") \
            if index_path.exists() else None
        extra: dict[Path, str] = {}
        if new_index != old_index:
            extra[index_path] = new_index
            index_written = True
        if changelog_entries:
            extra[kdir / CHANGELOG_NAME] = _render_changelog(
                kdir, changelog_entries, today)
        if extra:
            _stage_and_replace(extra)
    if not writes and not renames:
        logger.info("nothing published, updated or migrated; knowledge/ untouched")

    report = {
        "input": len(facts),
        "published": counts["published"],
        "updated": counts["updated"],
        "unchanged": counts["unchanged"],
        "skipped": len(skipped),
        "renamed": len(renames),
        "index_updated": index_written,
        "documents": sorted(str(p) for p in writes),
        "last_run": now.isoformat(timespec="seconds"),
    }
    logger.info("report: %d input -> %d published, %d updated, %d unchanged, "
                "%d skipped, %d renamed", report["input"], report["published"],
                report["updated"], report["unchanged"], report["skipped"],
                report["renamed"])
    return report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None,
         knowledge_dir: str | Path = DEFAULT_KNOWLEDGE_PATH) -> int:
    """CLI entry point: python3 -m pipeline.publisher MERGED_FACTS_JSON

    MERGED_FACTS_JSON is a file with a JSON array of Merger facts (or
    {"facts": [...]}) or "-" for stdin. Prints the publication report as JSON.
    Exit 0 on success; 2 on invalid input.
    """
    parser = argparse.ArgumentParser(
        description="Publish merged facts into knowledge/ as OKF documents "
                    "(deterministic; no LLM).")
    parser.add_argument("facts", metavar="MERGED_FACTS_JSON",
                        help="JSON array of merged facts, or '-' for stdin")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    raw = sys.stdin.read() if args.facts == "-" \
        else Path(args.facts).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON input: {exc}", file=sys.stderr)
        return 2
    if isinstance(data, dict) and isinstance(data.get("facts"), list):
        data = data["facts"]
    try:
        report = publish_facts(data, knowledge_dir=knowledge_dir)
    except PublisherError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
