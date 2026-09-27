"""Merger stage: collapse validated facts into the final fact set for publishing.

Architecture boundary (deliberate and visible):

    LLM seam  ->  semantic relationship classification   duplicate | conflicting | complementary
    Python    ->  conflict resolution & combination      authority > recency > majority

The LLM answers ONLY "how are these two facts related?" It never decides a
winner, assigns confidence, rewrites claims, creates merged facts, or touches
knowledge/. Every decision after the label — which conflicting fact wins, how
duplicates and complementary facts combine — is deterministic Python, so
identical input plus identical labels produce identical output.

Input: the Validator's output — the original fact fields (url, date, content,
is_changed, last_run, source_type, community_agree_count, topic_id, ...)
already extended with "confidence_score" and "status". Only facts with status
"valid" or "valid_low_confidence" participate in merging; "rejected" facts
pass through completely unchanged (never merged, never dropped).

Resolution values on merged facts:
  single_source                   untouched fact, no relation found in its group
  duplicate_merged                same claim from multiple sources, one survivor
  complementary_merge             same subject, non-conflicting details combined
  conflict_resolved_by_authority  official beat community
  conflict_resolved_by_recency    newer source beat older (authority tied)
  conflict_resolved_by_majority   more independent sources won (authority+date tied)
  unresolved_conflict             authority, recency and majority all tied: BOTH kept

Conflicts are resolved cluster-vs-cluster with precedence authority > recency
> majority. Authority: a cluster containing any official source outranks a
community-only cluster, regardless of community numbers. Recency: newest
source date in the cluster (missing/unparseable dates lose to dated ones).
Majority: count of distinct supporting URLs. The losing cluster is dropped
with a log line naming the rule — never silently. Conflicts are processed in
deterministic (input-index) order, and a cluster already dropped by an
earlier conflict takes no further part.

Deterministic combination rules (the LLM never rewrites content):
  duplicates      keep ONE original content verbatim — the highest-scoring
                  member's (earliest input index on ties); never paraphrase
  complementary   join member contents in input order with a single space:
                  both texts preserved verbatim, nothing invented. The merge
                  is skipped when any pair between the two clusters was
                  labeled conflicting (keep facts separate rather than force
                  a relationship)
  confidence      max of member scores; status "valid" if any member is valid
  sources         every contributing URL once, as {url, date, source_type}

Grouping: by topic_id. Facts without topic_id are treated as their own topic
(no LLM calls for them) — cross-topic semantic grouping is upstream's job.

Fail-safe LLM seam: invalid JSON or an invalid label raises LlmError; the
offending pair is reported (logged) and both facts stay separate
single_source facts instead of silently guessing a relationship.

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

logger = logging.getLogger("pipeline.merger")  # stable name when run as -m

LABELS = ("duplicate", "conflicting", "complementary")
PARTICIPATING_STATUSES = ("valid", "valid_low_confidence")
REJECTED = "rejected"
SOURCE_TYPES = ("official", "community")
REQUIRED_FACT_KEYS = ("url", "content", "source_type", "status")
LLM_TIMEOUT = 120  # seconds, one-shot claude call per pair


class LlmError(RuntimeError):
    """The LLM seam failed (unavailable, bad exit, unparseable output)."""


class MergerError(ValueError):
    """An input fact violates the Merger contract."""


# --------------------------------------------------------------------------
# LLM seam — semantic classification only. Mock this in tests.
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
# Deterministic layer — grouping, conflict resolution, combination.
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


def _group_facts(facts: list[dict]) -> list[list[int]]:
    """Bucket participating fact indices by topic_id, in first-appearance order.

    Facts without a usable topic_id become their own single-fact group — the
    Merger never guesses semantic grouping across topics.
    """
    groups: dict[str, list[int]] = {}
    order: list[str] = []
    for i, fact in enumerate(facts):
        if fact["status"] == REJECTED:
            continue
        topic = fact.get("topic_id")
        key = topic.strip() if isinstance(topic, str) and topic.strip() \
            else f"\x00auto:{i}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(i)
    return [groups[k] for k in order]


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


def _pair_labels(facts: list[dict], group: list[int], llm) -> dict[tuple[int, int], str]:
    """Classify every pair in a group (local indices, x < y). Failed pairs are
    reported and simply left out — both facts then stay separate."""
    labels: dict[tuple[int, int], str] = {}
    for x in range(len(group)):
        for y in range(x + 1, len(group)):
            i, j = group[x], group[y]
            try:
                label = llm(facts[i], facts[j])
            except LlmError as exc:
                logger.warning(
                    "LLM classification failed for %s <-> %s; keeping both "
                    "facts separate (%s)", facts[i]["url"], facts[j]["url"], exc)
                continue
            if label not in LABELS:
                logger.warning(
                    "invalid label %r for %s <-> %s; keeping both facts separate",
                    label, facts[i]["url"], facts[j]["url"])
                continue
            labels[(x, y)] = label
            logger.debug("pair %s <-> %s: %s",
                         facts[i]["url"], facts[j]["url"], label)
    return labels


def _resolve_conflict(
    facts: list[dict],
    members_a: list[int],
    members_b: list[int],
) -> tuple[str, str] | None:
    """Deterministic precedence: authority > recency > majority.

    Returns (("left"|"right", rule), ...) or None when completely tied.
    No LLM involvement — this is pure Python by design.
    """
    a_official = any(facts[m]["source_type"] == "official" for m in members_a)
    b_official = any(facts[m]["source_type"] == "official" for m in members_b)
    if a_official != b_official:
        return ("left", "authority") if a_official else ("right", "authority")

    a_date = max((d for d in (_parse_date(facts[m].get("date")) for m in members_a)
                  if d is not None), default=None)
    b_date = max((d for d in (_parse_date(facts[m].get("date")) for m in members_b)
                  if d is not None), default=None)
    if a_date is None and b_date is None:
        pass
    elif b_date is None:
        return ("left", "recency")
    elif a_date is None:
        return ("right", "recency")
    elif a_date != b_date:
        return ("left", "recency") if a_date > b_date else ("right", "recency")

    a_urls = len({facts[m]["url"] for m in members_a})
    b_urls = len({facts[m]["url"] for m in members_b})
    if a_urls != b_urls:
        return ("left", "majority") if a_urls > b_urls else ("right", "majority")
    return None


def _build_merged(
    facts: list[dict],
    members: list[int],
    resolution: str,
    has_complementary: bool,
) -> dict:
    """Combine one cluster into the output fact. No content is ever rewritten:
    duplicates keep one member's text verbatim; complementary merges join the
    original texts in input order with a single space."""
    if len(members) == 1:
        content = facts[members[0]]["content"]
    elif has_complementary:
        content = " ".join(facts[m]["content"] for m in members)
    else:
        best = max(members, key=lambda m: (facts[m]["confidence_score"], -m))
        content = facts[best]["content"]

    sources: list[dict] = []
    seen: set[str] = set()
    for m in members:
        url = facts[m]["url"]
        if url in seen:
            continue
        seen.add(url)
        sources.append({"url": url, "date": facts[m].get("date"),
                        "source_type": facts[m]["source_type"]})

    return {
        "content": content,
        "sources": sources,
        "confidence_score": max(facts[m]["confidence_score"] for m in members),
        "resolution": resolution,
        "status": ("valid" if any(facts[m]["status"] == "valid" for m in members)
                   else "valid_low_confidence"),
    }


def _merge_group(facts: list[dict], group: list[int], llm) -> list[tuple[int, dict]]:
    """Merge one topic group. Returns [(first_member_global_index, merged), ...]."""
    labels = _pair_labels(facts, group, llm)
    n = len(group)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def members_of(root: int) -> list[int]:
        return [l for l in range(n) if find(l) == root]

    # Duplicates first: same claim -> one survivor cluster.
    for (x, y), label in sorted(labels.items()):
        if label == "duplicate":
            rx, ry = find(x), find(y)
            if rx != ry:
                parent[ry] = rx

    # Complementary: join clusters, but never across a conflicting pair.
    has_comp = {l: False for l in range(n)}  # keyed by current root
    for (x, y), label in sorted(labels.items()):
        if label != "complementary":
            continue
        rx, ry = find(x), find(y)
        if rx == ry:
            has_comp[rx] = True
            continue
        lefts, rights = members_of(rx), members_of(ry)
        unsafe = any(labels.get((min(a, b), max(a, b))) == "conflicting"
                     for a in lefts for b in rights)
        if unsafe:
            logger.debug("complementary merge %s <-> %s skipped: a conflicting "
                         "pair exists between the clusters",
                         facts[group[lefts[0]]]["url"], facts[group[rights[0]]]["url"])
            continue
        parent[ry] = rx
        has_comp[rx] = has_comp.get(rx, False) or has_comp.get(ry, False) or True

    # Conflicts: deterministic precedence, losers dropped (with a log line).
    win_rule: dict[int, str] = {}
    unresolved: set[int] = set()
    dead: set[int] = set()
    for (x, y), label in sorted(labels.items()):
        if label != "conflicting":
            continue
        rx, ry = find(x), find(y)
        if rx == ry:
            logger.debug("conflict inside one merged cluster (%s <-> %s); kept",
                         facts[group[x]]["url"], facts[group[y]]["url"])
            continue
        if rx in dead or ry in dead:
            continue
        members_a = [group[l] for l in members_of(rx)]
        members_b = [group[l] for l in members_of(ry)]
        outcome = _resolve_conflict(facts, members_a, members_b)
        if outcome is None:
            unresolved.update((rx, ry))
            logger.info("unresolved conflict (authority, recency and majority "
                        "tied): keeping both %s and %s",
                        [facts[m]["url"] for m in members_a],
                        [facts[m]["url"] for m in members_b])
            continue
        side, rule = outcome
        winner_root, loser_root = (rx, ry) if side == "left" else (ry, rx)
        win_rule.setdefault(winner_root, rule)
        dead.add(loser_root)
        loser_urls = [facts[m]["url"] for m in
                      ([group[l] for l in members_of(loser_root)])]
        logger.info("conflict resolved by %s: %s wins, dropping %s", rule,
                    [facts[m]["url"] for m in
                     ([group[l] for l in members_of(winner_root)])], loser_urls)

    # Assemble: one output fact per surviving cluster, in member order.
    out: list[tuple[int, dict]] = []
    for root in sorted({find(l) for l in range(n)}):
        if root in dead:
            continue
        members = sorted(members_of(root))
        if root in unresolved:
            resolution = "unresolved_conflict"
        elif root in win_rule:
            resolution = f"conflict_resolved_by_{win_rule[root]}"
        elif len(members) > 1 and has_comp.get(root, False):
            resolution = "complementary_merge"
        elif len(members) > 1:
            resolution = "duplicate_merged"
        else:
            resolution = "single_source"
        merged = _build_merged(facts, [group[l] for l in members],
                               resolution, has_comp.get(root, False))
        logger.info("merge group of %d -> resolution=%s status=%s sources=%d",
                    len(members), resolution, merged["status"],
                    len(merged["sources"]))
        out.append((group[members[0]], merged))
    return out


def merge_facts(facts: list[dict], llm=claude_cli_classify) -> list[dict]:
    """Merge Validator output into the final fact set for the Publisher.

    Rejected facts pass through unchanged. Participating facts are merged per
    topic group; output preserves input order (each merged fact appears at its
    first contributing member's position).
    """
    _check_contract(facts)
    emitted: dict[int, dict] = {}
    for group in _group_facts(facts):
        for representative, merged in _merge_group(facts, group, llm):
            emitted[representative] = merged
    out: list[dict] = []
    for i, fact in enumerate(facts):
        if fact["status"] == REJECTED:
            out.append(dict(fact))
        elif i in emitted:
            out.append(emitted[i])
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None, llm=claude_cli_classify) -> int:
    """CLI entry point: python3 -m pipeline.merger FACTS_JSON

    FACTS_JSON is a file containing a JSON array of Validator-output facts
    (or {"facts": [...]}) or "-" to read stdin. Prints the merged fact array
    to stdout. Exit 0 on success; 2 means the input itself was invalid.
    """
    parser = argparse.ArgumentParser(
        description="Merge validated facts (LLM classifies pairs; Python "
                    "resolves and combines).")
    parser.add_argument("facts", metavar="FACTS_JSON",
                        help="JSON array of validated facts, or '-' for stdin")
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
    try:
        merged = merge_facts(data, llm=llm)
    except MergerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(merged, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
