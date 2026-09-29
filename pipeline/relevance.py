"""Relevance gate: drop claims that are not about Amazon Ads.

Sits BETWEEN Extract and Adapter (review step 4). A GitHub organization
page yields genuinely off-topic claims (an Alexa SDK, a computer-vision-in-
Excel repo, ...); without a gate they flow into the bundle as concepts no
one asked for. The gate keeps the pipeline's scope promise: claims about
Amazon Ads, its advertising APIs, and seller/vendor advertising tooling.

Decision order (deterministic first, one LLM call only for borderline):
  1. any DROP token  -> dropped  (named off-topic artifacts: alexa, excel,
                               pecos, smoke, amazon pay, ... — deterministic)
  2. any STRONG token -> kept   (ads/advertising/sponsored/campaign/... —
                               an ads-specific word is decisive evidence)
  3. otherwise        -> ONE Claude yes/no call, cached in
                         state/gate_cache.json so re-runs (rebuild!) replay
                         the recorded verdict instead of re-asking — the
                         gate stays deterministic ACROSS runs.

Every dropped claim is logged to state/dropped.json with its reason
(idempotently: re-filtering the same claims leaves the log byte-identical).

The gate never affects the unchanged-source short-circuit: it runs after
Extract, which only runs for new/changed content.

CLI: python3 -m pipeline.relevance CLAIMS_JSON  (prints kept/dropped).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

from pipeline.validator import canonical_tokens, stem

logger = logging.getLogger("pipeline.relevance")

LLM_TIMEOUT = 120  # seconds, one-shot claude call per borderline claim

DEFAULT_DROPPED_PATH = Path(__file__).resolve().parents[1] / "state" / "dropped.json"
DEFAULT_GATE_CACHE_PATH = Path(__file__).resolve().parents[1] / "state" / "gate_cache.json"

# Ads-specific words: one hit is decisive evidence of relevance. Lists are
# normalized through the shared stemmer at import time so a human writes
# surface forms ('pecos', 'advertising') while matching stays canonical
# (stem('pecos') == 'peco').
STRONG_TOKENS = frozenset(stem(t) for t in {
    "ad", "ads", "advertising", "advertiser", "sponsored", "sponsor",
    "campaign", "campaigns", "bulksheet", "bulksheets", "marketing",
    "dsp", "amc", "mcp", "stream", "reporting", "reports", "seller",
    "sellers", "selling", "vendor", "vendors", "partner", "partners",
    "audience", "bidding", "keyword", "keywords", "budget", "aps",
    "unboxed", "onboarding",
})

# Named off-topic artifacts from the ingested corpus: one hit drops the
# claim even when it also mentions Amazon (deterministic; extend the list,
# never the LLM, when a new off-topic artifact appears).
DROP_TOKENS = frozenset(stem(t) for t in {
    "alexa", "echo", "excel", "pecos", "smoke", "pay", "voice",
    "vision", "kindle", "twitch", "imdb", "audible", "zeek", "kraken",
    "brackets",
})

GATE_PROMPT = """You are the relevance gate of an Amazon Ads knowledge pipeline.
Decide whether ONE factual claim is IN SCOPE for a knowledge base about
Amazon Ads: its advertising APIs (Amazon Ads API, reporting, Amazon
Marketing Stream, Amazon Marketing Cloud, bulksheets, MCP server), the
Amazon advertising developer ecosystem (ads repositories and SDKs under
Amazon's GitHub organization, Selling Partner API tools used for selling
and advertising on Amazon), and seller/vendor advertising tooling.

Claims about other Amazon products (Alexa/Echo, Amazon Pay, retail
features, non-advertising open-source projects) are OUT OF SCOPE even when
they mention Amazon. Claims about the amzn GitHub organization itself or
its repositories are IN SCOPE when they are about ads/API/developer
tooling; purely incidental Amazon facts (company size, unrelated repos)
are OUT OF SCOPE.

Rules:
- Judge ONLY this claim's subject. Do not speculate about the page it came
  from, and do not rewrite anything.

Return ONLY one JSON object, no prose: {{"in_scope": true}} or {{"in_scope": false}}

