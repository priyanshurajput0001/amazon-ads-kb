"""Validator stage: deterministically score extracted facts and set status.

Input: a list of fact dicts from an Extractor run:

    {"url", "date", "confidence_pct", "content", "is_changed", "last_run",
     "source_type", "community_agree_count", ["topic_id"]}

Output: the same dicts, each extended with "confidence_score", "status" and —
unless the fact is cleanly valid — a "reason". No fact is ever dropped:
rejected facts stay in the output with an explanation.

Deterministic by construction: no LLM, no network, no clock reads, no RNG.
Identical input produces identical output. The Extractor's confidence_pct is
deliberately never read (informational only); trust is recomputed from source
type, corroboration, and stability.

Scoring (computed in integer hundredths to avoid float drift, emitted as x.xx):
  base           official 0.60 | community >=3 independent people 0.30
                 community <3 people 0.15 — the spec leaves this base
                 undefined; 0.15 keeps low-quality agreement inside the
                 valid_low_confidence band, as the quality rule requires
  corroboration  +0.15 per additional independent agreeing URL, capped +0.30
  stability      +0.10 when is_changed == "N" and last_run is set. The
                 Fetcher's "unchanged" verdict already means the current
                 content equals the prior stored run for that URL, so those
                 two conditions together are "unchanged across the last 2
                 runs"; last_run is required to prove a prior run existed.
  final          clamped to [0.0, 1.0]

Status: >=0.60 valid | >=0.30 valid_low_confidence | <0.30 rejected — then
rule layers below may lower it, never raise it.

Rule layers (first match wins per fact):
  1. official-overrides-community — a community fact contradicting an official
     fact is rejected even when many community sources agree with it (there is
     no majority vote against an official source).
  2. support-required — official backing, OR >=2 independent community URLs
     agreeing, OR >=3 independent people behind a community fact; otherwise
     rejected.
  3. score-below-minimum — computed score < 0.30.
  4. official-cross-page-contradiction — official facts that materially
     contradict each other are capped at valid_low_confidence (both sides: a
     deterministic validator cannot decide which official page is right; the
     Merger notes the disagreement).
  5. low-quality-community-cap — agreement made only of community sources
     with <3 independent people each can never be valid.

Grouping: facts carrying topic_id are bucketed by it; the rest are bucketed by
content similarity (a topic-less fact joins a topic bucket only when it is the
same claim). Inside a bucket, every cross-URL pair is classified from
normalized token-set Jaccard similarity plus two deterministic value-flip
checks:

    sim < 0.5                            unrelated (different facts)
    negation flip or numeric mismatch    contradict (same fact, opposite claim)
    sim >= 0.8                           agree (corroboration)
    0.5 <= sim < 0.8                     contradict (materially different claim)

These bands are honest heuristics, not understanding: they catch wording
variants, "X vs not X" flips, and "1 day vs 5 days" value flips, and they are
constants so every decision is reproducible and auditable.

CLI: python3 -m pipeline.validator FACTS_JSON   ("-" reads stdin)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("pipeline.validator")  # stable name when run as -m

# Scoring constants, in integer hundredths of a point (avoids float drift).
BASE_OFFICIAL = 60
BASE_COMMUNITY = 30
BASE_COMMUNITY_LOW = 15
CORROBORATION_STEP = 15
CORROBORATION_MAX = 30
STABILITY_BONUS = 10
VALID_MIN = 60
LOW_CONFIDENCE_MIN = 30
FULL_SCALE = 100
COMMUNITY_PEOPLE_MIN = 3  # ">=3 independent people" makes a community source quality

# Similarity bands for pairwise claim comparison (token-set Jaccard).
AGREE_MIN = 0.8
CONFLICT_MIN = 0.5

NEGATION_TOKENS = frozenset(
    {"not", "no", "never", "cannot", "without", "nor", "none", "neither"})
TOKEN_RE = re.compile(r"[a-z0-9]+")
NUMBER_RE = re.compile(r"^\d+(?:[.,]\d+)*$")

STATUS_VALID = "valid"
STATUS_LOW = "valid_low_confidence"
STATUS_REJECTED = "rejected"

REQUIRED_FACT_KEYS = ("url", "content", "source_type")
SOURCE_TYPES = ("official", "community")


class ValidationError(ValueError):
    """An input fact violates the Validator contract."""


@dataclass(frozen=True)
class _Profile:
    """Precomputed deterministic view of one fact used for all comparisons."""

    url: str
    source_type: str
    agree_count: int
    stable: bool
    tokens: frozenset[str]
    numbers: frozenset[str]
    negations: frozenset[str]


def _check_contract(facts: object) -> None:
    """Reject malformed input up front; nothing downstream re-checks shapes."""
    if not isinstance(facts, list):
        raise ValidationError("input must be a JSON array of fact objects")
    for i, fact in enumerate(facts):
        if not isinstance(fact, dict):
            raise ValidationError(f"fact[{i}] is not an object")
        for key in REQUIRED_FACT_KEYS:
            if key not in fact:
                raise ValidationError(f"fact[{i}]: missing {key!r}")
        if not isinstance(fact["url"], str) or not fact["url"].strip():
            raise ValidationError(f"fact[{i}]: 'url' must be a non-empty string")
        if not isinstance(fact["content"], str) or not fact["content"].strip():
            raise ValidationError(f"fact[{i}]: 'content' must be a non-empty string")
        if fact["source_type"] not in SOURCE_TYPES:
            raise ValidationError(
                f"fact[{i}]: source_type must be one of {SOURCE_TYPES}, "
                f"got {fact['source_type']!r}")


def _coerce_agree_count(value: object) -> int:
    """Tolerate int or numeric-string; anything else counts as zero people."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    if value is not None:
        logger.debug("ignoring non-numeric community_agree_count %r", value)
    return 0


