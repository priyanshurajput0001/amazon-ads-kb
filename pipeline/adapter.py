"""Adapter stage: turn Extractor claim files + fetch state into Validator facts.

Deterministic glue between the Extractor and the Validator — no LLM, no
network, no clock reads. Upstream stages stay untouched; this module only
reshapes what they already recorded:

    state/claims/<sha256>.json   (per content version, written by the Extractor)
    + the URL's fetch-state entry (written by the Fetcher)
    -> one Validator fact per claim

Field mapping (explicit, nothing invented):
  content      the Extractor claim, verbatim
  url / date   the source URL and its fetch time from fetch state
  source_type  explicit URL rule: advertising.amazon.com pages and the
               github.com/amzn org are official; everything else is community
               (OFFICIAL_HOSTS / OFFICIAL_URL_PREFIXES constants)
  is_changed   "N" with last_run only when a fetch AFTER the claims were
               extracted re-saw the SAME sha256 — i.e. the content survived a
               later run unchanged (fetch state keeps no verdict history, so
               this is the honest derivation from what is stored). Otherwise
               "Y" with no last_run: first sighting of this content version.
  topic_id     the Extractor's topic_hint (its advisory slug) promoted to a
               grouping key for Validator/Merger; the original topic_hint is
               preserved alongside
  community_agree_count  always 0 — no pipeline stage records people-agreement
               data yet; it is defaulted, never fabricated

Extractor provenance (quote, confidence, topic_hint, sha256, extracted_at)
rides along unchanged for auditing; the Validator ignores fields it does not
score and passes everything through.

Failures are reported, never papered over: a URL missing from fetch state, a
never-successfully-fetched URL, or a missing/unreadable claims file for the
current content version yields an error entry with zero facts.

CLI: python3 -m pipeline.adapter [URL ...]   (no URLs: every URL in state)
Per-URL results are logged to stderr; the combined facts array is printed to
stdout as JSON, ready to pipe into `python3 -m pipeline.validator -`.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys
from pathlib import Path

from pipeline.extractor import DEFAULT_CLAIMS_PATH
from pipeline.fetch import DEFAULT_STATE_PATH
from pipeline.state import SourceState, load_state

logger = logging.getLogger("pipeline.adapter")  # stable name when run as -m

OFFICIAL_HOSTS = ("advertising.amazon.com",)
OFFICIAL_URL_PREFIXES = ("https://github.com/amzn",)  # amzn org root and subpaths


class AdapterError(ValueError):
    """An input violates the Adapter contract (bad claims doc shape)."""


def classify_source_type(
    url: str,
    official_hosts: tuple[str, ...] = OFFICIAL_HOSTS,
    official_url_prefixes: tuple[str, ...] = OFFICIAL_URL_PREFIXES,
) -> str:
    """Explicit, dumb-on-purpose source classification. No heuristics."""
    host = url.split("//", 1)[-1].split("/", 1)[0].lower()
    if host in (h.lower() for h in official_hosts):
        return "official"
    if any(url == p or url.startswith(p.rstrip("/") + "/")
           for p in official_url_prefixes):
        return "official"
    return "community"


def _parse_dt(value: object) -> datetime.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


def _derive_change(entry: SourceState, doc: dict) -> tuple[str, str | None]:
    """(is_changed, last_run). "N" only when a fetch after extraction re-saw
    the same sha256 — the content version survived a later run unchanged."""
    fetched = _parse_dt(entry.pending_fetched_at or entry.fetched_at)
    extracted_run = _parse_dt(doc.get("fetched_at"))
    same_content = (entry.current_content_sha256 is not None
                    and entry.current_content_sha256 == doc.get("sha256"))
    if (fetched is not None and extracted_run is not None
            and fetched > extracted_run and same_content):
        return "N", doc.get("fetched_at")
    return "Y", None


def build_facts(
    doc: dict,
    entry: SourceState,
    official_hosts: tuple[str, ...] = OFFICIAL_HOSTS,
    official_url_prefixes: tuple[str, ...] = OFFICIAL_URL_PREFIXES,
) -> list[dict]:
    """Pure transformation: one claims doc + its fetch state -> Validator facts."""
    claims = doc.get("claims")
    if not isinstance(claims, list):
        raise AdapterError("claims doc has no 'claims' list")
    source_type = classify_source_type(entry.url, official_hosts,
                                       official_url_prefixes)
    is_changed, last_run = _derive_change(entry, doc)
    facts: list[dict] = []
    for i, claim in enumerate(claims):
        text = claim.get("claim") if isinstance(claim, dict) else None
        if not isinstance(text, str) or not text.strip():
            logger.warning("skipping malformed claim[%d] for %s", i, entry.url)
            continue
        facts.append({
            "url": entry.url,
            "date": entry.pending_fetched_at or entry.fetched_at,
            "content": text,
            "is_changed": is_changed,
            "last_run": last_run,
            "source_type": source_type,
            "community_agree_count": 0,
            "topic_id": claim.get("topic_hint") or None,
            # Extractor provenance, preserved unchanged for auditing:
            "quote": claim.get("quote"),
            "confidence": claim.get("confidence"),
            "topic_hint": claim.get("topic_hint"),
            "sha256": doc.get("sha256"),
            "extracted_at": doc.get("extracted_at"),
        })
    return facts


def adapt_url(
    url: str,
    states: dict[str, SourceState],
    claims_dir: str | Path,
    official_hosts: tuple[str, ...] = OFFICIAL_HOSTS,
    official_url_prefixes: tuple[str, ...] = OFFICIAL_URL_PREFIXES,
) -> dict:
    """Adapt one URL; returns {url, status, facts|error} like the other stages."""
    entry = states.get(url)
    if entry is None:
        return {"url": url, "status": "error",
                "error": "URL not found in fetch state (never fetched)"}
    sha = entry.current_content_sha256
    if not sha:
        return {"url": url, "status": "error",
                "error": "no content hash in fetch state (never successfully fetched)"}
    claims_file = Path(claims_dir) / f"{sha}.json"
    if not claims_file.exists():
        return {"url": url, "status": "error",
                "error": (f"no claims extracted for current content version "
                          f"({sha[:12]}...) — run the Extractor first")}
    try:
        doc = json.loads(claims_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"url": url, "status": "error",
                "error": f"corrupt claims file: {exc}"}
    if doc.get("status") != "ok" or not isinstance(doc.get("claims"), list):
        return {"url": url, "status": "error",
                "error": f"claims doc for {sha[:12]}... is not a valid ok extraction"}
    try:
        facts = build_facts(doc, entry, official_hosts, official_url_prefixes)
    except AdapterError as exc:
        return {"url": url, "status": "error", "error": str(exc)}
    logger.info("adapted %s: %d facts (source_type=%s, is_changed=%s)",
                url, len(facts), facts[0]["source_type"] if facts else "-",
                facts[0]["is_changed"] if facts else "-")
    return {"url": url, "status": "ok", "fact_count": len(facts), "facts": facts}


def main(
    argv: list[str] | None = None,
    state_path: str | Path | None = None,
    claims_path: str | Path | None = None,
) -> int:
    """CLI entry point: python3 -m pipeline.adapter [URL ...]

    Logs one result line per URL to stderr and prints the combined facts
    array to stdout (chainable with the Validator's '-' stdin). Exit 0 when
    everything adapted; 2 when nothing could be adapted or state is corrupt.
    Individual failures never stop the batch — they are logged and skipped.
    """
    parser = argparse.ArgumentParser(
        description="Convert Extractor claims + fetch state into Validator facts.")
    parser.add_argument("urls", nargs="*", metavar="URL",
                        help="URLs to adapt (default: every URL in fetch state)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    states = load_state(state_path or DEFAULT_STATE_PATH)
    urls = args.urls or list(states)
    results = [adapt_url(url, states, claims_path or DEFAULT_CLAIMS_PATH)
               for url in urls]

    ok = [r for r in results if r["status"] == "ok"]
    for r in results:
        if r["status"] == "error":
            logger.warning("adapt failed for %s: %s", r["url"], r["error"])
    if results and not ok:
        print(json.dumps([{k: v for k, v in r.items() if k != "facts"}
                          for r in results], indent=2, ensure_ascii=False))
        return 2
    facts = [f for r in ok for f in r["facts"]]
    logger.info("adapter: %d/%d URLs ok, %d facts total",
                len(ok), len(results), len(facts))
    print(json.dumps(facts, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
