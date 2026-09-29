"""Discover stage: find NEW candidate URLs from what the pipeline already
fetched (review step 5 — the brief's stage 1).

    discover(seed_urls, state) ->
      1. read the links out of the cached pages of the seed URLs (markdown
         link syntax or raw http(s) URLs; HTML hrefs for html caches);
      2. optionally enrich with a `tvly search --json` query when
         TAVILY_API_KEY is set and the tvly CLI exists (a seam — fake it in
         tests; never called otherwise);
      3. keep only in-scope hosts: the official Amazon Ads docs host, the
         amzn GitHub org (github.com/amzn*, raw.githubusercontent.com/amzn*)
         — plus any host already deliberately ingested into fetch state;
      4. drop URLs already known to fetch_state.json (committed OR pending);
      5. return the candidates, de-duplicated, in a deterministic order
         (seed order, then first position within the page), capped
         (default 10).

Deterministic by construction: same caches + state -> same candidates. No
LLM. The orchestrator wires this in as an optional first stage
(`python3 -m pipeline.orchestrate --discover URL ...`).

CLI: python3 -m pipeline.discover URL [URL ...]   (prints the report JSON)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from pipeline.fetch import DEFAULT_CACHE_PATH, DEFAULT_STATE_PATH
from pipeline.state import load_state

logger = logging.getLogger("pipeline.discover")

DEFAULT_CAP = 10
SEARCH_TIMEOUT = 60

# In-scope hosts/prefixes: official Amazon Ads documentation, Amazon's GitHub
# org (pages and raw files). Anything else is out of scope unless a URL with
# the same host was already ingested deliberately (fetch state = the
# operator's past decisions).
OFFICIAL_HOSTS = ("advertising.amazon.com",)
OFFICIAL_URL_PREFIXES = (
    "https://github.com/amzn",          # org root, repos, subpaths
    "https://raw.githubusercontent.com/amzn",  # raw files of amzn repos
)

DEFAULT_QUERY = ("Amazon Ads API documentation site:advertising.amazon.com "
                 "OR site:github.com/amzn")

MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")
HREF_RE = re.compile(r"""href=["'](https?://[^"']+)["']""", re.IGNORECASE)
BARE_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


class DiscoverError(RuntimeError):
    """Discovery could not run (unreadable state, bad arguments)."""


def normalize_url(url: str) -> str:
    """Deterministic normalization: strip whitespace, drop #fragments, then
    drop a trailing slash (so /path and /path/ are one URL)."""
    url = url.strip().split("#", 1)[0]
    return url.rstrip("/")


def in_scope(url: str, state_urls: set[str]) -> bool:
    """Official host/prefix, or a host the operator already ingested."""
    try:
        host = url.split("//", 1)[-1].split("/", 1)[0].lower()
    except IndexError:
        return False
    if host in (h.lower() for h in OFFICIAL_HOSTS):
        return True
    if any(url.startswith(p) for p in OFFICIAL_URL_PREFIXES):
        return True
    known_hosts = {u.split("//", 1)[-1].split("/", 1)[0].lower()
                   for u in state_urls if "//" in u}
    return host in known_hosts


def extract_links(content: str, content_type: str) -> list[str]:
    """Links from one cached page, in order of appearance (position in the
    page — the page's own emphasis — with duplicates dropped at first
    sighting). Markdown caches: link syntax plus bare URLs; HTML caches:
    hrefs plus bare URLs."""
    found: list[tuple[int, str]] = []
    if content_type == "html":
        found.extend((m.start(), m.group(1)) for m in HREF_RE.finditer(content))
    else:
        found.extend((m.start(), m.group(1))
                     for m in MARKDOWN_LINK_RE.finditer(content))
    found.extend((m.start(), m.group(0)) for m in BARE_URL_RE.finditer(content))
    found.sort(key=lambda pair: pair[0])  # stable: page order wins
    seen: set[str] = set()
    out = []
    for _, url in found:
        url = normalize_url(url)
        if url and url.startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _cached_page(cache_dir: str | Path, sha: str) -> tuple[str, str] | None:
    """(content, content_type) of the cached page for a sha, or None."""
    for ext, kind in ((".md", "markdown"), (".html", "html")):
        path = Path(cache_dir) / f"{sha}{ext}"
        if path.exists():
            try:
                return path.read_text(encoding="utf-8"), kind
            except OSError:
                continue
    return None


def tvly_search_urls(query: str, max_results: int = DEFAULT_CAP) -> list[str]:
    """Optional search seam: tvly search --json. Only called by discover()
    when TAVILY_API_KEY is set and the tvly CLI exists. Fake in tests."""
    if not os.environ.get("TAVILY_API_KEY"):
        return []
    if shutil.which("tvly") is None:
        return []
    env = dict(os.environ)
    env["PATH"] = env.get("PATH", "") + os.pathsep + os.path.expanduser(
        "~/.local/bin")
    try:
        proc = subprocess.run(
            ["tvly", "search", query, "--json", "--max-results",
             str(max_results)],
            capture_output=True, text=True, timeout=SEARCH_TIMEOUT + 30,
            env=env)
    except subprocess.TimeoutExpired:
        logger.warning("tvly search timed out after %ss; skipped",
                       SEARCH_TIMEOUT + 30)
        return []
    if proc.returncode != 0:
        logger.warning("tvly search exited %d; skipped", proc.returncode)
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning("tvly search returned invalid JSON; skipped")
        return []
    results = data.get("results") if isinstance(data, dict) else data
    if not isinstance(results, list):
        return []
    return [r.get("url", "") for r in results if isinstance(r, dict)]


def discover(
    seed_urls: list[str],
    state_path: str | Path | None = None,
    cache_path: str | Path | None = None,
    search=None,
    cap: int = DEFAULT_CAP,
) -> dict:
    """Return the discovery report: candidate URLs (new, in-scope, capped,
    deterministic) plus what was scanned and skipped. `search` is the
    optional tvly seam (default: the real CLI behind the TAVILY_API_KEY
    guard); pass a fake in tests."""
    if not seed_urls:
        raise DiscoverError("no seed URLs given")
    state_path = state_path or DEFAULT_STATE_PATH
    cache_path = cache_path or DEFAULT_CACHE_PATH
    search = search or tvly_search_urls

    states = load_state(state_path)
    known = {normalize_url(u) for u in states}
    known_pending = {normalize_url(u) for u, s in states.items()
                     if s.pending_sha256}
    state_urls = set(states)

    candidates: list[str] = []
    seen: set[str] = set()
    seeds_scanned: list[str] = []
    skipped_known = 0
    skipped_scope = 0

    def offer(url: str) -> None:
        """Deterministic candidate admission (first-seen order wins)."""
        nonlocal skipped_known, skipped_scope
        url = normalize_url(url)
        if not url or url in seen:
            return
        seen.add(url)
        if url in known:
            skipped_known += 1
            return
        if not in_scope(url, state_urls):
            skipped_scope += 1
            return
        candidates.append(url)

    for seed in seed_urls:
        entry = states.get(seed) or states.get(normalize_url(seed))
        sha = entry.current_content_sha256 if entry else None
        page = _cached_page(cache_path, sha) if sha else None
        if page is None:
            logger.info("seed %s has no cached page; skipped", seed)
            continue
        content, kind = page
        seeds_scanned.append(seed)
        for url in extract_links(content, kind):
            offer(url)

    search_used = False
    search_urls = []
    if callable(search):
        try:
            search_urls = search(DEFAULT_QUERY, cap) or []
            search_used = bool(search_urls)
        except Exception as exc:  # the search seam is optional, never fatal
            logger.warning("search seam failed (%s); skipped", exc)
    for url in search_urls:
        offer(url)

    return {
        "candidates": candidates[:cap],
        "seeds_scanned": seeds_scanned,
        "search_used": search_used,
        "skipped": {"already_in_state": skipped_known,
                    "out_of_scope": skipped_scope},
        "cap": cap,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: python3 -m pipeline.discover URL [URL ...]

    Prints the discovery report as JSON. Exit 0 even with zero candidates
    (an empty frontier is a result); exit 2 on usage errors.
    """
    parser = argparse.ArgumentParser(
        description="Discover new candidate URLs from cached official "
                    "pages (deterministic; optional tvly search when "
                    "TAVILY_API_KEY is set).")
    parser.add_argument("urls", nargs="+", metavar="URL",
                        help="seed URLs whose cached pages are scanned")
    parser.add_argument("--cap", type=int, default=DEFAULT_CAP,
                        help=f"maximum candidates returned "
                             f"(default {DEFAULT_CAP})")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        report = discover(args.urls, cap=args.cap)
    except DiscoverError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
