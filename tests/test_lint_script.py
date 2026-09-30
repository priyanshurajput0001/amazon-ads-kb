"""Tests for scripts/lint_bundle.py — the bundle lint + its PreToolUse hook.

Runs the script as a subprocess (exactly as the hook does) against fixture
bundles and the real knowledge/ directory. No network, no LLM.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "lint_bundle.py"

DOC = """---
id: {slug}
title: {title}
type: concept
sources:
  - https://advertising.amazon.com/x
confidence: high
status: official
last_checked: 2026-09-30
---

# {title}

## Details

### Facts

- A claim about {subject}.
  - confidence_score: 0.60
  - status: valid
  - resolution: single_source
  - first_seen: 2026-09-30
  - sources: https://advertising.amazon.com/x (official, fetched 2026-09-30T00:00:00+00:00)

## Sources

- https://advertising.amazon.com/x — official, fetched 2026-09-30T00:00:00+00:00 (confirmed 1 fact)
"""

INDEX = """# Knowledge Index

| Concept | Title | Type | Status | Confidence | Last checked |
|---------|-------|------|--------|------------|--------------|
| [alpha-topic](./alpha-topic.md) | Alpha Topic | concept | official | high | 2026-09-30 |
"""


def run_script(args, stdin=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT)] + args, capture_output=True, text=True,
        input=stdin, cwd=REPO, timeout=60)


def build_bundle(root: Path, docs: dict[str, str], index: str = INDEX) -> Path:
    kdir = root / "knowledge"
    kdir.mkdir(parents=True)
    for name, text in docs.items():
        (kdir / name).write_text(text, encoding="utf-8")
    (kdir / "INDEX.md").write_text(index, encoding="utf-8")
    (kdir / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    return kdir


class FullLintModeTests(unittest.TestCase):
    def test_clean_bundle_lints_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            kdir = build_bundle(Path(tmp),
                                {"alpha-topic.md": DOC.format(
                                    slug="alpha-topic", title="Alpha Topic",
                                    subject="alpha")})
            result = run_script(["--knowledge-dir", str(kdir)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("lint clean", result.stdout)

    def test_invalid_bundle_exits_nonzero_and_names_problems(self):
        with tempfile.TemporaryDirectory() as tmp:
            kdir = build_bundle(
                Path(tmp),
                {"alpha-topic.md": DOC.format(slug="WRONG-ID", title="T",
                                              subject="alpha"),
                 "stale.md": "no frontmatter at all"})
            result = run_script(["--knowledge-dir", str(kdir)])
        self.assertEqual(result.returncode, 1)
        self.assertIn("WRONG-ID", result.stderr)
        self.assertIn("stale.md", result.stderr)

    def test_real_repo_bundle_frontmatter_valid(self):
        """The maintained bundle must always pass the document-level lint
        (INDEX/date consistency is covered by test_bundle_lint; this pins
        that no document in knowledge/ is malformed)."""
        result = run_script([])
        problems = [line for line in result.stderr.splitlines()
                    if line.strip().startswith("- ")
                    and "INDEX" not in line]
        self.assertEqual(problems, [], result.stderr)


class PreToolUseHookTests(unittest.TestCase):
    def hook(self, kdir, payload):
        return run_script(["--pretooluse", "--knowledge-dir", str(kdir)],
                          stdin=json.dumps(payload))

    def test_valid_concept_write_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            kdir = build_bundle(Path(tmp), {"alpha-topic.md": DOC.format(
                slug="alpha-topic", title="Alpha Topic", subject="alpha")})
            payload = {"tool_name": "Write",
                       "tool_input": {
                           "file_path": str(kdir / "beta-topic.md"),
                           "content": DOC.format(slug="beta-topic",
                                                 title="Beta Topic",
                                                 subject="beta")}}
            # INDEX deliberately not updated: a mid-batch write may lag it
            result = self.hook(kdir, payload)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_malformed_write_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            kdir = build_bundle(Path(tmp), {"alpha-topic.md": DOC.format(
                slug="alpha-topic", title="Alpha Topic", subject="alpha")})
            payload = {"tool_name": "Write",
                       "tool_input": {"file_path": str(kdir / "broken.md"),
                                      "content": "not an okf document"}}
            result = self.hook(kdir, payload)
        self.assertEqual(result.returncode, 2)
        self.assertIn("broken.md", result.stderr)

    def test_id_filename_mismatch_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            kdir = build_bundle(Path(tmp), {"alpha-topic.md": DOC.format(
                slug="alpha-topic", title="Alpha Topic", subject="alpha")})
            payload = {"tool_name": "Write",
                       "tool_input": {
                           "file_path": str(kdir / "gamma.md"),
                           "content": DOC.format(slug="different-id",
                                                 title="Gamma",
                                                 subject="gamma")}}
            result = self.hook(kdir, payload)
        self.assertEqual(result.returncode, 2)
        self.assertIn("different-id", result.stderr)

    def test_dangling_related_link_is_blocked(self):
        doc = DOC.format(slug="alpha-topic", title="Alpha Topic",
                         subject="alpha") + (
            "\n## Related\n\n- [Nope](./nope.md)\n")
        with tempfile.TemporaryDirectory() as tmp:
            kdir = build_bundle(Path(tmp), {"alpha-topic.md": doc})
            payload = {"tool_name": "Write",
                       "tool_input": {
                           "file_path": str(kdir / "alpha-topic.md"),
                           "content": doc}}
            result = self.hook(kdir, payload)
        self.assertEqual(result.returncode, 2)
        self.assertIn("dangling link", result.stderr)

    def test_edit_mode_applies_the_patch_before_linting(self):
        with tempfile.TemporaryDirectory() as tmp:
            kdir = build_bundle(Path(tmp), {"alpha-topic.md": DOC.format(
                slug="alpha-topic", title="Alpha Topic", subject="alpha")})
            payload = {"tool_name": "Edit",
                       "tool_input": {
                           "file_path": str(kdir / "alpha-topic.md"),
                           "old_string": "title: Alpha Topic",
                           "new_string": "title: Alpha Topic OK"}}
            result = self.hook(kdir, payload)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_writes_outside_knowledge_are_ignored(self):
        payload = {"tool_name": "Write",
                   "tool_input": {"file_path": "/tmp/anything.md",
                                  "content": "junk"}}
        result = run_script(["--pretooluse"], stdin=json.dumps(payload))
        self.assertEqual(result.returncode, 0)

    def test_unreadable_payload_is_blocked(self):
        # A payload that cannot be parsed cannot be proven to target a path
        # outside knowledge/, so the hook fails CLOSED (exit 2) with a reason.
        result = run_script(["--pretooluse"], stdin="not-json{{{")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unreadable payload", result.stderr)
        self.assertIn("blocking", result.stderr)

    def test_non_write_tools_with_valid_payload_still_pass(self):
        # Valid JSON naming a tool this hook does not guard -> exit 0.
        result = run_script(["--pretooluse"],
                            stdin=json.dumps({"tool_name": "Bash",
                                              "tool_input": {"command": "ls"}}))
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
