"""Merger stage: fold validated facts into concepts, against the live bundle.

Architecture boundary (deliberate and visible):

    Python    ->  concept matching (deterministic band) + conflict resolution
                  (authority > recency > majority) + combination
    LLM seam  ->  same-concept decisions in the ambiguous band, and pairwise
                  duplicate|conflicting|complementary labels within a concept

The knowledge bundle PARTICIPATES (review 2026-09-28): the Merger loads the
existing concept documents from knowledge/, matches every new validated fact
against them (deterministic token-overlap candidates first, LLM confirmation
only in the ambiguous band — the whole bundle is never sent to the LLM), and
merges new facts INTO the existing concepts. A source that changes a value
(MIT-0 -> Apache-2.0) therefore updates the concept it already belongs to.

Output: concepts, not sentences. One concept = one future OKF document:

    {"id": stable slug, "title": ..., "type": "concept",
     "facts": [fact dicts], "conflicts": [superseded fact dicts]}

fact dict: content, confidence_score, status, resolution, first_seen,
sources [{url, date, source_type}]; conflict dicts add superseded_by.

Conflicts are never silently erased: the losing fact is RETAINED in the
concept's conflicts list with its full provenance, the rule that demoted it,
and the content that superseded it. A complete tie (authority, recency and
majority all equal) keeps BOTH facts current, each stamped
unresolved_conflict.

Deterministic combination rules (the LLM never rewrites content):
  duplicates      keep ONE original content verbatim — an existing fact's
                  wording when any is present (stability across runs), else
                  the highest-scoring member's; never paraphrase
  complementary   facts simply coexist in the concept — sentences are no
                  longer space-joined into run-on hybrids
  confidence      recomputed corroboration (Validator arithmetic, reused)
                  but never below the best member's score
  sources         union of every contributing {url, date, source_type};

Rejected facts pass through in the report, never merged, never published.

CLI: python3 -m pipeline.merger FACTS_JSON   ("-" reads stdin)
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

from pipeline import concepts
from pipeline.concepts import (
    AUTO_SAME,
    GENERIC_TOKENS,
    canonical_tokens,
    humanize,
    jaccard,
    number_tokens,
)
from pipeline.validator import NEGATION_TOKENS, calculate_confidence

logger = logging.getLogger("pipeline.merger")  # stable name when run as -m

LABELS = ("duplicate", "conflicting", "complementary")
PARTICIPATING_STATUSES = ("valid", "valid_low_confidence")
REJECTED = "rejected"
SOURCE_TYPES = ("official", "community")
REQUIRED_FACT_KEYS = ("url", "content", "source_type", "status")
LLM_TIMEOUT = 120  # seconds, one-shot claude call per pair

# Within-concept deterministic bands (token-set Jaccard over stemmed tokens).
FLIP_MIN = 0.5   # a negation/numeric flip at >= 0.5 overlap is a conflict


class LlmError(RuntimeError):
    """The LLM seam failed (unavailable, bad exit, unparseable output)."""


class MergerError(ValueError):
    """An input fact violates the Merger contract."""


# --------------------------------------------------------------------------
# LLM seams — semantic classification only. Mock these in tests.
# --------------------------------------------------------------------------

MERGER_PROMPT = """You are the semantic-comparison seam of the Merger for an Amazon Ads knowledge pipeline.
Compare the two claims below and answer EXACTLY one question: how are they related?

Labels:
- "duplicate"     : same underlying claim, different wording.
- "conflicting"   : same subject/claim but contradictory values or information.
- "complementary" : same subject but each contains different non-conflicting information.

Rules:
- Do NOT decide which claim wins.
- Do NOT assign confidence, rewrite the claims, or merge them.
- Do NOT touch knowledge files; you are one comparison, nothing more.

Return ONLY one JSON object, no prose: {{"label": "duplicate"}} (label one of duplicate|conflicting|complementary).

