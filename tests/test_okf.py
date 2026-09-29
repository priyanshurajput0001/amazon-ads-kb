"""Tests for the OKF frontmatter schema (pipeline/okf.py).

Thresholds and enums here are pinned deliberately: the expected values are
written out as literals, never derived from the module, so an accidental
schema change fails these tests instead of silently re-validating the bundle.
"""

import unittest
from pathlib import Path

from pipeline import okf

BASE_META = {
    "id": "sponsored-products-overview",
    "title": "Sponsored Products Overview",
    "type": "concept",
    "sources": ["https://advertising.amazon.com/API/docs/en-us"],
    "confidence": "high",
    "status": "official",
    "last_checked": "2026-09-26",
}


def doc_text(**overrides) -> str:
    meta = dict(BASE_META)
    meta.update(overrides)
    sources = meta.pop("sources")
    lines = ["---"]
    for key in ("id", "title", "type", "confidence", "status", "last_checked"):
        lines.append(f"{key}: {meta[key]}")
    lines.append("sources:")
    lines.extend(f"  - {url}" for url in sources)
    lines.append("---")
    return "\n".join(lines) + "\n\nBody text.\n"


class ParseTests(unittest.TestCase):
    def test_valid_document_with_type_parses(self):
        meta, body = okf.parse(doc_text())
        self.assertEqual(meta["type"], "concept")
        self.assertEqual(body, "Body text.")

    def test_missing_type_is_rejected(self):
        text = doc_text()
        text = text.replace("type: concept\n", "")
        with self.assertRaises(okf.OkfError) as ctx:
            okf.parse(text)
        self.assertIn("type", str(ctx.exception))

    def test_invalid_type_is_rejected(self):
        with self.assertRaises(okf.OkfError) as ctx:
            okf.parse(doc_text(type="fact"))
        self.assertIn("type", str(ctx.exception))

    def test_unknown_key_is_still_rejected(self):
        with self.assertRaises(okf.OkfError) as ctx:
            okf.parse(doc_text() .replace("type: concept",
                                          "type: concept\nnonsense: 1"))
        self.assertIn("unknown key", str(ctx.exception))

    def test_doc_types_enum_is_exactly_concept(self):
        # Pinned: widening the enum is a deliberate schema decision.
        self.assertEqual(okf.DOC_TYPES, ("concept",))

    def test_required_keys_include_type(self):
        self.assertEqual(
            okf.REQUIRED,
            {"id", "title", "type", "sources", "confidence", "status",
             "last_checked"})


class ValidateTests(unittest.TestCase):
    def test_validate_accepts_concept(self):
        okf.validate(dict(BASE_META))

    def test_validate_rejects_missing_type(self):
        meta = dict(BASE_META)
        del meta["type"]
        with self.assertRaises(okf.OkfError):
            okf.validate(meta)

    def test_validate_rejects_empty_type(self):
        with self.assertRaises(okf.OkfError):
            okf.validate(dict(BASE_META, type=""))

    def test_validate_rejects_uppercase_type(self):
        with self.assertRaises(okf.OkfError):
            okf.validate(dict(BASE_META, type="Concept"))


class SerializeTests(unittest.TestCase):
    def test_serialize_emits_type_in_canonical_order(self):
        text = okf.serialize(dict(BASE_META), "Body.")
        lines = text.splitlines()
        self.assertEqual(lines[0], "---")
        # Canonical order: id, title, type, sources, confidence, status,
        # last_checked — type sits directly after title.
        self.assertEqual(lines[1], "id: sponsored-products-overview")
        self.assertEqual(lines[2], "title: Sponsored Products Overview")
        self.assertEqual(lines[3], "type: concept")
        self.assertIn("sources:", lines[4:6])

    def test_serialize_without_type_raises(self):
        meta = dict(BASE_META)
        del meta["type"]
        with self.assertRaises(okf.OkfError):
            okf.serialize(meta, "Body.")

    def test_round_trip_preserves_type(self):
        text = okf.serialize(dict(BASE_META), "Body.")
        meta, body = okf.parse(text)
        self.assertEqual(meta["type"], "concept")
        self.assertEqual(body, "Body.")


class BundleLintTests(unittest.TestCase):
    """Every concept document in the real knowledge/ bundle must pass the OKF
    parser and validator: valid frontmatter, valid ids, valid sources, a
    `type`, no duplicate ids, INDEX links resolving both ways."""

    BUNDLE = Path(__file__).resolve().parents[1] / "knowledge"

    def test_every_document_is_valid_okf_with_type(self):
        docs = [p for p in self.BUNDLE.glob("*.md")
                if p.name not in ("INDEX.md", "CHANGELOG.md")]
        self.assertTrue(docs, "knowledge/ contains no concept documents")
        seen_ids = set()
        for path in sorted(docs):
            with self.subTest(doc=path.name):
                meta, _ = okf.parse(path.read_text(encoding="utf-8"), path)
                okf.validate(meta, path)
                self.assertEqual(meta["type"], "concept")
                self.assertNotIn(meta["id"], seen_ids,
                                 f"duplicate id {meta['id']}")
                seen_ids.add(meta["id"])

    def test_index_links_and_documents_are_consistent(self):
        index = (self.BUNDLE / "INDEX.md").read_text(encoding="utf-8")
        docs = {p.stem for p in self.BUNDLE.glob("*.md")
                if p.name not in ("INDEX.md", "CHANGELOG.md")}
        linked = {line.split("](./", 1)[1].split(".md)", 1)[0]
                  for line in index.splitlines() if "](./" in line}
        self.assertEqual(docs, linked,
                         "INDEX and knowledge/ documents must match exactly")

    def test_related_links_never_dangle(self):
        docs = {p.stem for p in self.BUNDLE.glob("*.md")
                if p.name not in ("INDEX.md", "CHANGELOG.md")}
        for path in sorted(self.BUNDLE.glob("*.md")):
            if path.name in ("INDEX.md", "CHANGELOG.md"):
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if "](./" in line and "## Related" not in line:
                    target = line.split("](./", 1)[1].split(".md)", 1)[0]
                    self.assertIn(
                        target, docs,
                        f"{path.name} links to missing document {target}")


if __name__ == "__main__":
    unittest.main()
