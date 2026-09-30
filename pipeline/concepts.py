"""Concept layer: stable concept identity + matching + bundle (de)serialization.

WHY THIS EXISTS (review 2026-09-28): document identity used to be the hash of
the final extracted sentence, so any rewording produced a NEW document and a
changed value (MIT-0 -> Apache-2.0) left the old and new documents side by
side. Identity must instead hang off a stable CONCEPT, established from
source identity + topic/concept signals, and survive wording changes.

The layer has three jobs:

1. STABLE IDENTITY. A concept is identified by a readable slug (`id`) stored
   in the document frontmatter. Once written, the id never changes; later
   runs find the document by MATCHING (deterministic token-overlap first,
   LLM confirmation only in the ambiguous band) and adopt the existing id.
   Wording never feeds identity after first publication.

2. MATCHING (deterministic-first, LLM only where needed):
      jaccard(new fact, concept material) >= AUTO_SAME   -> same concept
      jaccard >= CANDIDATE_MIN                            -> LLM decides,
                                     asked TWICE on 'no': first with a
                                     MATCH_PREVIEW-claim preview, then with
                                     the concept's FULL fact list; only two
                                     'no' answers reject the candidate —
                                     one 'no' can never mint a duplicate
                                     document (review step 2)
      below CANDIDATE_MIN (and no hint agreement)         -> different concept
   Candidates are scored against the concept's title and each of its facts,
   so a concept is findable through any of its facts, not a summary.

3. BUNDLE I/O. knowledge/ documents are the source of truth for existing
   knowledge. This module renders and parses the concept document body in
   one rigid, deterministic format (round-trip tested), so the Merger can
   read the maintained bundle and the Publisher can extend it without a
   second, divergent copy of the data.

Fact dict (the unit inside a concept):
    content           single-line claim, verbatim
    confidence_score  float in [0, 1]
    status            valid | valid_low_confidence
    resolution        single_source | duplicate_merged | complementary_merge
                      | conflict_resolved_by_{authority,recency,majority}
                      | unresolved_conflict
    first_seen        YYYY-MM-DD (earliest source date)
    sources           [{url, date, source_type}] — every contributing URL

Concept dict:
    id        stable slug (frontmatter id, filename stem)
    title     humanized from the id — stable with it
    type      always "concept" (only OKF document type in the bundle)
    facts     current facts (winners of any conflict)
    conflicts superseded facts, kept with full provenance + superseded_by
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

from pipeline import okf
from pipeline import topics as topics_mod
from pipeline.validator import (  # single source of truth for token rules
    AGREE_MIN,
    MATCH_STOPWORDS,
    NEGATION_TOKENS,
    NUMBER_RE,
    STOPWORDS,
    TOKEN_RE,
    canonical_tokens,  # re-exported here since 2026-09-30; shared with topics
    stem,
)

logger = logging.getLogger("pipeline.concepts")

CONCEPT_DOC_TYPE = "concept"

# Matching bands (token-set Jaccard over canonical content words).
# AUTO_SAME reuses the Validator's AGREE_MIN: near-identical wording is the
# same concept with no LLM call. CANDIDATE_MIN is deliberately lower than the
# Validator's CONFLICT_MIN: concept matching is coarser than claim
# comparison, and a wider candidate band is what lets a changed VALUE
# (MIT-0 -> Apache-2.0) reach the LLM seam instead of silently starting a
# new concept.
AUTO_SAME = AGREE_MIN   # >= 0.8: same concept, deterministic (the documented
                        # cutoff above which a reworded fact merges with NO
                        # LLM call — a wrong seam answer cannot split it)
CANDIDATE_MIN = 0.30    # >= 0.30: candidate; the LLM seam decides
HINT_TOKEN_MIN = 0.5  # topic-hint token overlap that also creates a candidate
MATCH_PREVIEW = 5     # claims shown to the seam on its FIRST ask for a
                      # candidate; the retry after a 'no' shows the FULL
                      # fact list, so a late-fact paraphrase is still found

# Words so generic in this corpus that sharing only them means nothing.
GENERIC_TOKENS = frozenset({
    "amazon", "ads", "ad", "api", "apis", "advertising", "advertisers",
    "advertiser", "amazon's", "using", "use", "used", "new",
})

# Words uppercased in generated titles (deterministic acronym handling).
ACRONYMS = frozenset({"api", "mcp", "rss", "sdk", "aws", "dsp", "http", "https",
                      "lwa", "amc", "gtm"})

SLUG_WORD_RE = re.compile(r"[a-z0-9]+")
SLUG_MAX = 64
ID_WORDS_MAX = 8  # cap on words taken from content when coining a new id

RESOLUTIONS = (
    "single_source",
    "duplicate_merged",
    "complementary_merge",
    "conflict_resolved_by_authority",
    "conflict_resolved_by_recency",
    "conflict_resolved_by_majority",
    "unresolved_conflict",
)
FACT_STATUSES = ("valid", "valid_low_confidence")
SOURCE_TYPES = ("official", "community")

INDEX_NAME = "INDEX.md"
CHANGELOG_NAME = "CHANGELOG.md"


class ConceptError(ValueError):
    """A concept/fact violates the concept-layer contract."""


# ---------------------------------------------------------------------------
# Canonicalization helpers (token rules live in pipeline/validator.py —
# the single source of truth — and are re-exported above for compatibility)
# ---------------------------------------------------------------------------

# Backwards-compatible alias: internal callers used the private name.
_stem = stem


def number_tokens(tokens: frozenset[str]) -> frozenset[str]:
    return frozenset(t for t in tokens if NUMBER_RE.match(t))


def hint_tokens(hint: object) -> frozenset[str]:
    if not isinstance(hint, str):
        return frozenset()
    return frozenset(stem(w) for w in SLUG_WORD_RE.findall(hint.lower())
                     if w not in MATCH_STOPWORDS)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def readable_slug(text: str) -> str:
    """Deterministic kebab-case slug: lowercase words, stopwords dropped,
    capped at SLUG_MAX on a word boundary. Empty input -> 'concept'."""
    words = [w for w in SLUG_WORD_RE.findall(text.lower())
             if w not in STOPWORDS]
    slug = "-".join(words)
    if len(slug) > SLUG_MAX:
        slug = slug[:SLUG_MAX].rsplit("-", 1)[0]
    return slug.strip("-") or "concept"


def humanize(slug: str) -> str:
    """'amazon-ads-api-access' -> 'Amazon Ads API Access' (stable with id)."""
    words = []
    for word in slug.split("-"):
        words.append(word.upper() if word in ACRONYMS else word.capitalize())
    return " ".join(words)


def coining_slug(facts: list[dict]) -> str:
    """Deterministic slug for a brand-new concept: the unanimous topic hint
    when the group has exactly one, else the leading content words of the
    representative (highest score, earliest index) fact."""
    hints = {tuple(sorted(hint_tokens(f.get("topic_hint"))))
             for f in facts}
    hints.discard(())
    if len(hints) == 1:
        raw = facts[0].get("topic_hint") or ""
        slug = readable_slug(raw)
        if slug != "concept":
            return slug
    representative = max(
        range(len(facts)),
        key=lambda i: (float(facts[i].get("confidence_score", 0)), -i))
    words = [w for w in SLUG_WORD_RE.findall(
        facts[representative].get("content", "").lower())
        if w not in STOPWORDS][:ID_WORDS_MAX]
    return "-".join(words).strip("-") or "concept"


# ---------------------------------------------------------------------------
# LLM seam — concept matching only. Mock this in tests.
# ---------------------------------------------------------------------------

LLM_TIMEOUT = 120  # seconds, one-shot claude call per ambiguous pair

CONCEPT_MATCH_PROMPT = """You are the concept-matching seam of an Amazon Ads knowledge pipeline.
Decide whether ONE new claim belongs to the SAME concept/topic as an EXISTING knowledge document.