CLAIM A ({url_a}): {content_a}
CLAIM B ({url_b}): {content_b}
"""


def claude_cli_classify(fact_a: dict, fact_b: dict) -> str:
    """LLM seam: one-shot headless Claude call answering only the label question."""
    if shutil.which("claude") is None:
        raise LlmError("no LLM backend available (claude CLI not found)")
    prompt = MERGER_PROMPT.format(url_a=fact_a["url"], content_a=fact_a["content"],
                                  url_b=fact_b["url"], content_b=fact_b["content"])
    try:
        proc = subprocess.run(["claude", "-p", prompt], capture_output=True,
                              text=True, timeout=LLM_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise LlmError(f"LLM timed out after {LLM_TIMEOUT}s") from None
    if proc.returncode != 0:
        raise LlmError(f"claude CLI exited {proc.returncode}: {proc.stderr.strip()[:200]}")
    return _parse_label(proc.stdout)


def _parse_label(text: str) -> str:
    """Parse the LLM reply: bare object or fenced JSON; must be {label: ...}."""
    stripped = text.strip()
    if "```" in stripped:
        blocks = [b for b in stripped.split("```") if b.strip()]
        stripped = max(blocks, key=len).strip()
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        raise LlmError("LLM returned invalid JSON") from None
    if isinstance(data, dict) and data.get("label") in LABELS:
        return data["label"]
    raise LlmError(
        f'LLM JSON must be {{"label": ...}} with a label in {LABELS}, '
        f"got {text.strip()[:120]!r}")


# --------------------------------------------------------------------------
# Contract validation
# --------------------------------------------------------------------------

def _check_contract(facts: object) -> None:
    """Validate the Validator-output contract up front; nothing re-checks later."""
    if not isinstance(facts, list):
        raise MergerError("input must be a JSON array of fact objects")
    for i, fact in enumerate(facts):
        if not isinstance(fact, dict):
            raise MergerError(f"fact[{i}] is not an object")
        for key in REQUIRED_FACT_KEYS:
            if key not in fact:
                raise MergerError(f"fact[{i}]: missing {key!r}")
        if not isinstance(fact["url"], str) or not fact["url"].strip():
            raise MergerError(f"fact[{i}]: 'url' must be a non-empty string")
        if fact["source_type"] not in SOURCE_TYPES:
            raise MergerError(
                f"fact[{i}]: source_type must be one of {SOURCE_TYPES}, "
                f"got {fact['source_type']!r}")
        if fact["status"] not in PARTICIPATING_STATUSES + (REJECTED,):
            raise MergerError(
                f"fact[{i}]: status must be one of "
                f"{PARTICIPATING_STATUSES + (REJECTED,)}, got {fact['status']!r}")
        if fact["status"] != REJECTED:
            if not isinstance(fact["content"], str) or not fact["content"].strip():
                raise MergerError(f"fact[{i}]: 'content' must be a non-empty string")
            score = fact.get("confidence_score")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise MergerError(
                    f"fact[{i}]: participating facts need a numeric "
                    f"'confidence_score', got {score!r}")


# --------------------------------------------------------------------------
# Within-concept pairwise classification (deterministic first)
# --------------------------------------------------------------------------

def _classify_deterministic(a: frozenset[str], b: frozenset[str]) -> str | None:
    """duplicate | conflicting | None (undecided — maybe worth an LLM call).

    Reuses the Validator's tripwires: a negation or numeric flip at >= 0.5
    overlap is a conflict; >= 0.8 overlap is the same claim restated.
    """
    sim = jaccard(a, b)
    if sim < FLIP_MIN:
        return None
    neg_a = {t for t in a if t in NEGATION_TOKENS}
    neg_b = {t for t in b if t in NEGATION_TOKENS}
    num_a, num_b = number_tokens(a), number_tokens(b)
    if (neg_a != neg_b) or (num_a and num_b and num_a != num_b):
        return "conflicting"
    if sim >= AUTO_SAME:
        return "duplicate"
    return None


def _needs_llm(a: frozenset[str], b: frozenset[str]) -> bool:
    """Cheap deterministic gate: only pairs that could plausibly be
    duplicate/conflicting reach the LLM seam (shared informative content
    words, or value-bearing numbers on both sides)."""
    shared = a & b
    informative = shared - GENERIC_TOKENS
    if len(informative) >= 2:
        return True
    if number_tokens(a) and number_tokens(b) and informative:
        return True
    return False


def _member_tokens(member: dict) -> frozenset[str]:
    return canonical_tokens(member["content"])


def _pair_labels(members: list[dict], new_flags: list[bool],
                 llm) -> dict[tuple[int, int], str]:
    """Classify pairs that involve at least one NEW fact. Deterministic bands
    first; the LLM seam only for undecided, plausible pairs. Failed pairs are
    reported and left out — both facts then stay separate."""
    tokens = [_member_tokens(m) for m in members]
    labels: dict[tuple[int, int], str] = {}
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            if not (new_flags[i] or new_flags[j]):
                continue  # existing-existing: settled in an earlier run
            verdict = _classify_deterministic(tokens[i], tokens[j])
            if verdict is None:
                if not _needs_llm(tokens[i], tokens[j]):
                    continue  # coexist; no relationship to act on
                try:
                    verdict = llm(
                        {"url": members[i]["sources"][0]["url"],
                         "content": members[i]["content"]},
                        {"url": members[j]["sources"][0]["url"],
                         "content": members[j]["content"]})
                except LlmError as exc:
                    logger.warning(
                        "LLM classification failed for %r <-> %r; keeping both "
                        "facts separate (%s)", members[i]["content"][:50],
                        members[j]["content"][:50], exc)
                    continue
                if verdict not in LABELS:
                    logger.warning(
                        "invalid label %r; keeping both facts separate", verdict)
                    continue
            labels[(i, j)] = verdict
    return labels


# --------------------------------------------------------------------------
# Conflict resolution — deterministic precedence over clusters
# --------------------------------------------------------------------------

def _parse_date(value: object) -> datetime.datetime | None:
    """Best-effort ISO-8601 parse; naive datetimes are pinned to UTC so mixed
    aware/naive inputs stay comparable. Unparseable -> None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def _cluster_dates(members: list[dict]) -> datetime.datetime | None:
    dates = [d for d in (_parse_date(s.get("date"))
                         for m in members for s in m["sources"]) if d]
    return max(dates) if dates else None


