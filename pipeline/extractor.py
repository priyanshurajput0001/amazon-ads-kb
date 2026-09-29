"""Extractor stage: turn cached Markdown pages into discrete factual claims.

Deterministic layer (this module): resolve a URL against fetch state, locate
and integrity-check the cached Markdown, call the LLM seam, validate the
claims (including verbatim-quote grounding), and persist one claims file per
content version at state/claims/<sha256>.json.

LLM judgment (isolated in `claude_cli_extract`, mockable): what the page
actually claims. The LLM never decides new/changed/contradicted, never
deduplicates across URLs, and never touches knowledge/ — that is the
Validator's and Merger's job.

CLI: python3 -m pipeline.extractor URL [URL ...]
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
from pathlib import Path

from pipeline.fetch import DEFAULT_CACHE_PATH, DEFAULT_STATE_PATH
from pipeline.okf import CONFIDENCE_LEVELS
from pipeline.state import SourceState, load_state

DEFAULT_CLAIMS_PATH = Path(__file__).resolve().parents[1] / "state" / "claims"
SCHEMA_VERSION = 1
LLM_TIMEOUT = 300  # seconds, one-shot claude call per URL

REQUIRED_CLAIM_KEYS = ("claim", "quote", "topic_hint", "confidence")
REQUIRED_DOC_KEYS = ("schema_version", "source_url", "sha256", "fetched_at",
                     "extracted_at", "status", "claims")


class LlmError(RuntimeError):
    """The LLM seam failed (unavailable, bad exit, unparseable output)."""


class ValidationError(ValueError):
    """An LLM extraction violated the claims contract; nothing is written."""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# LLM seam — the only non-deterministic part. Mock this in tests.
# --------------------------------------------------------------------------

EXTRACTOR_PROMPT = """You are the Extractor for an Amazon Ads knowledge pipeline.
From the Markdown source below, extract only factual claims explicitly supported by the text.

Rules:
- Each claim is ONE self-contained factual statement.
- "quote" is copied VERBATIM from the source (a single contiguous span).
- Do not invent facts; do not merge distant parts of the page into unsupported claims.
- Ignore navigation/sidebar/search/sign-in boilerplate and broken blob: image URLs.
- Preserve Amazon terminology exactly (e.g. "Amazon Marketing Stream").
- "topic_hint" is a suggested slug for a future knowledge doc — advisory only, not a final doc id.
- "confidence" is how clearly the source states the claim: low | medium | high.
- Do NOT decide new/changed/contradicted, deduplicate across pages, or write knowledge/.

Return ONLY a JSON array (no prose) shaped like:
[{{"claim": "...", "quote": "...", "topic_hint": "...", "confidence": "high"}}]

