"""Fetch layer: pull clean content for a URL, with a strategy chain.

Chain (first success wins, failures recorded, never fatal to the batch):
  1. `tvly extract` basic    -> markdown
  2. `tvly extract` advanced -> markdown
  3. direct HTTP GET         -> html

Every result carries the strategy used so provenance can note how it was
fetched. JS-rendered SPA pages often fail the whole chain; callers should
route those to a browser-based fetcher (e.g. Playwright MCP) when available.

fetch_many_with_state() wraps fetch_many() with persistent change detection
(see pipeline/state.py).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pipeline.state import update_state

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
           "--extract-depth", depth]
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


DEFAULT_STATE_PATH = Path(__file__).resolve().parents[1] / "state" / "fetch_state.json"


def fetch_many_with_state(
    urls: list[str],
    fetched_at: str,
    state_path: str | Path | None = None,
    fetcher: Callable[[list[str], str], list[dict]] = fetch_many,
) -> list[dict]:
    """fetch_many() + persistent change detection (see pipeline/state.py).

    Each result dict gains a `change` verdict: new | unchanged | changed |
    error. fetch_url()/fetch_many() themselves are unchanged.
    """
    results = fetcher(urls, fetched_at)
    verdicts = update_state(state_path or DEFAULT_STATE_PATH, results, fetched_at)
    return [{**result, "change": verdicts[result["url"]]} for result in results]


def main(
    argv: list[str] | None = None,
    state_path: str | Path | None = None,
    fetcher: Callable[[list[str], str], list[dict]] = fetch_many,
) -> int:
    """CLI entry point: python3 -m pipeline.fetch URL [URL ...]

    Prints one JSON object per URL. Individual URL failures show up as
    "ok": false in their JSON; the exit code stays 0. Non-zero exit means
    the CLI itself failed (bad arguments, unreadable state file, ...).
    """
    parser = argparse.ArgumentParser(
        description="Fetch URLs and record per-URL change state.")
    parser.add_argument("urls", nargs="+", metavar="URL",
                        help="one or more URLs to fetch")
    args = parser.parse_args(argv)

    fetched_at = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    results = fetch_many_with_state(args.urls, fetched_at,
                                    state_path=state_path, fetcher=fetcher)
    for r in results:
        print(json.dumps({
            "url": r["url"],
            "ok": r["ok"],
            "verdict": r["change"],
            "sha256": r.get("sha256"),
            "strategy": r.get("strategy"),
            "content_length": len(r["content"]) if "content" in r else None,
            "error": r.get("error"),
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())