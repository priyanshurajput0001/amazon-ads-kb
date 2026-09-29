"""Tests for pipeline/discover — the optional first stage. Fully offline:
the tvly search seam is a stub, links come from fixture caches."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from pipeline.discover import (
    DEFAULT_CAP,
    DiscoverError,
    discover,
    extract_links,
    in_scope,
    normalize_url,
)

T0 = "2026-09-26T12:00:00+00:00"

SEED = "https://advertising.amazon.com/API/docs/en-us"
PAGE = """# Amazon Ads API docs

See the [guides overview](https://advertising.amazon.com/API/docs/en-us/guides/overview)
and the [release notes](https://advertising.amazon.com/API/docs/en-us/info/release-notes).
Community mirror: https://blog.example.com/ads-api-guide
Back to [start](https://advertising.amazon.com/API/docs/en-us)
Official samples: [amzn org](https://github.com/amzn)
Raw readme: https://raw.githubusercontent.com/amzn/ads-advanced-tools-docs/main/README.md
"""


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def no_search(query, cap):
    raise AssertionError("search seam must not run when not requested")


class HelperTests(unittest.TestCase):
    def test_normalize_strips_fragment_and_trailing_slash(self):
        self.assertEqual(normalize_url("https://a.example/x/#frag "),
                         "https://a.example/x")

    def test_in_scope_hosts(self):
        state = {"https://advertising.amazon.com/a"}
        self.assertTrue(in_scope("https://advertising.amazon.com/x", state))
        self.assertTrue(in_scope("https://github.com/amzn/some-repo", state))
        self.assertTrue(in_scope(
            "https://raw.githubusercontent.com/amzn/r/main/README.md", state))
        self.assertFalse(in_scope("https://blog.example.com/x", state))
        # a host the operator already ingested deliberately stays in scope
        self.assertTrue(in_scope("https://developer.amazon.com/y",
                                 state | {"https://developer.amazon.com/z"}))

    def test_extract_links_markdown_ordered_and_deduped(self):
        links = extract_links(PAGE, "markdown")
        self.assertEqual(links, [
            "https://advertising.amazon.com/API/docs/en-us/guides/overview",
            "https://advertising.amazon.com/API/docs/en-us/info/release-notes",
            "https://blog.example.com/ads-api-guide",
            "https://advertising.amazon.com/API/docs/en-us",  # dup removed
            "https://github.com/amzn",
            "https://raw.githubusercontent.com/amzn/ads-advanced-tools-docs/main/README.md",
        ])

    def test_extract_links_html(self):
        html = ('<a href="https://advertising.amazon.com/a">a</a> '
                '<a href="https://advertising.amazon.com/a">dup</a> '
                '<a href="https://github.com/amzn">org</a>')
        self.assertEqual(extract_links(html, "html"), [
            "https://advertising.amazon.com/a", "https://github.com/amzn"])


class DiscoverTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.state = base / "fetch_state.json"
        self.cache = base / "cache"
        self.cache.mkdir()
        # seed URL committed with a cached page
        self.state.write_text(json.dumps({
            SEED: {"status": "ok", "fetched_at": T0, "sha256": sha(PAGE),
                   "strategy": "tvly-basic"},
            # already ingested — must not be re-discovered
            "https://advertising.amazon.com/API/docs/en-us/guides/overview": {
                "status": "ok", "fetched_at": T0, "sha256": "old",
                "strategy": "tvly-basic"},
        }), encoding="utf-8")
        (self.cache / f"{sha(PAGE)}.md").write_text(PAGE, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_filters_scope_dedups_state_and_orders_deterministically(self):
        report = discover([SEED], state_path=self.state, cache_path=self.cache,
                          search=no_search)
        self.assertEqual(report["seeds_scanned"], [SEED])
        # guides/overview is already in state; the blog mirror is out of
        # scope; the page's self-link IS the seed (a state key) -> skipped.
        self.assertEqual(report["candidates"], [
            "https://advertising.amazon.com/API/docs/en-us/info/release-notes",
            "https://github.com/amzn",
            "https://raw.githubusercontent.com/amzn/ads-advanced-tools-docs/main/README.md",
        ])
        self.assertEqual(report["skipped"]["already_in_state"], 2)
        self.assertEqual(report["skipped"]["out_of_scope"], 1)
        self.assertFalse(report["search_used"])
        # deterministic: identical inputs -> identical candidates
        again = discover([SEED], state_path=self.state, cache_path=self.cache,
                         search=no_search)
        self.assertEqual(again["candidates"], report["candidates"])

    def test_cap_limits_candidates_in_first_seen_order(self):
        report = discover([SEED], state_path=self.state, cache_path=self.cache,
                          search=no_search, cap=2)
        self.assertEqual(len(report["candidates"]), 2)
        self.assertEqual(report["candidates"][:2], [
            "https://advertising.amazon.com/API/docs/en-us/info/release-notes",
            "https://github.com/amzn"])

    def test_pending_urls_are_also_dropped(self):
        self.state.write_text(json.dumps({
            SEED: {"status": "ok", "fetched_at": T0, "sha256": "old",
                   "strategy": "tvly-basic",
                   "pending_sha256": sha(PAGE),
                   "pending_fetched_at": T0},
        }), encoding="utf-8")
        report = discover([SEED], state_path=self.state, cache_path=self.cache,
                          search=no_search)
        # the pending seed is still scanned via its pending hash; the page's
        # self-link equals the seed URL (a state key) -> never a candidate
        self.assertEqual(report["seeds_scanned"], [SEED])
        self.assertNotIn(SEED, report["candidates"])

    def test_search_seam_results_filtered_and_merged_after_page_links(self):
        def fake_search(query, cap):
            assert "advertising.amazon.com" in query
            return ["https://advertising.amazon.com/API/docs/en-us/news",
                    "https://advertising.amazon.com/API/docs/en-us",  # dup
                    "https://random.example.com/ads"]  # out of scope

        report = discover([SEED], state_path=self.state, cache_path=self.cache,
                          search=fake_search)
        self.assertTrue(report["search_used"])
        self.assertIn(
            "https://advertising.amazon.com/API/docs/en-us/news",
            report["candidates"])
        self.assertNotIn("https://random.example.com/ads",
                         report["candidates"])

    def test_seed_without_cache_is_skipped_not_fatal(self):
        report = discover(["https://advertising.amazon.com/never-fetched"],
                          state_path=self.state, cache_path=self.cache,
                          search=no_search)
        self.assertEqual(report["seeds_scanned"], [])
        self.assertEqual(report["candidates"], [])

    def test_no_seeds_is_a_usage_error(self):
        with self.assertRaises(DiscoverError):
            discover([], state_path=self.state, cache_path=self.cache)


if __name__ == "__main__":
    unittest.main()
