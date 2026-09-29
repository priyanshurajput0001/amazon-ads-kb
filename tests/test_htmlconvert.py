"""Tests for the stdlib HTML->Markdown converter (pipeline/htmlconvert.py)
— offline, fixture HTML only (review step 6)."""

import unittest

from pipeline.htmlconvert import convertible, html_to_markdown

FIXTURE = """<!DOCTYPE html>
<html>
<head>
<title>Amazon Ads API release notes</title>
<script>var tracking = "nope"; window.boot();</script>
<style>body { color: red; }</style>
</head>
<body>
<nav><a href="/menu">Menu</a><a href="/signin">Sign in</a></nav>
<header>Amazon Ads global header banner</header>
<main>
<h1>Amazon Ads API release notes</h1>
<p>The Amazon Ads API version 2 is now generally available. See the
<a href="https://advertising.amazon.com/API/docs/en-us">API documentation</a>
for details.</p>
<h2>Changes</h2>
<ul>
<li>Added asynchronous report requests.</li>
<li>Deprecated the snapshots APIs.
  <ul><li>Use export APIs instead.</li></ul>
</li>
</ul>
<table>
<tr><th>Version</th><th>Date</th></tr>
<tr><td>v2</td><td>2026-08-01</td></tr>
</table>
</main>
<footer>Copyright Amazon.com, Inc.</footer>
</body>
</html>"""


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.md = html_to_markdown(FIXTURE)

    def test_headings_kept_with_levels(self):
        self.assertIn("# Amazon Ads API release notes", self.md)
        self.assertIn("## Changes", self.md)

    def test_paragraph_text_kept(self):
        self.assertIn("The Amazon Ads API version 2 is now generally "
                      "available.", self.md)

    def test_links_kept_as_markdown(self):
        self.assertIn(
            "[API documentation]"
            "(https://advertising.amazon.com/API/docs/en-us)", self.md)

    def test_lists_kept_with_nesting_indent(self):
        self.assertIn("- Added asynchronous report requests.", self.md)
        self.assertIn("- Deprecated the snapshots APIs.", self.md)
        self.assertIn("  - Use export APIs instead.", self.md)

    def test_tables_as_text_rows(self):
        self.assertIn("| Version | Date |", self.md)
        self.assertIn("| v2 | 2026-08-01 |", self.md)

    def test_script_style_nav_header_footer_dropped(self):
        for noise in ("tracking", "color: red", "Menu", "Sign in",
                      "global header banner", "Copyright Amazon"):
            self.assertNotIn(noise, self.md)

    def test_deterministic(self):
        self.assertEqual(self.md, html_to_markdown(FIXTURE))


class XmlFeedTests(unittest.TestCase):
    """Release-notes RSS feeds arrive as text/xml (Amazon's CDN) and convert
    through the same stdlib path as HTML."""

    RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Amazon Ads API Release Notes</title>
<link>https://advertising.amazon.com/API/docs/en-us/info/release-notes</link>
<description>Description of updates to the Amazon Ads API.</description>
<copyright>Copyright 2015-2025, Amazon</copyright>
<item>
<title>Inventory Management Unified APIs now generally available</title>
<description>Unified programmatic access to inventory-related data.</description>
<pubDate>Tue, 29 Sep 2026 19:02:58 GMT</pubDate>
</item>
<item>
<title>Reporting API v2 open beta</title>
<description>Multi-dimensional reporting with standardized metrics.</description>
</item>
</channel></rss>"""

    def test_rss_converts_to_readable_text(self):
        md = html_to_markdown(self.RSS)
        self.assertIn("Amazon Ads API Release Notes", md)
        self.assertIn("Inventory Management Unified APIs now generally "
                      "available", md)
        self.assertIn("Reporting API v2 open beta", md)
        self.assertIn("Copyright 2015-2025, Amazon", md)

    def test_rss_is_convertible(self):
        self.assertTrue(convertible(self.RSS))


class ConvertibleTests(unittest.TestCase):
    def test_rich_page_is_convertible(self):
        self.assertTrue(convertible(FIXTURE))

    def test_js_shell_page_is_not_convertible(self):
        shell = ("<html><head><script>boot();</script></head>"
                 "<body><div id=\"root\"></div></body></html>")
        self.assertFalse(convertible(shell))

    def test_empty_body_is_not_convertible(self):
        self.assertFalse(convertible("<html><body></body></html>"))

    def test_tiny_body_below_threshold_is_not_convertible(self):
        self.assertFalse(convertible("<html><body>x</body></html>"))


if __name__ == "__main__":
    unittest.main()
