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

Document mapping (per fact, one OKF document):
  id / filename  "kb-" + sha256(content)[:16] — identity from content only, so
                 the same claim from new sources UPDATES the same document
                 instead of forking a second one
  title          the fact content, verbatim
  sources        sorted, de-duplicated source URLs
  confidence     score mapped onto the OKF levels with the Validator's bands:
                 >=0.60 high | >=0.30 medium | else low
  status         official if any contributing source is official, else community
  last_checked   preserved on unchanged documents; bumped only on real change
  body           verbatim content + a provenance block (confidence_score,
                 resolution, status) + one ## Sources line per source with its
                 url, date and source_type — never paraphrased, never summarized

Idempotency (CLAUDE.md safe-to-re-run contract): unchanged documents are not
rewritten at all; knowledge/INDEX.md is regenerated only when rows change and
knowledge/CHANGELOG.md is appended to only when something was published or
updated. Unrelated existing documents are never touched.

Atomicity: every new/changed file is first staged as <name>.tmp, and the
atomic renames happen only after all staged writes succeeded. A failure at
any point cleans up its temp files and leaves knowledge/ without partial
documents.

CLI: python3 -m pipeline.publisher MERGED_FACTS.json   ("-" reads stdin)
Prints the publication report as JSON; exit 2 on invalid input.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
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
# Deterministic document building
# --------------------------------------------------------------------------

def fact_id(content: str) -> str:
    """Stable document identity: content hash only. No timestamps, no RNG."""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:ID_HASH_LEN]
    return f"{ID_PREFIX}{digest}"


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


def _index_row(fact: dict, last_checked: str) -> str:
    title = fact["content"].replace("|", "\\|")
    sources = fact["sources"]
    return (f"| [{fact_id(fact['content'])}](./{fact_id(fact['content'])}.md) "
            f"| {title} | {doc_status(sources)} "
            f"| {confidence_level(float(fact['confidence_score']))} "
            f"| {last_checked} |")


def _render_index(kdir: Path, rows: dict[str, str]) -> str:
    """Rebuild INDEX.md from existing rows (preserved verbatim) + our updates."""
    path = kdir / INDEX_NAME
    existing: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("| [") and "](./" in stripped:
                doc_id = stripped.split("](./", 1)[1].split(".md)", 1)[0]
                existing[doc_id] = line
    existing.update(rows)
    lines = list(INDEX_HEADER) + [existing[k] for k in sorted(existing)]
    return "\n".join(lines) + "\n"


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
    section = f"## {today}\n" + "\n".join(bullets) + "\n"
    lines = text.splitlines()
    heading = f"## {today}"
    for i, line in enumerate(lines):
        if line.strip() == heading:
            insert_at = i + 1
            while insert_at < len(lines) and not lines[insert_at].startswith("## "):
                insert_at += 1
            lines[insert_at:insert_at] = bullets + [""]
            return "\n".join(lines).rstrip("\n") + "\n"
    return text.rstrip("\n") + "\n\n" + section


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
    of genuinely changed documents — it never influences document identity.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    if now.tzinfo is None:
        raise PublisherError("'now' must be timezone-aware")
    today = now.date().isoformat()

    publishable, skipped = _check_contract(facts)
    kdir = Path(knowledge_dir)
    kdir.mkdir(parents=True, exist_ok=True)

    counts = {"published": 0, "updated": 0, "unchanged": 0}
    writes: dict[Path, str] = {}
    index_rows: dict[str, str] = {}
    changelog_entries: list[tuple[str, dict, str]] = []

    for fact in publishable:
        fid = fact_id(fact["content"])
        path = kdir / f"{fid}.md"
        staged_text = writes.get(path)
        on_disk = path.read_text(encoding="utf-8") if path.exists() else None
        current = staged_text if staged_text is not None else on_disk

        if current is None:
            writes[path] = build_document(fact, today)
            counts["published"] += 1
            changelog_entries.append(("created", fact, today))
            logger.info("published %s (%s)", fid, fact["resolution"])
        else:
            try:
                meta, _ = okf.parse(current)
                prior_date = meta["last_checked"]
            except okf.OkfError:
                prior_date = None  # unparseable existing doc: rewrite it
                logger.warning("%s exists but does not parse as OKF; rewriting", fid)
            if prior_date and build_document(fact, prior_date) == current:
                counts["unchanged"] += 1
                logger.info("unchanged %s", fid)
            else:
                writes[path] = build_document(fact, today)
                counts["updated"] += 1
                changelog_entries.append(("updated", fact, today))
                logger.info("updated %s", fid)
        index_rows[fid] = _index_row(fact, today)

    for fact in skipped:
        logger.info("skipped rejected fact: %s", str(fact.get("content"))[:80])

    if counts["published"] or counts["updated"]:
        _stage_and_replace(writes)
        _stage_and_replace({
            kdir / INDEX_NAME: _render_index(kdir, index_rows),
            kdir / CHANGELOG_NAME: _render_changelog(kdir, changelog_entries, today),
        })
    else:
        logger.info("nothing published or updated; knowledge/ untouched")

    report = {
        "input": len(facts),
        "published": counts["published"],
        "updated": counts["updated"],
        "unchanged": counts["unchanged"],
        "skipped": len(skipped),
        "documents": sorted(str(p) for p in writes),
        "last_run": now.isoformat(timespec="seconds"),
    }
    logger.info("report: %d input -> %d published, %d updated, %d unchanged, "
                "%d skipped", report["input"], report["published"],
                report["updated"], report["unchanged"], report["skipped"])
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