def _cluster_official(members: list[dict]) -> bool:
    return any(s.get("source_type") == "official"
               for m in members for s in m["sources"])


def _cluster_urls(members: list[dict]) -> set[str]:
    return {s["url"] for m in members for s in m["sources"]}


def _resolve_conflict(members_a: list[dict],
                      members_b: list[dict]) -> tuple[str, str] | None:
    """Deterministic precedence: authority > recency > majority.

    Returns ("left"|"right", rule) or None when completely tied.
    No LLM involvement — this is pure Python by design.
    """
    a_official, b_official = _cluster_official(members_a), _cluster_official(members_b)
    if a_official != b_official:
        return ("left", "authority") if a_official else ("right", "authority")

    a_date, b_date = _cluster_dates(members_a), _cluster_dates(members_b)
    if a_date is None and b_date is None:
        pass
    elif b_date is None:
        return ("left", "recency")
    elif a_date is None:
        return ("right", "recency")
    elif a_date != b_date:
        return ("left", "recency") if a_date > b_date else ("right", "recency")

    a_urls, b_urls = len(_cluster_urls(members_a)), len(_cluster_urls(members_b))
    if a_urls != b_urls:
        return ("left", "majority") if a_urls > b_urls else ("right", "majority")
    return None


# --------------------------------------------------------------------------
# Combination
# --------------------------------------------------------------------------

def _date_part(value: object) -> str | None:
    if isinstance(value, str) and len(value) >= 10:
        return value[:10]
    return None