The new claim may be worded differently, or state a different VALUE for the same
subject (e.g. a changed license, a changed limit) — those are the same concept.
Two claims about genuinely different subjects are different concepts.

Rules:
- You answer ONLY the same-concept question. Do not judge truth, confidence,
  or which value is right, and do not rewrite anything.

Return ONLY one JSON object, no prose: {{"same_concept": true}} or {{"same_concept": false}}

NEW CLAIM: {new_claim}
EXISTING DOCUMENT — title: {title}
Existing claims:
{existing_claims}
"""


def claude_cli_concept_match(new_claim: str, title: str,
                             existing_claims: list[str]) -> bool:
    """LLM seam: one-shot headless Claude call; True = same concept."""
    if shutil.which("claude") is None:
        raise ConceptError("no LLM backend available (claude CLI not found)")
    # The full claim list is passed through uncut: the retry after a 'no'
    # deliberately shows the seam EVERY fact of the concept (review step 2).
    claims = "\n".join(f"- {c}" for c in existing_claims) or "- (none)"
    prompt = CONCEPT_MATCH_PROMPT.format(
        new_claim=new_claim, title=title, existing_claims=claims)
    try:
        proc = subprocess.run(["claude", "-p", prompt], capture_output=True,
                              text=True, timeout=LLM_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ConceptError(f"LLM timed out after {LLM_TIMEOUT}s") from None
    if proc.returncode != 0:
        raise ConceptError(
            f"claude CLI exited {proc.returncode}: "
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
        raise ConceptError("LLM returned invalid JSON") from None
    if isinstance(data, dict) and isinstance(data.get("same_concept"), bool):
        return data["same_concept"]
    raise ConceptError(
        f'LLM JSON must be {{"same_concept": true|false}}, '
        f"got {text.strip()[:120]!r}")


# ---------------------------------------------------------------------------
# Concept assignment — which concept does each new fact belong to?
# ---------------------------------------------------------------------------

def _concept_material(concept: dict) -> list[frozenset[str]]:
    """Matching targets for an existing concept: its title + every fact."""
    targets = [canonical_tokens(concept.get("title") or "")]
    targets.extend(canonical_tokens(f["content"])
                   for f in concept.get("facts", []))
    return [t for t in targets if t]


def _best_overlap(fact_tokens: frozenset[str], fact_hint: frozenset[str],
                  concept: dict) -> float:
    """Best Jaccard between the new fact and any concept material, boosted by
    topic-hint agreement (hints are the Extractor's concept signal)."""
    best = max((jaccard(fact_tokens, t) for t in _concept_material(concept)),
               default=0.0)
    title_h = hint_tokens(concept.get("id", ""))
    if fact_hint and title_h:
        hint_sim = jaccard(fact_hint, title_h)
        if hint_sim >= HINT_TOKEN_MIN:
            best = max(best, CANDIDATE_MIN)  # enough to become a candidate
    return best


def _ask_match(match_llm, new_claim: str, concept: dict, *, full: bool):
    """One seam call. First ask: title + first MATCH_PREVIEW claims. Retry:
    the concept's FULL fact list — the paraphrase evidence may live in a late
    fact the preview never showed. Returns the bool answer or None on seam
    failure (callers treat failure as 'not this concept', never a guess)."""
    claims = [f["content"] for f in concept.get("facts", [])]
    if not full:
        claims = claims[:MATCH_PREVIEW]
    try:
        return bool(match_llm(new_claim, concept.get("title") or "", claims))
    except ConceptError as exc:
        logger.warning("concept match seam failed for %r vs %s (%s); "
                       "treating as different", new_claim[:60],
                       concept.get("id"), exc)
        return None


def assign_concepts(
    facts: list[dict],
    existing: dict[str, dict],
    match_llm=claude_cli_concept_match,
    route=None,
) -> tuple[list[dict], dict[str, str]]:
    """Annotate each fact with `concept_id` (existing doc id or a new one).

    TOPIC ROUTING FIRST (review step 3): when a `route` callable is given
    (fact -> taxonomy topic slug, or None), every routable fact takes the
    topic's slug as its concept id — deterministically adopting the existing
    document of that name when one exists. Slugs come from the taxonomy,
    never from fact wording, so a reworded extraction updates the same
    document. Facts the router cannot place fall through to the legacy
    matcher below.

    Legacy matching (deterministic-first): exact token-set agreement
    (AUTO_SAME) adopts an existing concept with no LLM call; below
    CANDIDATE_MIN a fact cannot belong to any existing concept. In between,
    the LLM seam decides — but it must answer 'no' TWICE (preview ask, then
    a full-fact-list ask) before a candidate is rejected, so a single wrong
    'no' on a paraphrase can never mint a duplicate document. Candidates are
    visited in a fixed (best-overlap, id) order so runs are reproducible for
    identical answers.

    Returns (facts, new_concepts) where new_concepts maps new id -> title.
    Facts are copied, never mutated.
    """
    facts = [dict(f) for f in facts]
    new_concepts: dict[str, str] = {}

    # --- Pass 0: topic routing — the identity layer (taxonomy slugs) ---
    if route is not None:
        valid_slugs = {t.slug for t in topics_mod.TOPICS}
        for fact in facts:
            slug = route(fact)
            if slug is None:
                continue
            if slug not in valid_slugs:
                logger.warning("router returned unknown topic %r; "
                               "fact falls back to matching", slug)
                continue
            fact["concept_id"] = slug
            if slug not in existing:
                new_concepts.setdefault(
                    slug, topics_mod.TOPIC_BY_SLUG[slug].title)

    # --- Pass 1: match UNROUTED facts against existing concepts ---
    # A fact rejected by the seam after two 'no's still had in-band
    # candidates; when such a fact later coins a NEW concept (Pass 3), that
    # is a potential duplicate mint and must be visible — hence the record.
    rejected_with_candidates: dict[int, tuple[float, str, str]] = {}
    for fact in facts:
        if "concept_id" in fact:
            continue
        fact_tokens = canonical_tokens(fact["content"])
        fact_hint = hint_tokens(fact.get("topic_hint"))
        scored = sorted(
            (( _best_overlap(fact_tokens, fact_hint, concept), cid)
             for cid, concept in existing.items()),
            key=lambda pair: (-pair[0], pair[1]))
        assigned = None
        if scored and scored[0][0] >= AUTO_SAME:
            assigned = scored[0][1]  # deterministic adopt, no LLM
        else:
            for overlap, cid in scored:
                if overlap < CANDIDATE_MIN:
                    break  # sorted descending; the rest cannot qualify
                concept = existing[cid]
                same = _ask_match(match_llm, fact["content"], concept,
                                  full=False)
                if same is not True:
                    # One 'no' proves nothing: re-ask with the FULL fact
                    # list. Only two 'no's reject this candidate.
                    same = _ask_match(match_llm, fact["content"], concept,
                                      full=True)
                if same is True:
                    assigned = cid
                    logger.debug("LLM matched %r -> concept %s",
                                 fact["content"][:60], cid)
                    break
        if assigned is not None:
            fact["concept_id"] = assigned
        elif scored and scored[0][0] >= CANDIDATE_MIN:
            # unassigned despite an in-band candidate: remember the closest
            # one so Pass 3 can warn if this fact coins a new concept
            rejected_with_candidates[id(fact)] = (
                scored[0][0], scored[0][1], fact["content"])

    # --- Pass 2: group the still-unassigned facts among themselves ---
    pending = [f for f in facts if "concept_id" not in f]
    parent = list(range(len(pending)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    tokens = [canonical_tokens(f["content"]) for f in pending]
    hints = [hint_tokens(f.get("topic_hint")) for f in pending]
    for i in range(len(pending)):
        for j in range(i + 1, len(pending)):
            same = False
            if hints[i] and hints[i] == hints[j]:
                same = True  # identical Extractor concept hint
            else:
                sim = jaccard(tokens[i], tokens[j])
                if sim >= AUTO_SAME:
                    same = True
                elif sim >= CANDIDATE_MIN:
                    try:
                        same = match_llm(pending[i]["content"], "",
                                         [pending[j]["content"]])
                    except ConceptError as exc:
                        logger.warning("concept match failed between two new "
                                       "facts (%s); keeping separate", exc)
                        same = False
            if same:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri

    # --- Pass 3: coin stable ids for the new concept groups ---
    taken = set(existing) | set(new_concepts)
    groups: dict[int, list[dict]] = {}
    for i, fact in enumerate(pending):
        groups.setdefault(find(i), []).append(fact)
    for members in groups.values():
        base = coining_slug(members)
        cid, n = base, 1
        while cid in taken:
            n += 1
            cid = f"{base}-{n}"
        taken.add(cid)
        new_concepts[cid] = humanize(cid)
        for member in members:
            member["concept_id"] = cid
        # A brand-new concept minted from a fact the match seam rejected
        # twice while an in-band candidate existed is a possible duplicate
        # document. Behaviour is unchanged (two deliberate 'no's reject the
        # candidate by design); the mint is only made visible.
        best = max((rejected_with_candidates[id(m)] for m in members
                    if id(m) in rejected_with_candidates), default=None)
        if best is not None:
            overlap, closest, content = best
            logger.warning(
                "coining new concept %r from fact %r that was rejected by "
                "the match seam twice despite %.2f overlap with existing "
                "concept %r — possible duplicate; review the taxonomy "
                "routing", cid, content[:60], overlap, closest)

    return facts, new_concepts


# ---------------------------------------------------------------------------
# Bundle I/O — the concept document body format (render + parse, one truth)
# ---------------------------------------------------------------------------

def check_fact(fact: dict, where: str) -> None:
    content = fact.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ConceptError(f"{where}: 'content' must be a non-empty string")
    if "\n" in content or "\r" in content:
        raise ConceptError(f"{where}: 'content' must be single-line")
    if content.startswith("- "):
        raise ConceptError(f"{where}: 'content' must not start with '- '")
    score = fact.get("confidence_score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) \
            or not 0.0 <= float(score) <= 1.0:
        raise ConceptError(f"{where}: 'confidence_score' must be in [0, 1], "
                           f"got {score!r}")
    if fact.get("status") not in FACT_STATUSES:
        raise ConceptError(f"{where}: status must be one of {FACT_STATUSES}")
    if fact.get("resolution") not in RESOLUTIONS:
        raise ConceptError(f"{where}: resolution must be one of {RESOLUTIONS}")
    if fact.get("first_seen") is not None and not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", str(fact["first_seen"])):
        raise ConceptError(f"{where}: first_seen must be YYYY-MM-DD")
    sources = fact.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ConceptError(f"{where}: 'sources' must be a non-empty list")
    for k, source in enumerate(sources):
        if not isinstance(source, dict):
            raise ConceptError(f"{where}.sources[{k}] is not an object")
        if not isinstance(source.get("url"), str) \
                or not source["url"].startswith(("http://", "https://")):
            raise ConceptError(f"{where}.sources[{k}]: invalid url")
        if source.get("source_type") not in SOURCE_TYPES:
            raise ConceptError(f"{where}.sources[{k}]: invalid source_type")
        if source.get("date") is not None and not isinstance(source["date"], str):
            raise ConceptError(f"{where}.sources[{k}]: date must be a string")


def check_concept(concept: dict) -> None:
    cid = concept.get("id")
    if not isinstance(cid, str) or not okf.SLUG_RE.fullmatch(cid):
        raise ConceptError(f"id must be a slug, got {cid!r}")
    if concept.get("type") not in (CONCEPT_DOC_TYPE, None):
        raise ConceptError(f"type must be {CONCEPT_DOC_TYPE!r}")
    facts = concept.get("facts")
    if not isinstance(facts, list) or not facts:
        raise ConceptError(f"{cid}: a concept needs at least one fact")
    for i, fact in enumerate(facts):
        check_fact(fact, f"{cid}.facts[{i}]")
    for i, conflict in enumerate(concept.get("conflicts", [])):
        check_fact(conflict, f"{cid}.conflicts[{i}]")


def _render_source(source: dict) -> str:
    date = source["date"] or "unknown"
    return f"{source['url']} ({source['source_type']}, fetched {date})"


def _parse_source(text: str) -> dict:
    url, _, rest = text.partition(" (")
    source_type, _, date = rest.partition(", fetched ")
    return {"url": url.strip(),
            "source_type": source_type.strip(),
            "date": None if date.strip().rstrip(")") == "unknown"
            else date.strip().rstrip(")")}


def render_fact(fact: dict) -> list[str]:
    lines = [f"- {fact['content']}",
             f"  - confidence_score: {float(fact['confidence_score']):.2f}",
             f"  - status: {fact['status']}",
             f"  - resolution: {fact['resolution']}"]
    if fact.get("first_seen"):
        lines.append(f"  - first_seen: {fact['first_seen']}")
    if fact.get("superseded_by"):
        lines.append(f"  - superseded_by: {fact['superseded_by']}")
    lines.append("  - sources: " + " ; ".join(
        _render_source(s) for s in fact["sources"]))
    return lines


def parse_fact_block(lines: list[str]) -> dict:
    """Parse one fact block: '- content' then '  - key: value' lines."""
    if not lines or not lines[0].startswith("- "):
        raise ConceptError(f"malformed fact block: {lines[:1]!r}")
    fact: dict = {"content": lines[0][2:].strip()}
    for line in lines[1:]:
        if not line.startswith("  - "):
            raise ConceptError(f"unexpected line in fact block: {line!r}")
        item = line[4:]
        key, _, value = item.partition(": ")
        if key == "sources":
            fact["sources"] = [_parse_source(part)
                               for part in value.split(" ; ") if part.strip()]
        elif key == "confidence_score":
            fact["confidence_score"] = float(value)
        else:
            fact[key] = value
    return fact


def render_body(concept: dict) -> str:
    """Render the concept document body (no frontmatter, no Related)."""
    body = [f"# {concept.get('title') or humanize(concept['id'])}", "",
            "## Details", "", "### Facts", ""]
    for fact in concept["facts"]:
        body.extend(render_fact(fact))
        body.append("")
    conflicts = concept.get("conflicts", [])
    if conflicts:
        body.extend(["### Conflicts", ""])
        for conflict in conflicts:
            body.extend(render_fact(conflict))
            body.append("")

    urls: dict[str, list] = {}
    for fact in concept["facts"]:
        for source in fact["sources"]:
            urls.setdefault(source["url"], [source, 0])
            urls[source["url"]][1] += 1
    body.extend(["## Sources", ""])
    for url in sorted(urls):
        source, count = urls[url]
        date = source["date"] or "unknown"
        body.append(f"- {url} — {source['source_type']}, fetched {date} "
                    f"(confirmed {count} fact{'s' if count != 1 else ''})")
    return "\n".join(body).rstrip("\n") + "\n"


def parse_body(text: str) -> tuple[list[dict], list[dict]]:
    """Parse a rendered body back into (facts, conflicts)."""
    facts: list[dict] = []
    conflicts: list[dict] = []
    section = None
    block: list[str] = []

    def flush() -> None:
        nonlocal block
        if not block:
            return
        if section == "facts":
            facts.append(parse_fact_block(block))
        elif section == "conflicts":
            conflicts.append(parse_fact_block(block))
        block = []

    for line in text.splitlines():
        if line.startswith("### "):
            flush()
            name = line[4:].strip().lower()
            section = name if name in ("facts", "conflicts") else None
            continue
        if line.startswith("## "):
            flush()
            section = None  # Sources/Related carry no fact blocks
            continue
        if section in ("facts", "conflicts"):
            if not line.strip():
                flush()  # blank line separates consecutive fact blocks
                continue
            if line.startswith("- "):
                flush()  # a new top-level fact starts a new block
                block.append(line)
            elif line.startswith("  - "):
                block.append(line)
            else:
                raise ConceptError(
                    f"unexpected body line in {section}: {line!r}")
    flush()
    return facts, conflicts


def related_link_targets(body: str) -> list[str]:
    """Every `./<slug>.md` cross-link target in a rendered body, in order.
    Used by the bundle lint to prove Related links resolve."""
    targets = []
    for token in re.findall(r"\]\((\./[a-z0-9-]+\.md)\)", body):
        targets.append(token[2:-3])
    return targets


def load_bundle(knowledge_dir: str | Path) -> dict[str, dict]:
    """Read every valid concept document in knowledge/ into {id: concept}.

    Files that do not parse as OKF concept documents (INDEX, CHANGELOG,
    pre-migration fragments) are skipped with a log line — they are not
    concept-layer data. This is the deterministic read of the maintained
    bundle that the Merger matches new facts against.
    """
    kdir = Path(knowledge_dir)
    bundle: dict[str, dict] = {}
    if not kdir.is_dir():
        return bundle
    for path in sorted(kdir.glob("*.md")):
        if path.name in (INDEX_NAME, CHANGELOG_NAME):
            continue
        try:
            meta, body = okf.parse(path.read_text(encoding="utf-8"), path)
        except (okf.OkfError, OSError):
            logger.debug("%s is not a valid OKF document; skipped by the "
                         "concept layer", path.name)
            continue
        if meta.get("type") != CONCEPT_DOC_TYPE:
            continue
        facts, conflicts = parse_body(body)
        bundle[meta["id"]] = {
            "id": meta["id"],
            "title": meta["title"],
            "type": meta["type"],
            "confidence": meta["confidence"],
            "status": meta["status"],
            "last_checked": meta["last_checked"],
            "sources": list(meta["sources"]),
            "facts": facts,
            "conflicts": conflicts,
        }
    return bundle


# ---------------------------------------------------------------------------
# CLI: bundle health check (used by tests and the rebuild driver)
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """CLI entry point: python3 -m pipeline.concepts [KNOWLEDGE_DIR]

    Parses every document in the bundle and prints a one-line summary per
    concept (id, facts, conflicts, sources). Exit 1 if any document fails
    concept-layer validation.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("knowledge_dir", nargs="?", default=None)
    args = parser.parse_args(argv)
    kdir = Path(args.knowledge_dir) if args.knowledge_dir else \
        Path(__file__).resolve().parents[1] / "knowledge"
    bundle = load_bundle(kdir)
    if not bundle:
        print(f"{kdir}: no concept documents found", file=sys.stderr)
        return 1
    failures = 0
    for cid, concept in sorted(bundle.items()):
        try:
            check_concept(concept)
        except ConceptError as exc:
            failures += 1
            print(f"INVALID {cid}: {exc}", file=sys.stderr)
            continue
        print(f"{cid}: {len(concept['facts'])} facts, "
              f"{len(concept['conflicts'])} conflicts, "
              f"{len(concept['sources'])} sources")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