def _profile(fact: dict) -> _Profile:
    tokens = TOKEN_RE.findall(fact["content"].lower())
    is_changed = fact.get("is_changed", "Y")
    if is_changed not in ("Y", "N"):
        logger.debug("fact %s: unknown is_changed %r treated as 'Y'",
                     fact["url"], is_changed)
        is_changed = "Y"
    stable = is_changed == "N" and bool(fact.get("last_run"))
    if is_changed == "N" and not fact.get("last_run"):
        logger.debug("fact %s: is_changed='N' without last_run; no stability "
                     "bonus (no proof of a prior stored run)", fact["url"])
    return _Profile(
        url=fact["url"],
        source_type=fact["source_type"],
        agree_count=_coerce_agree_count(fact.get("community_agree_count", 0)),
        stable=stable,
        tokens=frozenset(tokens),
        numbers=frozenset(t for t in tokens if NUMBER_RE.match(t)),
        negations=frozenset(t for t in tokens if t in NEGATION_TOKENS),
    )


# --------------------------------------------------------------------------
# Grouping and pairwise classification
# --------------------------------------------------------------------------

def _similarity(a: _Profile, b: _Profile) -> float:
    """Token-set Jaccard similarity of two claims' normalized content."""
    if not a.tokens and not b.tokens:
        return 1.0  # two empty claims are treated as identical
    if not a.tokens or not b.tokens:
        return 0.0
    return len(a.tokens & b.tokens) / len(a.tokens | b.tokens)


def _classify_pair(a: _Profile, b: _Profile) -> str:
    """unrelated | agree | contradict — the deterministic seam of the Validator."""
    sim = _similarity(a, b)
    if sim < CONFLICT_MIN:
        return "unrelated"
    negation_flip = a.negations != b.negations
    numeric_flip = bool(a.numbers) and bool(b.numbers) and a.numbers != b.numbers
    if negation_flip or numeric_flip:
        return "contradict"
    if sim >= AGREE_MIN:
        return "agree"
    return "contradict"


