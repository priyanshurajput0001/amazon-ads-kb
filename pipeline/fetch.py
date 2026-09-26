"""Fetch layer: pull clean content for a URL, with a strategy chain.

Chain (first success wins, failures recorded, never fatal to the batch):
  1. `tvly extract` basic    -> markdown
  2. `tvly extract` advanced -> markdown
  3. direct HTTP GET         -> html

Every result carries the strategy used so provenance can note how it was
fetched. JS-rendered SPA pages often fail the whole chain; callers should
route those to a browser-based fetcher (e.g. Playwright MCP) when available.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass

TVLY_TIMEOUT = 60  # seconds, matches --timeout upper bound
DIRECT_TIMEOUT = 30
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
)


class FetchError(Exception):
    """All strategies failed for a URL."""


@dataclass
class FetchResult:
    url: str
    strategy: str  # tvly-basic | tvly-advanced | direct
    content_type: str  # markdown | html
    title: str | None
    content: str
    sha256: str
    fetched_at: str  # ISO-8601 UTC instant

    def to_json(self) -> dict:
        return {
            "url": self.url,
            "strategy": self.strategy,
            "content_type": self.content_type,
            "title": self.title,
            "sha256": self.sha256,
            "fetched_at": self.fetched_at,
            "content": self.content,
        }


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tvly_extract(url: str, depth: str, fetched_at: str) -> FetchResult | None:
    if shutil.which("tvly") is None:
        return None
    cmd = ["tvly", "extract", url, "--format", "markdown", "--json",
           "--extract-depth", depth, "--timeout", str(TVLY_TIMEOUT)]
    env = dict(os.environ)
    env["PATH"] = env.get("PATH", "") + os.pathsep + os.path.expanduser("~/.local/bin")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TVLY_TIMEOUT + 30, env=env)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    results = data.get("results") or []
    if not results or not (results[0].get("raw_content") or "").strip():
        return None
    content = results[0]["raw_content"]
    return FetchResult(url=url, strategy=f"tvly-{depth}", content_type="markdown",
                       title=results[0].get("title"), content=content,
                       sha256=_sha256(content), fetched_at=fetched_at)


def _direct(url: str, fetched_at: str) -> FetchResult | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=DIRECT_TIMEOUT) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            content = resp.read().decode(charset, errors="replace")
    except (OSError, urllib.error.URLError, ValueError):
        return None
    if not content.strip():
        return None
    return FetchResult(url=url, strategy="direct", content_type="html", title=None,
                       content=content, sha256=_sha256(content), fetched_at=fetched_at)


def fetch_url(url: str, fetched_at: str, depths: tuple[str, ...] = ("basic", "advanced")) -> FetchResult:
    """Fetch one URL through the strategy chain. Raises FetchError on total failure."""
    for depth in depths:
        result = _tvly_extract(url, depth, fetched_at)
        if result is not None:
            return result
    result = _direct(url, fetched_at)
    if result is not None:
        return result
    raise FetchError(url)


def fetch_many(urls: list[str], fetched_at: str) -> list[dict]:
    """Fetch a batch; per-URL failures come back as {"url", "error", "ok": False}."""
    out = []
    for url in urls:
        try:
            out.append(fetch_url(url, fetched_at).to_json() | {"ok": True})
        except FetchError:
            out.append({"url": url, "ok": False, "error": "all fetch strategies failed"})
    return out