SOURCE URL: {url}
SOURCE MARKDOWN:
{content}
"""


def claude_cli_extract(content: str, source_url: str) -> list[dict]:
    """LLM seam: one-shot headless Claude call (the project's agent runtime)."""
    if shutil.which("claude") is None:
        raise LlmError("no LLM backend available (claude CLI not found)")
    prompt = EXTRACTOR_PROMPT.format(url=source_url, content=content)
    try:
        proc = subprocess.run(["claude", "-p", prompt], capture_output=True,
                              text=True, timeout=LLM_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise LlmError(f"LLM timed out after {LLM_TIMEOUT}s") from None
    if proc.returncode != 0:
        raise LlmError(f"claude CLI exited {proc.returncode}: {proc.stderr.strip()[:200]}")
    return _parse_json_claims(proc.stdout)


def _parse_json_claims(text: str) -> list[dict]:
    """Parse the LLM reply: bare array, fenced array, or {{"claims": [...]}}."""
    text = text.strip()
    if "```" in text:
        blocks = [b for b in text.split("```") if b.strip()]
        text = max(blocks, key=len).strip()
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise LlmError("LLM returned invalid JSON") from None
    if isinstance(data, dict) and isinstance(data.get("claims"), list):
        data = data["claims"]
    if not isinstance(data, list):
        raise LlmError("LLM JSON is not a claims array")
    return data


# --------------------------------------------------------------------------
# Deterministic validation — nothing invalid is ever written to disk.
# --------------------------------------------------------------------------

def validate_extraction(doc: dict, content: str, url: str, sha256: str) -> None:
    """Verify a claims document against its source. Raises ValidationError."""
    missing = [k for k in REQUIRED_DOC_KEYS if k not in doc]
    if missing:
        raise ValidationError(f"missing top-level fields: {missing}")
    if doc["schema_version"] != SCHEMA_VERSION:
        raise ValidationError(f"schema_version must be {SCHEMA_VERSION}")
    if doc["source_url"] != url:
        raise ValidationError(f"source_url {doc['source_url']!r} != requested {url!r}")
    if doc["sha256"] != sha256:
        raise ValidationError(f"sha256 {doc['sha256']!r} != cache {sha256!r}")
    if doc["status"] != "ok":
        raise ValidationError(f"status must be 'ok', got {doc['status']!r}")
    if not isinstance(doc["claims"], list):
        raise ValidationError("'claims' must be a list")

    for i, claim in enumerate(doc["claims"]):
        where = f"claim[{i}]"
        if not isinstance(claim, dict):
            raise ValidationError(f"{where}: not an object")
        for key in REQUIRED_CLAIM_KEYS:
            if key not in claim:
                raise ValidationError(f"{where}: missing {key!r}")
        for key in ("claim", "quote", "topic_hint"):
            if not isinstance(claim[key], str) or not claim[key].strip():
                raise ValidationError(f"{where}: {key!r} must be a non-empty string")
        if claim["confidence"] not in CONFIDENCE_LEVELS:
            raise ValidationError(
                f"{where}: confidence must be one of {CONFIDENCE_LEVELS}, "
                f"got {claim['confidence']!r}")
        if claim["quote"] not in content:
            raise ValidationError(f"{where}: quote not found verbatim in source")


def _write_json_atomic(path: Path, doc: dict) -> None:
    """Write claims JSON using the project's state-layer conventions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Per-URL extraction pipeline
# --------------------------------------------------------------------------

def extract_url(
    url: str,
    states: dict[str, SourceState],
    cache_dir: str | Path,
    claims_dir: str | Path,
    llm=claude_cli_extract,
    now: str | None = None,
) -> dict:
    """Run the Extractor for one URL; returns the CLI result line."""
    entry = states.get(url)
    if entry is None or not entry.current_content_sha256:
        return {"url": url, "status": "error",
                "error": "URL not found in fetch state (never successfully fetched)"}

    sha = entry.current_content_sha256  # pending version when one exists
    md_path = Path(cache_dir) / f"{sha}.md"
    if not md_path.exists():
        if (Path(cache_dir) / f"{sha}.html").exists():
            return {"url": url, "status": "error",
                    "error": "cache is HTML, not Markdown; extraction unsupported"}
        return {"url": url, "status": "error", "error": "cache file missing"}

    content = md_path.read_text(encoding="utf-8")
    if _sha256(content) != sha:
        return {"url": url, "status": "error",
                "error": "integrity check failed: cache hash does not match fetch state"}

    claims_file = Path(claims_dir) / f"{sha}.json"
    if claims_file.exists():
        return {"url": url, "status": "ok", "sha256": sha, "skipped": True,
                "reason": "claims already exist", "claims_path": str(claims_file)}

    try:
        claims = llm(content, url)
    except LlmError as exc:
        return {"url": url, "status": "error", "error": f"LLM extraction failed: {exc}"}

    doc = {
        "schema_version": SCHEMA_VERSION,
        "source_url": url,
        "sha256": sha,
        "fetched_at": entry.pending_fetched_at or entry.fetched_at,
        "extracted_at": now or datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "status": "ok",
        "claims": claims,
    }
    try:
        validate_extraction(doc, content, url, sha)
    except ValidationError as exc:
        return {"url": url, "status": "error", "error": f"invalid extraction: {exc}"}

    _write_json_atomic(claims_file, doc)
    return {"url": url, "status": "ok", "sha256": sha, "skipped": False,
            "claims_path": str(claims_file), "claim_count": len(claims)}


def main(
    argv: list[str] | None = None,
    state_path: str | Path | None = None,
    cache_path: str | Path | None = None,
    claims_path: str | Path | None = None,
    llm=claude_cli_extract,
) -> int:
    """CLI entry point: python3 -m pipeline.extractor URL [URL ...]

    One JSON result line per URL; a failing URL never stops the batch.
    """
    parser = argparse.ArgumentParser(
        description="Extract factual claims from cached fetches.")
    parser.add_argument("urls", nargs="+", metavar="URL",
                        help="source URLs to extract (must already be fetched)")
    args = parser.parse_args(argv)

    states = load_state(state_path or DEFAULT_STATE_PATH)
    for url in args.urls:
        result = extract_url(url, states,
                             cache_path or DEFAULT_CACHE_PATH,
                             claims_path or DEFAULT_CLAIMS_PATH,
                             llm=llm)
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
