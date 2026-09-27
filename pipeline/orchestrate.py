"""Orchestration driver: sequence the existing pipeline stages for URLs.

Claude Code is the top-level orchestrator: it parses the user's request
("fetch <url>, update the bundle"), invokes this driver as ONE command, and
relays the JSON report. This module contains NO pipeline logic — every step
below is a call into an existing stage; the only added value is sequencing
and the short-circuit the contract requires:

    fetch verdict unchanged   -> STOP that URL (nothing downstream runs at all)
    fetch failed / HTML cache -> STOP that URL, report the reason honestly
    new or changed markdown   -> Extract -> Adapter -> Validator -> Merger -> Publisher

Per-URL isolation: a URL that stops early never blocks the others, and a
failure in a shared stage (validate/merge/publish) aborts the run BEFORE
publishing, so a half-failed run can never corrupt the knowledge bundle.

The Extractor and Merger LLM seams are the real ones by default (they shell
out to the claude CLI themselves); tests inject fakes through the same
parameters used by every other stage.

CLI: python3 -m pipeline.orchestrate URL [URL ...]
Prints one JSON report; exit 2 only for usage errors (no URLs given).
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys
from pathlib import Path

from pipeline.adapter import DEFAULT_CLAIMS_PATH, adapt_url
from pipeline.extractor import DEFAULT_CLAIMS_PATH as EXTRACT_CLAIMS_PATH
from pipeline.extractor import claude_cli_extract, extract_url
from pipeline.fetch import DEFAULT_CACHE_PATH, DEFAULT_STATE_PATH, fetch_many
from pipeline.fetch import fetch_many_with_state
from pipeline.merger import claude_cli_classify, merge_facts
from pipeline.publisher import DEFAULT_KNOWLEDGE_PATH, publish_facts
from pipeline.state import load_state
from pipeline.validator import validate_facts

logger = logging.getLogger("pipeline.orchestrate")

ALL_STAGES = ("fetch", "extract", "adapter", "validator", "merger", "publisher")


class OrchestratorError(ValueError):
    """Usage-level failure (no URLs provided)."""


def orchestrate(
    urls: list[str],
    *,
    state_path: str | Path | None = None,
    cache_path: str | Path | None = None,
    claims_path: str | Path | None = None,
    knowledge_dir: str | Path | None = None,
    fetcher=fetch_many,
    extract_llm=claude_cli_extract,
    merge_llm=claude_cli_classify,
    now: datetime.datetime | None = None,
) -> dict:
    """Run the pipeline for each URL. Returns the full report dict."""
    urls = [u.strip() for u in urls if isinstance(u, str) and u.strip()]
    if not urls:
        raise OrchestratorError("no URLs provided")
    now = now or datetime.datetime.now(datetime.UTC)
    fetched_at = now.isoformat(timespec="seconds")
    state_path = state_path or DEFAULT_STATE_PATH
    cache_path = cache_path or DEFAULT_CACHE_PATH
    claims_path = claims_path or DEFAULT_CLAIMS_PATH
    knowledge_dir = knowledge_dir or DEFAULT_KNOWLEDGE_PATH

    # ---- Stage 1: Fetch (existing change detection decides everything) ----
    fetch_results = fetch_many_with_state(
        urls, fetched_at, state_path=state_path, fetcher=fetcher,
        cache_path=cache_path)
    states = load_state(state_path)

    per_url: list[dict] = []
    extracted_urls: list[str] = []
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

    # ---- Stage 3: Adapter (only for successfully extracted URLs) ----
    facts: list[dict] = []
    for entry in per_url:
        url = entry["url"]
        if url not in extracted_urls:
            continue
        adapted = adapt_url(url, states, claims_path)
        entry["adapter"] = {"status": adapted["status"],
                            "fact_count": adapted.get("fact_count", 0)}
        if adapted["status"] == "ok":
            facts.extend(adapted["facts"])
        else:
            entry.update(stopped_at="adapter", error=adapted["error"])
            logger.warning("%s: adapter failed — %s", url, adapted["error"])

    report: dict = {"urls": per_url, "stages_executed": ["fetch"]}

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
        merged = merge_facts(validated, llm=merge_llm)
    except Exception as exc:  # MergerError and friends
        report["stage_failed"] = {"stage": "merger", "error": str(exc)}
        report["knowledge_bundle_modified"] = False
        logger.error("merger failed — aborting before publish: %s", exc)
        return report
    report["stages_executed"].append("merger")
    report["merge"] = {"input": len(validated), "output": len(merged)}

    try:
        published = publish_facts(merged, knowledge_dir=knowledge_dir, now=now)
    except Exception as exc:  # PublisherError and friends
        report["stage_failed"] = {"stage": "publisher", "error": str(exc)}
        report["knowledge_bundle_modified"] = False
        logger.error("publisher failed — knowledge bundle untouched: %s", exc)
        return report
    report["stages_executed"].append("publisher")
    report["publish"] = published
    report["knowledge_bundle_modified"] = bool(published["published"]
                                               or published["updated"])
    return report


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: python3 -m pipeline.orchestrate URL [URL ...]

    Prints one JSON report covering every URL. Exit 0 even when individual
    URLs fail (their failures are data in the report); exit 2 only for usage
    errors. Headless-friendly: no prompts, one command, honest output.
    """
    parser = argparse.ArgumentParser(
        description="Orchestrate fetch->extract->adapter->validator->merger->"
                    "publisher for URL(s), stopping per URL on unchanged "
                    "content or failure.")
    parser.add_argument("urls", nargs="+", metavar="URL")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        report = orchestrate(args.urls)
    except OrchestratorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