def _combine(members: list[dict], resolution: str) -> dict:
    """Combine one duplicate cluster into a single fact. No content is ever
    rewritten: an existing member's wording wins (stability across runs);
    for brand-new clusters, the highest-scoring member's text (earliest
    index on ties)."""
    existing = [m for m in members if not m.get("is_new")]
    if existing:
        content = existing[0]["content"]
        template = existing[0]
    else:
        best = max(range(len(members)),
                   key=lambda i: (float(members[i]["confidence_score"]), -i))
        content = members[best]["content"]
        template = members[best]

    # Union of sources: one entry per URL, latest date wins (a re-fetch that
    # re-confirms a claim refreshes its fetch date; first_seen keeps the past).
    by_url: dict[str, dict] = {}
    for member in members:
        for source in member["sources"]:
            url = source["url"]
            prior = by_url.get(url)
            if prior is None or (_parse_date(source.get("date")) or
                                 datetime.datetime.min.replace(
                                     tzinfo=datetime.UTC)) >= \
                    (_parse_date(prior.get("date")) or
                     datetime.datetime.min.replace(tzinfo=datetime.UTC)):
                by_url[url] = {"url": url, "date": source.get("date"),
                               "source_type": source["source_type"]}
    sources = [by_url[url] for url in sorted(by_url)]

    first_seen = min((m.get("first_seen") for m in members
                      if m.get("first_seen")), default=None)

    # Recompute corroboration with the Validator's arithmetic, but never
    # lower a score the members already earned.
    best_score = max(float(m["confidence_score"]) for m in members)
    urls = {s["url"] for m in members for s in m["sources"]}
    agree = max((int(m.get("community_agree_count") or 0) for m in members),
                default=0)
    stable = any(m.get("stable") for m in members)
    recomputed, _ = calculate_confidence(
        template["sources"][0]["source_type"], agree, max(0, len(urls) - 1),
        stable)
    score = max(best_score, recomputed)

    return {
        "content": content,
        "confidence_score": score,
        "status": ("valid" if any(m["status"] == "valid" for m in members)
                   else "valid_low_confidence"),
        "resolution": resolution,
        "first_seen": first_seen,
        "sources": sources,
    }


# --------------------------------------------------------------------------
# Per-concept merge
# --------------------------------------------------------------------------

def _new_member(fact: dict, today: str) -> dict:
    date = fact.get("date")
    return {
        "content": fact["content"],
        "confidence_score": float(fact["confidence_score"]),
        "status": fact["status"],
        "resolution": "single_source",
        "first_seen": _date_part(date) or today,
        "sources": [{"url": fact["url"], "date": date,
                     "source_type": fact["source_type"]}],
        "community_agree_count": fact.get("community_agree_count", 0),
        "stable": fact.get("is_changed") == "N",
        "is_new": True,
    }


def _existing_member(fact: dict) -> dict:
    return {**fact, "community_agree_count": 0, "stable": False,
            "is_new": False}


