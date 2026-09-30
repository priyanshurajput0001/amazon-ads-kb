#!/usr/bin/env python3
"""Lint the knowledge bundle (OKF + concept contract). Stdlib only.

Modes:
  python3 scripts/lint_bundle.py
      Full-bundle lint over knowledge/: every document parses as OKF with
      type=concept, id equals the filename stem, sources non-empty, no
      duplicate ids or titles, Related links resolve, INDEX rows match the
      files exactly. Wraps pipeline.rebuild.lint_bundle. Exit 1 on any
      problem (prints them), 0 when clean.

  python3 scripts/lint_bundle.py --pretooluse
      PreToolUse hook mode (registered in .claude/settings.json for
      Write|Edit). Reads the hook payload from stdin; when the tool would
      write inside knowledge/, the proposed change is applied to a throwaway
      COPY of the bundle and the copy is linted with the bundle-wide checks
      that are attributable to THIS write (document contract, duplicate
      id/title, dangling links). INDEX-consistency is deliberately NOT
      enforced per-write: INDEX.md is regenerated atomically by the
      publisher together with the documents, so a single mid-batch write
      legitimately lags the INDEX — the full-bundle mode above (and the
      publisher's own gate) enforce it. Exit 2 blocks the tool call. An
      unreadable/malformed JSON payload also blocks (exit 2): it cannot be
      proven to target a path outside knowledge/.

  python3 scripts/lint_bundle.py --knowledge-dir DIR
      Lint a different bundle directory (used by tests).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from pipeline import concepts, okf  # noqa: E402
from pipeline.rebuild import lint_bundle  # noqa: E402

KNOWLEDGE_DIR = REPO_ROOT / "knowledge"


def _per_file_problems(kdir: Path, changed: Path) -> list[str]:
    """Bundle-wide checks attributable to one written file. `changed` is the
    file (inside the copied bundle) this tool call would modify."""
    problems: list[str] = []
    docs = [p for p in sorted(kdir.glob("*.md"))
            if p.name not in ("INDEX.md", "CHANGELOG.md")]
    stems = {p.stem for p in docs}
    titles: dict[str, str] = {}
    ids: set[str] = set()
    for path in docs:
        try:
            meta, body = okf.parse(path.read_text(encoding="utf-8"), path)
        except (okf.OkfError, OSError) as exc:
            if path == changed:
                problems.append(f"{path.name}: {exc}")
            continue
        if path != changed:
            # pre-existing documents only matter as context (duplicates)
            _ = (meta, body)
        if path == changed:
            if meta["type"] != concepts.CONCEPT_DOC_TYPE:
                problems.append(f"{path.name}: type must be 'concept'")
            if meta["id"] != path.stem:
                problems.append(f"{path.name}: id {meta['id']!r} != filename")
            if not meta.get("sources"):
                problems.append(f"{path.name}: sources list is empty")
            for target in concepts.related_link_targets(body):
                if target not in stems:
                    problems.append(f"{path.name}: dangling link ./{target}.md")
        if meta["id"] in ids:
            problems.append(f"{path.name}: duplicate id {meta['id']!r}")
        ids.add(meta["id"])
        if meta["title"] in titles:
            problems.append(f"{path.name}: duplicate title "
                            f"{meta['title']!r} (also {titles[meta['title']]})")
        titles[meta["title"]] = path.name
    return problems


def pretooluse(kdir: Path) -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        # An unreadable payload cannot be proven to target a path OUTSIDE
        # knowledge/, so the guard fails CLOSED: block and say why. Payloads
        # that clearly resolve outside knowledge/ (valid JSON, outside path)
        # still return 0 below.
        print(f"lint hook: unreadable payload ({exc}); blocking because the "
              f"target cannot be proven to be outside knowledge/",
              file=sys.stderr)
        return 2
    tool = payload.get("tool_name", "")
    if tool not in ("Write", "Edit"):
        return 0
    inp = payload.get("tool_input") or {}
    raw = inp.get("file_path") or inp.get("notebook_path") or ""
    try:
        path = Path(raw).resolve()
    except (TypeError, ValueError):
        return 0
    try:
        path.relative_to(kdir.resolve())
    except ValueError:
        return 0  # outside knowledge/: not this hook's business
    if path.name in ("INDEX.md", "CHANGELOG.md"):
        return 0  # catalog files: consistency is enforced by the full lint

    with tempfile.TemporaryDirectory(prefix="kb-lint-hook-") as tmp:
        copy = Path(tmp) / "knowledge"
        shutil.copytree(kdir, copy)
        target = copy / path.name
        if tool == "Write":
            target.write_text(str(inp.get("content", "")), encoding="utf-8")
        else:  # Edit: apply old_string -> new_string (all occurrences flag)
            text = target.read_text(encoding="utf-8") if target.exists() else ""
            old, new = str(inp.get("old_string", "")), str(inp.get("new_string", ""))
            if old and old in text:
                count = text.count(old)
                if inp.get("replace_all") or count == 1:
                    text = text.replace(old, new)
            else:
                print(f"lint hook: could not apply edit to {path.name}; "
                      f"blocking so the bundle is not corrupted",
                      file=sys.stderr)
                return 2
            target.write_text(text, encoding="utf-8")
        problems = _per_file_problems(copy, target)
    if problems:
        print(f"lint hook: this write would leave knowledge/ invalid:",
              file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2  # block the tool call
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--pretooluse", action="store_true",
                        help="PreToolUse hook mode: read the hook payload "
                             "from stdin and guard one knowledge/ write")
    parser.add_argument("--knowledge-dir", default=str(KNOWLEDGE_DIR),
                        help="bundle directory to lint (default: knowledge/)")
    args = parser.parse_args(argv)
    if args.pretooluse:
        return pretooluse(Path(args.knowledge_dir))
    problems = lint_bundle(Path(args.knowledge_dir))
    if problems:
        print(f"knowledge/ lint failed with {len(problems)} problem(s):",
              file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"knowledge/ lint clean ({Path(args.knowledge_dir)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
