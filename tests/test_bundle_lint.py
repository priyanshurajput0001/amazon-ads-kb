"""Bundle lint over the LIVE knowledge/ directory (review step 3).

The maintained bundle is part of the contract, not just test fixture data:
these tests fail when the published bundle drifts from the OKF + topic
contract — invalid frontmatter, missing type, duplicate ids or titles,
dangling Related links, INDEX rows that do not match the files, or a
document count outside the 10-15 topic taxonomy window.
"""

import unittest
from pathlib import Path

from pipeline import okf
from pipeline.concepts import related_link_targets
from pipeline.rebuild import lint_bundle

KNOWLEDGE = Path(__file__).resolve().parents[1] / "knowledge"
MIN_DOCS, MAX_DOCS = 10, 15


def concept_docs() -> list[Path]:
    return [p for p in sorted(KNOWLEDGE.glob("*.md"))
            if p.name not in ("INDEX.md", "CHANGELOG.md")]


class LiveBundleLintTests(unittest.TestCase):
    def test_bundle_lint_is_clean(self):
        self.assertEqual(lint_bundle(KNOWLEDGE), [])

    def test_every_document_is_valid_okf_concept(self):
        docs = concept_docs()
        self.assertTrue(docs, "knowledge/ contains no concept documents")
        for path in docs:
            meta, body = okf.parse(path.read_text(encoding="utf-8"), path)
            self.assertEqual(meta["type"], "concept", path.name)
            self.assertEqual(meta["id"], path.stem, path.name)
            self.assertTrue(meta["sources"], path.name)

    def test_document_count_within_topic_window(self):
        self.assertGreaterEqual(len(concept_docs()), MIN_DOCS)
        self.assertLessEqual(len(concept_docs()), MAX_DOCS)

    def test_every_related_link_resolves(self):
        stems = {p.stem for p in concept_docs()}
        for path in concept_docs():
            _, body = okf.parse(path.read_text(encoding="utf-8"))
            for target in related_link_targets(body):
                self.assertIn(target, stems,
                              f"{path.name} links to missing ./{target}.md")

    def test_ids_and_titles_unique(self):
        ids = [p.stem for p in concept_docs()]
        titles = []
        for path in concept_docs():
            meta, _ = okf.parse(path.read_text(encoding="utf-8"))
            titles.append(meta["title"])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(titles), len(set(titles)))


if __name__ == "__main__":
    unittest.main()