def group_facts(facts: list[dict], profiles: list[_Profile]) -> list[list[int]]:
    """Bucket fact indices: by topic_id when present, else by content similarity.

    A topic-less fact may join an existing bucket: topic buckets only at
    AGREE_MIN (it must be the same claim as a member), similarity buckets at
    CONFLICT_MIN (matching how those buckets form, so contradicting versions
    of one fact land together and get compared). Greedy first-match in input
    order keeps the result deterministic.
    """
    buckets: list[list[int]] = []
    is_topic_bucket: list[bool] = []
    topic_index: dict[str, int] = {}
    pending: list[int] = []

    for i, fact in enumerate(facts):
        topic = fact.get("topic_id")
        if isinstance(topic, str) and topic.strip():
            key = topic.strip()
            if key not in topic_index:
                topic_index[key] = len(buckets)
                buckets.append([])
                is_topic_bucket.append(True)
            buckets[topic_index[key]].append(i)
        else:
            pending.append(i)

    for i in pending:
        joined = False
        for b, members in enumerate(buckets):
            threshold = AGREE_MIN if is_topic_bucket[b] else CONFLICT_MIN
            if any(_similarity(profiles[i], profiles[m]) >= threshold
                   for m in members):
                buckets[b].append(i)
                joined = True
                break
        if not joined:
            buckets.append([i])
            is_topic_bucket.append(False)
    return buckets


def detect_conflicts(
    facts: list[dict],
    profiles: list[_Profile],
    buckets: list[list[int]],
) -> tuple[dict[int, set[int]], dict[int, set[int]]]:
    """Classify every cross-URL pair inside each bucket.

    Returns ({index: contradicting indices}, {index: agreeing indices}).
    Same-URL pairs are skipped: they are one source, not independent ones.
    """
    contradictions: dict[int, set[int]] = {i: set() for i in range(len(facts))}
    agreements: dict[int, set[int]] = {i: set() for i in range(len(facts))}
    for bucket in buckets:
        for x in range(len(bucket)):
            for y in range(x + 1, len(bucket)):
                i, j = bucket[x], bucket[y]
                if facts[i]["url"] == facts[j]["url"]:
                    continue
                verdict = _classify_pair(profiles[i], profiles[j])
                if verdict == "contradict":
                    contradictions[i].add(j)
                    contradictions[j].add(i)
                    logger.debug("conflict %s <-> %s (sim=%.3f)",
                                 facts[i]["url"], facts[j]["url"],
                                 _similarity(profiles[i], profiles[j]))
                elif verdict == "agree":
                    agreements[i].add(j)
                    agreements[j].add(i)
    return contradictions, agreements


# --------------------------------------------------------------------------
# Scoring and status decision
# --------------------------------------------------------------------------

def calculate_confidence(
    source_type: str,
    community_agree_count: int,
    corroborating_urls: int,
    stable: bool,
) -> tuple[float, dict]:
    """Score one fact. Returns (score in [0,1], breakdown for auditing)."""
    if source_type == "official":
        base = BASE_OFFICIAL
    elif community_agree_count >= COMMUNITY_PEOPLE_MIN:
        base = BASE_COMMUNITY
    else:
        base = BASE_COMMUNITY_LOW
    corroboration = min(corroborating_urls * CORROBORATION_STEP, CORROBORATION_MAX)
    stability = STABILITY_BONUS if stable else 0
    points = max(0, min(FULL_SCALE, base + corroboration + stability))
    breakdown = {
        "base": base / FULL_SCALE,
        "corroboration": corroboration / FULL_SCALE,
        "stability": stability / FULL_SCALE,
    }
    return points / FULL_SCALE, breakdown