def _merge_concept(new_facts: list[dict], existing: dict | None, today: str,
                   classify_llm) -> dict:
    """Merge the new facts for one concept with its existing document."""
    members: list[dict] = []
    new_flags: list[bool] = []
    if existing:
        for fact in existing.get("facts", []):
            members.append(_existing_member(fact))
            new_flags.append(False)
    for fact in new_facts:
        members.append(_new_member(fact, today))
        new_flags.append(True)

    labels = _pair_labels(members, new_flags, classify_llm) if len(members) > 1 \
        else {}
    n = len(members)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def members_of(root: int) -> list[int]:
        return [l for l in range(n) if find(l) == root]

    # Duplicates first: same claim -> one survivor cluster.
    for (i, j), label in sorted(labels.items()):
        if label == "duplicate":
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[rj] = ri

    # Conflicts: deterministic precedence between clusters. The losing
    # cluster is RETAINED (with provenance) as a superseded conflict.
    win_rule: dict[int, str] = {}
    unresolved: set[int] = set()
    dead: set[int] = set()
    conflict_records: list[dict] = []
    for (i, j), label in sorted(labels.items()):
        if label != "conflicting":
            continue
        ri, rj = find(i), find(j)
        if ri == rj:
            logger.debug("conflict inside one merged cluster; kept")
            continue
        if ri in dead or rj in dead:
            continue
        lefts = [members[l] for l in members_of(ri)]
        rights = [members[l] for l in members_of(rj)]
        outcome = _resolve_conflict(lefts, rights)
        if outcome is None:
            unresolved.update((ri, rj))
            logger.info("unresolved conflict (authority, recency and majority "
                        "tied): keeping both %r and %r",
                        lefts[0]["content"][:50], rights[0]["content"][:50])
            continue
        side, rule = outcome
        winner_root, loser_root = (ri, rj) if side == "left" else (rj, ri)
        win_rule.setdefault(winner_root, rule)
        dead.add(loser_root)
        winner = _combine([members[l] for l in members_of(winner_root)],
                          f"conflict_resolved_by_{rule}")
        loser_members = [members[l] for l in members_of(loser_root)]
        loser = _combine(loser_members, f"conflict_resolved_by_{rule}")
        loser["superseded_by"] = winner["content"]
        conflict_records.append(loser)
        logger.info("conflict resolved by %s: %r supersedes %r", rule,
                    winner["content"][:50], loser["content"][:50])

    # Assemble surviving clusters, in member order.
    facts: list[dict] = []
    for root in sorted({find(l) for l in range(n)}):
        if root in dead:
            continue
        idxs = sorted(members_of(root))
        if root in unresolved:
            resolution = "unresolved_conflict"
        elif root in win_rule:
            resolution = f"conflict_resolved_by_{win_rule[root]}"
        elif len(idxs) > 1:
            resolution = "duplicate_merged"
        else:
            resolution = members[idxs[0]].get("resolution", "single_source")
        merged = _combine([members[l] for l in idxs], resolution)
        facts.append(merged)

    return {
        "id": (existing or {}).get("id") or new_facts[0]["concept_id"],
        "title": (existing or {}).get("title")
        or humanize(new_facts[0]["concept_id"]),
        "type": "concept",
        "facts": facts,
        "conflicts": ((existing or {}).get("conflicts", [])
                      + conflict_records),
    }


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def merge_facts(
    facts: list[dict],
    existing: dict[str, dict] | None = None,
    classify_llm=claude_cli_classify,
    match_llm=concepts.claude_cli_concept_match,
    today: str | None = None,
) -> dict:
    """Merge Validator output into concepts, matched against the live bundle.

    Returns {"concepts": [...], "rejected": [...]}: concepts carry the full
    merged state for the Publisher; rejected facts pass through unchanged.
    """
    _check_contract(facts)
    existing = existing or {}
    today = today or datetime.datetime.now(datetime.UTC).date().isoformat()

    participating = [f for f in facts if f["status"] != REJECTED]
    rejected = [dict(f) for f in facts if f["status"] == REJECTED]

    assigned, new_concepts = concepts.assign_concepts(
        participating, existing, match_llm=match_llm)

    grouped: dict[str, list[dict]] = {}
    for fact in assigned:
        grouped.setdefault(fact["concept_id"], []).append(fact)

    merged_concepts: list[dict] = []
    for cid in sorted(grouped):
        merged = _merge_concept(grouped[cid], existing.get(cid), today,
                                classify_llm)
        concepts.check_concept(merged)
        merged_concepts.append(merged)
        logger.info("concept %s: %d facts, %d conflicts",
                    cid, len(merged["facts"]), len(merged["conflicts"]))

    return {"concepts": merged_concepts, "rejected": rejected}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None, classify_llm=claude_cli_classify,
         match_llm=concepts.claude_cli_concept_match) -> int:
    """CLI entry point: python3 -m pipeline.merger FACTS_JSON

    FACTS_JSON is a file containing a JSON array of Validator-output facts
    (or {"facts": [...]}) or "-" to read stdin. The existing bundle is read
    from knowledge/ (override with --knowledge-dir). Prints the merged
    concept array to stdout. Exit 0 on success; 2 on invalid input.
    """
    parser = argparse.ArgumentParser(
        description="Merge validated facts into concepts (LLM classifies "
                    "pairs and concept matches; Python resolves and combines).")
    parser.add_argument("facts", metavar="FACTS_JSON",
                        help="JSON array of validated facts, or '-' for stdin")
    parser.add_argument("--knowledge-dir", default=None,
                        help="existing bundle to merge into "
                             "(default: the repository knowledge/)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="also log pair labels and merge details")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s")

    raw = sys.stdin.read() if args.facts == "-" \
        else Path(args.facts).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON input: {exc}", file=sys.stderr)
        return 2
    if isinstance(data, dict) and isinstance(data.get("facts"), list):
        data = data["facts"]
    kdir = Path(args.knowledge_dir) if args.knowledge_dir else \
        Path(__file__).resolve().parents[1] / "knowledge"
    existing = concepts.load_bundle(kdir)
    try:
        merged = merge_facts(data, existing=existing, classify_llm=classify_llm,
                             match_llm=match_llm)
    except (MergerError, concepts.ConceptError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(merged, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