CLAIM: {claim}
"""


class GateError(RuntimeError):
    """The gate could not decide (seam unavailable/unparseable)."""


def claude_cli_gate(claim: str) -> bool:
    """LLM seam: one-shot headless Claude call; True = in scope."""
    if shutil.which("claude") is None:
        raise GateError("no LLM backend available (claude CLI not found)")
    prompt = GATE_PROMPT.format(claim=claim)
    try:
        proc = subprocess.run(["claude", "-p", prompt], capture_output=True,
                              text=True, timeout=LLM_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise GateError(f"LLM timed out after {LLM_TIMEOUT}s") from None
    if proc.returncode != 0:
        raise GateError(f"claude CLI exited {proc.returncode}: "
                        f"{proc.stderr.strip()[:200]}")
    return _parse_bool(proc.stdout)


def _parse_bool(text: str) -> bool:
    stripped = text.strip()
    if "```" in stripped:
        blocks = [b for b in stripped.split("```") if b.strip()]
        stripped = max(blocks, key=len).strip()
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        raise GateError("LLM returned invalid JSON") from None
    if isinstance(data, dict) and isinstance(data.get("in_scope"), bool):
        return data["in_scope"]
    raise GateError(
        f'LLM JSON must be {{"in_scope": true|false}}, '
        f"got {text.strip()[:120]!r}")


def _claim_key(url: str, claim: str) -> str:
    return hashlib.sha256(f"{url}|{claim}".encode("utf-8")).hexdigest()


def gate_claim(
    claim: str,
    url: str = "",
    llm=claude_cli_gate,
    cache_path: str | Path | None = None,
    today: str | None = None,
) -> tuple[bool, str, str]:
    """Decide ONE claim: (keep, reason, decided_by) where decided_by is
    'drop-tokens' | 'allow-list' | 'llm' | 'llm-cache'. Borderline verdicts
    are cached in gate_cache.json so re-runs replay them deterministically.

    A seam FAILURE keeps the claim (fail-open) with decided_by='error':
    dropping content because a helper was unavailable would lose knowledge
    silently; the claim can still be dropped on a later, healthy run."""
    tokens = canonical_tokens(claim)
    hit_drop = tokens & DROP_TOKENS
    if hit_drop:
        return False, f"off-topic token(s): {', '.join(sorted(hit_drop))}", \
            "drop-tokens"
    hit_strong = tokens & STRONG_TOKENS
    if hit_strong:
        return True, f"ads-specific token(s): {', '.join(sorted(hit_strong))}", \
            "allow-list"

    cache: dict[str, dict] = {}
    cache_file = Path(cache_path) if cache_path else None
    if cache_file and cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("gate cache unreadable (%s); ignored", exc)
            cache = {}
    claim_sha = hashlib.sha256(claim.encode("utf-8")).hexdigest()
    if claim_sha in cache and isinstance(cache[claim_sha].get("keep"), bool):
        return cache[claim_sha]["keep"], \
            f"cached verdict: {cache[claim_sha].get('reason', '')}", "llm-cache"

    try:
        keep = bool(llm(claim))
    except GateError as exc:
        logger.warning("relevance seam failed for %r (%s); keeping the "
                       "claim (fail-open)", claim[:60], exc)
        return True, f"seam error (kept, fail-open): {exc}", "error"

    today = today or datetime.datetime.now(datetime.UTC).date().isoformat()
    cache[claim_sha] = {"keep": keep, "reason": "borderline claim judged "
                                                "by the LLM seam",
                        "url": url, "date": today}
    if cache_file:
        _write_json_atomic(cache_file, cache)
    return keep, cache[claim_sha]["reason"], "llm"


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def log_dropped(path: str | Path, url: str, claim: str, reason: str,
                decided_by: str, today: str) -> None:
    """Append one dropped claim to the audit log, keyed by
    sha256(url|claim). An already-logged claim keeps its FIRST record —
    re-running the gate on another day must leave the log byte-identical
    (idempotency), and the first-seen date is the provenance that matters."""
    path = Path(path)
    log: dict[str, dict] = {}
    if path.exists():
        try:
            log = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("dropped log unreadable; recreating")
            log = {}
    key = _claim_key(url, claim)
    if key in log:
        return
    log[key] = {
        "claim": claim, "url": url, "reason": reason,
        "decided_by": decided_by, "date": today,
    }
    _write_json_atomic(path, log)


def filter_claims(
    doc: dict,
    llm=claude_cli_gate,
    dropped_path: str | Path | None = None,
    cache_path: str | Path | None = None,
    today: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Gate one Extractor claims document. Returns (kept_claims,
    dropped_records) — the claims doc itself is never mutated; the raw
    extraction stays the record of what the page said."""
    today = today or datetime.datetime.now(datetime.UTC).date().isoformat()
    url = doc.get("source_url", "")
    kept: list[dict] = []
    dropped: list[dict] = []
    for claim in doc.get("claims", []):
        text = claim.get("claim") if isinstance(claim, dict) else None
        if not isinstance(text, str) or not text.strip():
            continue  # malformed claims are the Adapter's problem, not scope
        keep, reason, decided_by = gate_claim(
            text, url=url, llm=llm, cache_path=cache_path, today=today)
        if keep:
            kept.append(claim)
        else:
            record = {"claim": text, "url": url, "reason": reason,
                      "decided_by": decided_by, "date": today}
            dropped.append(record)
            if dropped_path:
                log_dropped(dropped_path, url, text, reason, decided_by, today)
            logger.info("dropped off-topic claim (%s): %r", decided_by,
                        text[:80])
    return kept, dropped


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: python3 -m pipeline.relevance CLAIMS_JSON

    Prints {"kept": [...], "dropped": [...]} for one claims document.
    """
    parser = argparse.ArgumentParser(
        description="Gate one Extractor claims document for Amazon Ads "
                    "relevance (deterministic keywords first; one LLM "
                    "yes/no only for borderline claims).")
    parser.add_argument("claims", metavar="CLAIMS_JSON",
                        help="claims document JSON (or '-' for stdin)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    raw = sys.stdin.read() if args.claims == "-" \
        else Path(args.claims).read_text(encoding="utf-8")
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON input: {exc}", file=sys.stderr)
        return 2
    kept, dropped = filter_claims(doc, dropped_path=DEFAULT_DROPPED_PATH,
                                  cache_path=DEFAULT_GATE_CACHE_PATH)
    print(json.dumps({"kept": kept, "dropped": dropped}, indent=2,
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
