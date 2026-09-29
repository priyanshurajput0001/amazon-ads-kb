"""Orchestration driver: sequence the existing pipeline stages for URLs.

Claude Code is the top-level orchestrator: it parses the user's request
("ingest <url>, update the bundle"), invokes this driver as ONE command, and
relays the JSON report. This module contains NO pipeline logic — every step
below is a call into an existing stage; the only added value is sequencing
and the short-circuits the contract requires:

    fetch verdict unchanged   -> STOP that URL (nothing downstream runs at all)
    fetch failed / HTML cache -> STOP that URL, report the reason honestly
    new or changed markdown   -> Extract -> Adapter -> Validator -> Merger
                                  (matched against the existing bundle)
                                  -> Publisher

Fetch-state safety: the Fetch stage stages a new content hash as PENDING;
this driver commits it only AFTER the Publisher succeeded for the URLs that
flowed through. A failure anywhere downstream leaves the previous hash
committed, so the next run re-processes the source instead of silently
skipping it at Fetch.

Per-URL isolation: a URL that stops early never blocks the others, and a
failure in a shared stage (validate/merge/publish) aborts the run BEFORE
publishing, so a half-failed run can never corrupt the knowledge bundle.

The Extractor, concept-matching and Merger LLM seams are the real ones by
default (they shell out to the claude CLI themselves); tests inject fakes
through the same parameters used by every other stage.

CLI: python3 -m pipeline.orchestrate URL [URL ...]
     python3 -m pipeline.orchestrate --phrase "ingest <url>, update the bundle"
     python3 -m pipeline.orchestrate --discover URL [URL ...]   (optional
     first stage: pipeline/discover.py finds new candidate URLs from the
     seeds' cached pages; without the flag nothing changes)
Prints one JSON report; exit 2 only for usage errors (no URLs given).
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import re
import sys
from pathlib import Path

from pipeline import concepts as concepts_mod
from pipeline import topics as topics_mod
from pipeline import discover as discover_mod
from pipeline.adapter import DEFAULT_CLAIMS_PATH, adapt_url
from pipeline.extractor import DEFAULT_CLAIMS_PATH as EXTRACT_CLAIMS_PATH
from pipeline.extractor import claude_cli_extract, extract_url
from pipeline.fetch import DEFAULT_CACHE_PATH, DEFAULT_STATE_PATH, fetch_many
from pipeline.fetch import fetch_many_with_state
from pipeline.merger import claude_cli_classify, merge_facts
from pipeline.publisher import DEFAULT_KNOWLEDGE_PATH, publish_concepts
from pipeline.relevance import DEFAULT_DROPPED_PATH, DEFAULT_GATE_CACHE_PATH
from pipeline.relevance import claude_cli_gate, filter_claims
from pipeline.state import commit_state, load_state
from pipeline.validator import validate_facts

logger = logging.getLogger("pipeline.orchestrate")

ALL_STAGES = ("fetch", "extract", "gate", "adapter", "validator",
              "merger", "publisher")

URL_RE = re.compile(r"https?://[^\s,\"'<>)]+")


class OrchestratorError(ValueError):
    """Usage-level failure (no URLs provided)."""


def parse_ingest_phrase(text: str) -> list[str]:
    """Deterministically extract every http(s) URL from a user phrase like
    'ingest <url1>, <url2>, update the bundle'. Order preserved, duplicates
    and trailing punctuation dropped. The word 'ingest' itself carries no
    URL and is simply not matched."""
    urls: list[str] = []
    for match in URL_RE.findall(text):
        url = match.rstrip(".,;:!")
        if url not in urls:
            urls.append(url)
    return urls


def orchestrate(
    urls: list[str],
    *,
    state_path: str | Path | None = None,
    cache_path: str | Path | None = None,
    claims_path: str | Path | None = None,
    knowledge_dir: str | Path | None = None,
    fetcher=None,
    extract_llm=None,
    gate_llm=None,
    classify_llm=None,
    match_llm=None,
    route_llm=None,
    now: datetime.datetime | None = None,
) -> dict:
    """Run the pipeline for each URL. Returns the full report dict.

    Seam defaults (fetcher, LLM functions) are resolved at CALL time so
    tests can patch the module-level implementations.
    """
    urls = [u.strip() for u in urls if isinstance(u, str) and u.strip()]
    fetcher = fetcher or fetch_many
    extract_llm = extract_llm or claude_cli_extract
    gate_llm = gate_llm or claude_cli_gate
    classify_llm = classify_llm or claude_cli_classify
    match_llm = match_llm or concepts_mod.claude_cli_concept_match
    route_llm = route_llm or topics_mod.claude_cli_choose_topic

    def route(fact: dict) -> str | None:
        slug, _ = topics_mod.route_fact(fact["content"],
                                        fact.get("topic_hint"),
                                        choose_llm=route_llm)
        return slug
    if not urls:
        raise OrchestratorError("no URLs provided")
    now = now or datetime.datetime.now(datetime.UTC)
    fetched_at = now.isoformat(timespec="seconds")
    state_path = state_path or DEFAULT_STATE_PATH
    cache_path = cache_path or DEFAULT_CACHE_PATH
    claims_path = claims_path or DEFAULT_CLAIMS_PATH
    knowledge_dir = knowledge_dir or DEFAULT_KNOWLEDGE_PATH
    # The relevance gate's audit log and verdict cache live next to the
    # fetch state (state/dropped.json, state/gate_cache.json by default).
    state_dir = Path(state_path).parent
    dropped_path = state_dir / "dropped.json"
    gate_cache_path = state_dir / "gate_cache.json"

    # ---- Stage 1: Fetch (existing change detection decides everything) ----
    fetch_results = fetch_many_with_state(
        urls, fetched_at, state_path=state_path, fetcher=fetcher,
        cache_path=cache_path)
    states = load_state(state_path)

    per_url: list[dict] = []
    extracted_urls: list[str] = []
    extraction_paths: dict[str, str] = {}
    for result in fetch_results:
        url = result["url"]
        verdict = result.get("change", "error")
        entry: dict = {"url": url, "fetch": {
            "ok": bool(result.get("ok")), "verdict": verdict,
            "strategy": result.get("strategy")}}
        per_url.append(entry)

        if not result.get("ok"):
            entry.update(stopped_at="fetch",
                         error=result.get("error", "fetch failed"))
            logger.warning("%s: fetch failed — stopping this URL (%s)",
                           url, entry["error"])
            continue
        if verdict == "unchanged":
            # The contract: unchanged content NEVER reaches the Extractor.
            entry.update(stopped_at="fetch",
                         result="unchanged; bundle not modified")
            logger.info("%s: unchanged — stopping after fetch", url)
            continue
        if result.get("content_type") != "markdown":
            entry.update(stopped_at="fetch",
                         error="cache is HTML, not Markdown; extraction unsupported")
            logger.warning("%s: unusable cache format — stopping this URL", url)
            continue

        # ---- Stage 2: Extract (new/changed markdown only) ----
        extraction = extract_url(url, states, cache_path, claims_path,
                                 llm=extract_llm, now=fetched_at)
        entry["extract"] = {"status": extraction["status"],
                            "claim_count": extraction.get("claim_count"),
                            "skipped": extraction.get("skipped", False)}
        if extraction["status"] != "ok":
            entry.update(stopped_at="extract", error=extraction.get("error"))
            logger.warning("%s: extract failed — stopping this URL (%s)",
                           url, extraction.get("error"))
            continue
        extracted_urls.append(url)
        if extraction.get("claims_path"):
            extraction_paths[url] = extraction["claims_path"]

    report: dict = {"urls": per_url, "stages_executed": ["fetch"]}
    if extracted_urls:
        report["stages_executed"].extend(["extract", "gate"])

    # ---- Stage 3: Relevance gate + Adapter (extracted URLs only) ----
    facts: list[dict] = []
    adapted_urls: list[str] = []
    for entry in per_url:
        url = entry["url"]
        if url not in extracted_urls:
            continue
        # Gate between Extract and Adapter: off-topic claims never become
        # facts. The raw claims file stays untouched on disk.
        claims_doc = None
        if extraction_paths.get(url):
            try:
                raw_doc = json.loads(
                    Path(extraction_paths[url]).read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                entry.update(stopped_at="gate", error=f"unreadable claims: {exc}")
                logger.warning("%s: claims unreadable — stopping this URL", url)
                continue
            kept, dropped = filter_claims(
                raw_doc, llm=gate_llm, dropped_path=dropped_path,
                cache_path=gate_cache_path, today=now.date().isoformat())
            claims_doc = {**raw_doc, "claims": kept}
            entry["gate"] = {"kept": len(kept), "dropped": len(dropped)}
        adapted = adapt_url(url, states, claims_path, cache_dir=cache_path,
                            claims_doc=claims_doc)
        entry["adapter"] = {"status": adapted["status"],
                            "fact_count": adapted.get("fact_count", 0)}
        if adapted["status"] == "ok":
            facts.extend(adapted["facts"])
            if adapted.get("fact_count", 0) > 0:
                adapted_urls.append(url)
        else:
            entry.update(stopped_at="adapter", error=adapted["error"])
            logger.warning("%s: adapter failed — %s", url, adapted["error"])

    if not facts:
        report["knowledge_bundle_modified"] = False
        logger.info("no facts produced; validator/merger/publisher not run")
        return report

    # ---- Shared stages; a failure here aborts BEFORE publishing ----
    report["stages_executed"].append("adapter")
    try:
        validated = validate_facts(facts)
    except Exception as exc:  # ValidationError and friends
        report["stage_failed"] = {"stage": "validator", "error": str(exc)}
        report["knowledge_bundle_modified"] = False
        logger.error("validator failed — aborting before merge/publish: %s", exc)
        return report
    report["stages_executed"].append("validator")
    report["facts"] = {
        "adapted": len(facts),
        "valid": sum(1 for f in validated if f["status"] == "valid"),
        "valid_low_confidence": sum(1 for f in validated
                                    if f["status"] == "valid_low_confidence"),
        "rejected": sum(1 for f in validated if f["status"] == "rejected"),
    }

    try:
        # The maintained bundle participates: concepts are matched against
        # the existing documents (topic-routing identity first, then
        # deterministic candidates, LLM only in the ambiguous band — the
        # bundle is never loaded into the LLM whole).
        existing = concepts_mod.load_bundle(knowledge_dir)
        merged = merge_facts(validated, existing=existing,
                             classify_llm=classify_llm, match_llm=match_llm,
                             today=now.date().isoformat(), route=route)
    except Exception as exc:  # MergerError and friends
        report["stage_failed"] = {"stage": "merger", "error": str(exc)}
        report["knowledge_bundle_modified"] = False
        logger.error("merger failed — aborting before publish: %s", exc)
        return report
    report["stages_executed"].append("merger")
    report["merge"] = {"input_facts": len(validated),
                       "output_concepts": len(merged["concepts"]),
                       "rejected": len(merged["rejected"])}

    try:
        published = publish_concepts(merged, knowledge_dir=knowledge_dir,
                                     now=now)
    except Exception as exc:  # PublisherError and friends
        report["stage_failed"] = {"stage": "publisher", "error": str(exc)}
        report["knowledge_bundle_modified"] = False
        logger.error("publisher failed — knowledge bundle untouched: %s", exc)
        return report
    report["stages_executed"].append("publisher")
    report["publish"] = published
    report["knowledge_bundle_modified"] = bool(published["published"]
                                               or published["updated"])

    # ---- Commit fetch state ONLY after publication succeeded ----
    committed = commit_state(state_path, adapted_urls)
    if committed:
        logger.info("committed fetch state for %d URL(s) after successful "
                    "publication", len(committed))
    report["fetch_state_committed"] = committed
    return report


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: python3 -m pipeline.orchestrate URL [URL ...]
    or:               python3 -m pipeline.orchestrate --phrase "..."
    optional first stage: --discover (new candidate URLs are read from the
                          seeds' cached pages — and a tvly search when
                          TAVILY_API_KEY is set — then ingested after them)

    Prints one JSON report covering every URL. Exit 0 even when individual
    URLs fail (their failures are data in the report); exit 2 only for usage
    errors. Headless-friendly: no prompts, one command, honest output.
    Without --discover the behavior is exactly the pre-discovery pipeline.
    """
    parser = argparse.ArgumentParser(
        description="Orchestrate discover?->fetch->extract->gate->adapter->"
                    "validator->merger->publisher for URL(s), stopping per "
                    "URL on unchanged content or failure.")
    parser.add_argument("urls", nargs="*", metavar="URL")
    parser.add_argument("--phrase", metavar="TEXT",
                        help='full user phrase, e.g. "ingest <url>, update '
                             'the bundle"; URLs are parsed out of it')
    parser.add_argument("--discover", action="store_true",
                        help="first read links from the seed URLs' cached "
                             "pages (and a tvly search when TAVILY_API_KEY "
                             "is set), then also ingest the new in-scope "
                             "candidates, capped and de-duplicated against "
                             "fetch state")
    args = parser.parse_args(argv)

    urls = args.urls
    if args.phrase is not None:
        urls = parse_ingest_phrase(args.phrase) + [
            u for u in urls if u not in parse_ingest_phrase(args.phrase)]
    if not urls:
        parser.error("no URLs given (pass URLs or --phrase)")

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    discovery: dict | None = None
    if args.discover:
        try:
            discovery = discover_mod.discover(urls)
        except discover_mod.DiscoverError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        urls = urls + [u for u in discovery["candidates"] if u not in urls]

    try:
        report = orchestrate(urls)
    except OrchestratorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if discovery is not None:
        report = {"discover": discovery, **report}
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