def validate_fact(
    i: int,
    facts: list[dict],
    profiles: list[_Profile],
    contradictions: dict[int, set[int]],
    agreements: dict[int, set[int]],
    group_label: str,
) -> dict:
    """Decide one fact's additions: confidence_score, status, optional reason."""
    fact, profile = facts[i], profiles[i]
    agreeing_urls = {facts[j]["url"] for j in agreements[i]}
    corroborating_urls = len(agreeing_urls - {profile.url})
    score, breakdown = calculate_confidence(
        profile.source_type, profile.agree_count, corroborating_urls,
        profile.stable)
    logger.debug("score %s base=%.2f corroboration=%.2f stability=%.2f -> %.2f",
                 profile.url, breakdown["base"], breakdown["corroboration"],
                 breakdown["stability"], score)

    official_backing = (profile.source_type == "official"
                        or any(facts[j]["source_type"] == "official"
                               for j in agreements[i]))
    community_urls = {facts[j]["url"] for j in agreements[i]
                      if facts[j]["source_type"] == "community"}
    if profile.source_type == "community":
        community_urls.add(profile.url)
    supported = (official_backing
                 or len(community_urls) >= 2
                 or (profile.source_type == "community"
                     and profile.agree_count >= COMMUNITY_PEOPLE_MIN))
    contradicting_officials = sorted(
        {facts[j]["url"] for j in contradictions[i]
         if facts[j]["source_type"] == "official"})
    official_contradiction = (profile.source_type == "official"
                              and any(facts[j]["source_type"] == "official"
                                      for j in contradictions[i]))
    low_quality_only = (profile.source_type == "community"
                        and profile.agree_count < COMMUNITY_PEOPLE_MIN
                        and not official_backing
                        and len(community_urls) >= 2)

    additions: dict = {"confidence_score": score}
    if profile.source_type == "community" and contradicting_officials:
        additions["status"] = STATUS_REJECTED
        additions["reason"] = (
            "contradicts official source(s) "
            + ", ".join(contradicting_officials)
            + "; official version takes precedence over community agreement")
        rule = "official-overrides-community"
    elif not supported:
        additions["status"] = STATUS_REJECTED
        additions["reason"] = ("no official source supports this claim and "
                               "fewer than 2 independent community sources agree "
                               "(community_agree_count < 3)")
        rule = "support-required"
    elif score < LOW_CONFIDENCE_MIN / FULL_SCALE:
        additions["status"] = STATUS_REJECTED
        additions["reason"] = f"confidence score {score:.2f} is below the 0.30 minimum"
        rule = "score-below-minimum"
    else:
        status = STATUS_VALID if score >= VALID_MIN / FULL_SCALE else STATUS_LOW
        rule = "score-band"
        if official_contradiction:
            status = STATUS_LOW
            rule = "official-cross-page-contradiction"
        elif low_quality_only:
            status = STATUS_LOW
            rule = "low-quality-community-cap"
        additions["status"] = status
        if status == STATUS_LOW:
            if rule == "official-cross-page-contradiction":
                additions["reason"] = ("official sources materially contradict "
                                       "each other on this claim")
            elif rule == "low-quality-community-cap":
                additions["reason"] = ("corroborated only by low-quality community "
                                       "sources (<3 independent people each); "
                                       "capped at valid_low_confidence")
            else:
                additions["reason"] = (f"confidence score {score:.2f} is below "
                                       "the 0.60 valid threshold")

    logger.info("decision url=%s group=%s score=%.2f status=%s rule=%s%s",
                profile.url, group_label, score, additions["status"], rule,
                f" reason={additions['reason']}" if "reason" in additions else "")
    return additions


def validate_facts(facts: list[dict]) -> list[dict]:
    """Validate a batch of extracted facts. Pure, deterministic, order-preserving.

    Every input fact appears exactly once in the output, carrying all of its
    original fields plus "confidence_score", "status" and (unless cleanly
    valid) a "reason". Input dicts are never mutated.
    """
    _check_contract(facts)
    profiles = [_profile(fact) for fact in facts]
    buckets = group_facts(facts, profiles)
    contradictions, agreements = detect_conflicts(facts, profiles, buckets)

    group_of: dict[int, str] = {}
    for b, bucket in enumerate(buckets):
        first = facts[bucket[0]]
        topic = first.get("topic_id")
        label = f"topic:{topic.strip()}" if isinstance(topic, str) and topic.strip() \
            else f"auto:{bucket[0]}"
        for i in bucket:
            group_of[i] = label
    logger.debug("grouped %d facts into %d groups", len(facts), len(buckets))

    out = []
    for i, fact in enumerate(facts):
        additions = validate_fact(i, facts, profiles, contradictions,
                                  agreements, group_of[i])
        out.append({**fact, **additions})
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """CLI entry point: python3 -m pipeline.validator FACTS_JSON

    FACTS_JSON is a file containing a JSON array of facts (or {"facts": [...]})
    or "-" to read stdin. Prints the validated array to stdout. Exit 0 on
    success; 2 means the input itself was unreadable or invalid.
    """
    parser = argparse.ArgumentParser(
        description="Score extracted facts and decide their status "
                    "(deterministic; no LLM, no network).")
    parser.add_argument("facts", metavar="FACTS_JSON",
                        help="JSON array of facts, or '-' for stdin")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="also log pairwise similarity/conflict details")
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
    try:
        validated = validate_facts(data)
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(validated, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
