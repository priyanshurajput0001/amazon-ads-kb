"""One-time bundle rebuild: regenerate knowledge/ from the recorded claims.

WHY (review 2026-09-28, Part 12): the existing bundle is one extracted
sentence per document — 50+ fragments with duplicate pairs — because the old
Publisher made each sentence its own document. The concept layer replaces
that model. This driver re-runs the deterministic pipeline over EVERY
claims file ever recorded in state/claims/ (all content versions, with their
original fetch dates, so historical values surface as dated conflicts
resolved by recency) and republishes the bundle as concept documents.

Safety:
  1. the new bundle is built in a temporary directory (the live bundle is
     never touched while building);
  2. the COMPLETE new bundle is validated (every document parses as OKF
     with type=concept, INDEX <-> documents consistent) before anything
     replaces the old one;
  3. replacement is a directory swap: knowledge -> knowledge.pre-rebuild
     backup, new -> knowledge, backup removed only after the swap succeeded.
     On any failure the original bundle is still in place, untouched.

The old CHANGELOG history is carried over (the new bundle's CHANGELOG starts
from the old text, then appends the rebuild section); INDEX is regenerated.

Facts that only exist in hand-written pre-pipeline documents and were never
backed by a claims file are NOT carried over automatically — every fact in
the rebuilt bundle has claim-level provenance. (The overview page itself is
claims-backed, so its facts reappear; hand-written curated prose does not.)

CLI: python3 -m pipeline.rebuild    (LLM seams are the real ones by default;
tests inject fakes). Prints the rebuild report as JSON.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

from pipeline import concepts, okf
from pipeline.adapter import build_facts
from pipeline.merger import claude_cli_classify, merge_facts
from pipeline.publisher import publish_concepts
from pipeline.state import SourceState, load_state
from pipeline.validator import validate_facts

logger = logging.getLogger("pipeline.rebuild")

DEFAULTS = {
    "state": Path(__file__).resolve().parents[1] / "state" / "fetch_state.json",
    "claims": Path(__file__).resolve().parents[1] / "state" / "claims",
    "knowledge": Path(__file__).resolve().parents[1] / "knowledge",
}


class RebuildError(RuntimeError):
    """The rebuild could not be completed; the old bundle is untouched."""


def collect_facts(state_path: Path, claims_dir: Path) -> tuple[list[dict], int]:
    """Facts from EVERY recorded claims file (all content versions), in a
    deterministic (source_url, fetched_at, sha256) order."""
    states = load_state(state_path)
    files = []
    for path in sorted(claims_dir.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RebuildError(f"{path}: corrupt claims JSON ({exc})") from None
        if doc.get("status") != "ok" or not isinstance(doc.get("claims"), list):
            logger.warning("%s: not a valid ok claims doc; skipped", path.name)
            continue
        files.append((doc.get("source_url", ""), doc.get("fetched_at", ""),
                      doc.get("sha256", path.stem), doc, path))
    files.sort(key=lambda item: (item[0], item[1], item[2]))

    facts: list[dict] = []
    for source_url, fetched_at, sha, doc, path in files:
        committed = states.get(source_url)
        is_current = bool(committed and committed.sha256 == sha)
        version_state = SourceState(
            url=source_url, status="ok", fetched_at=fetched_at, sha256=sha,
            strategy=committed.strategy if is_current else None)
        built = build_facts(doc, version_state)
        for fact in built:
            fact["rebuild_version"] = "current" if is_current else "historical"
        facts.extend(built)
        logger.info("collected %d claims from %s (%s version, fetched %s)",
                    len(built), source_url,
                    "current" if is_current else "historical", fetched_at[:10])
    return facts, len(files)


def lint_bundle(kdir: Path) -> list[str]:
    """Validate a complete bundle: every document parses as an OKF concept,
    no duplicate ids, INDEX links resolve both ways. Returns problems ([])."""
    problems: list[str] = []
    docs = [p for p in sorted(kdir.glob("*.md"))
            if p.name not in ("INDEX.md", "CHANGELOG.md")]
    if not docs:
        return ["bundle contains no concept documents"]
    ids: set[str] = set()
    for path in docs:
        try:
            meta, body = okf.parse(path.read_text(encoding="utf-8"), path)
        except (okf.OkfError, OSError) as exc:
            problems.append(f"{path.name}: {exc}")
            continue
        if meta["type"] != concepts.CONCEPT_DOC_TYPE:
            problems.append(f"{path.name}: type is {meta['type']!r}")
        if meta["id"] in ids:
            problems.append(f"{path.name}: duplicate id {meta['id']}")
        ids.add(meta["id"])
        try:
            facts, conflicts = concepts.parse_body(body)
            concepts.check_concept({"id": meta["id"], "facts": facts,
                                    "conflicts": conflicts})
        except concepts.ConceptError as exc:
            problems.append(f"{path.name}: {exc}")
    index_path = kdir / "INDEX.md"
    if not index_path.exists():
        problems.append("INDEX.md missing")
    else:
        linked = {line.split("](./", 1)[1].split(".md)", 1)[0]
                  for line in index_path.read_text(encoding="utf-8").splitlines()
                  if "](./" in line}
        if linked != {p.stem for p in docs}:
            problems.append(
                f"INDEX/documents mismatch: only-in-index="
                f"{sorted(linked - {p.stem for p in docs})}, "
                f"only-on-disk={sorted({p.stem for p in docs} - linked)}")
    return problems


def rebuild_bundle(
    state_path: Path = DEFAULTS["state"],
    claims_dir: Path = DEFAULTS["claims"],
    knowledge_dir: Path = DEFAULTS["knowledge"],
    classify_llm=claude_cli_classify,
    match_llm=concepts.claude_cli_concept_match,
    now: datetime.datetime | None = None,
) -> dict:
    """Regenerate the bundle from recorded claims. The old bundle stays in
    place unless the new one builds AND validates cleanly."""
    now = now or datetime.datetime.now(datetime.UTC)
    knowledge_dir = Path(knowledge_dir)

    facts, files = collect_facts(state_path, claims_dir)
    if not facts:
        raise RebuildError("no claims found; nothing to rebuild from")
    validated = validate_facts(facts)
    participating = [f for f in validated if f["status"] != "rejected"]
    rejected = [f for f in validated if f["status"] == "rejected"]
    merged = merge_facts(participating, existing={}, classify_llm=classify_llm,
                         match_llm=match_llm, today=now.date().isoformat())

    with tempfile.TemporaryDirectory(prefix="kb-rebuild-") as tmpname:
        staging = Path(tmpname) / "knowledge"
        staging.mkdir(parents=True)
        # Carry the changelog history over; the Publisher appends to it.
        old_changelog = knowledge_dir / "CHANGELOG.md"
        if old_changelog.exists():
            shutil.copyfile(old_changelog, staging / "CHANGELOG.md")
        report = publish_concepts(
            {"concepts": merged["concepts"], "rejected": rejected},
            knowledge_dir=staging, now=now)

        problems = lint_bundle(staging)
        if problems:
            for problem in problems:
                logger.error("new bundle invalid: %s", problem)
            raise RebuildError(
                "rebuilt bundle failed validation; old bundle kept: "
                + "; ".join(problems[:5]))

        if knowledge_dir.exists():
            backup = knowledge_dir.with_name(knowledge_dir.name + ".pre-rebuild")
            if backup.exists():
                shutil.rmtree(backup)
            knowledge_dir.rename(backup)
            try:
                staging.rename(knowledge_dir)
            except OSError:
                backup.rename(knowledge_dir)  # put the original back
                raise
            shutil.rmtree(backup)
            logger.info("replaced %s (old bundle removed after successful "
                        "swap)", knowledge_dir)
        else:
            knowledge_dir.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(knowledge_dir)

    surviving = sum(len(c["facts"]) + len(c["conflicts"])
                    for c in merged["concepts"])
    return {
        "claims_files": files,
        "facts_built": len(facts),
        "facts_valid": len(participating),
        "facts_rejected": len(rejected),
        "concepts": len(merged["concepts"]),
        "facts_plus_conflicts_in_bundle": surviving,
        "published": report["published"],
        "documents": [Path(p).name for p in
                      sorted(knowledge_dir.glob("*.md"))
                      if p.name not in ("INDEX.md", "CHANGELOG.md")],
        "lint": "clean",
        "rebuilt_at": now.isoformat(timespec="seconds"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild knowledge/ as concept documents from the "
                    "recorded claims (one-time migration).")
    parser.add_argument("--state-path", default=str(DEFAULTS["state"]))
    parser.add_argument("--claims-dir", default=str(DEFAULTS["claims"]))
    parser.add_argument("--knowledge-dir", default=str(DEFAULTS["knowledge"]))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        report = rebuild_bundle(
            state_path=Path(args.state_path), claims_dir=Path(args.claims_dir),
            knowledge_dir=Path(args.knowledge_dir))
    except RebuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
