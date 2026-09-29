"""Topic taxonomy: the fixed set of CONCEPT identities for the bundle.

WHY (review step 3): concept identity used to be coined from the leading
words of the first extracted sentence, so the bundle grew one
sentence-shaped document per fact (37 files, 23 single-fact) with
near-duplicate clusters. Identity now comes from a FIXED taxonomy of topics:
one topic = one concept document = one stable slug. Slugs NEVER come from a
fact's wording; a reworded extraction routes to the same topic and therefore
updates the same document.

Routing (each new fact -> exactly one topic):
  1. DETERMINISTIC: keyword-overlap scoring against every topic's keyword
     set (raw + stemmed tokens). A topic wins outright when it matches at
     least TOPIC_MIN_HITS distinct keywords and strictly more than every
     other topic.
  2. AMBIGUOUS BAND: no outright winner (top scores tie, or the best score
     is below TOPIC_MIN_HITS) -> ONE LLM call choosing from the candidate
     list (every topic that scored at least 1, else the full taxonomy).
  3. If the seam fails, the fact falls through to the legacy overlap
     matcher in pipeline/concepts.py — never guessed here.

Catch-all cap (MAX_TOPIC_FACTS): a topic that would exceed the cap is split
by its declared SUB-TOPICS (taxonomy-declared, never by sentence). Facts
matching a sub-topic move to "{topic}-{subtopic}" concepts; the rest stay in
the parent. Topics that can grow past the cap MUST declare subtopics — the
split is a taxonomy decision, not a runtime heuristic.

The taxonomy is data: adjusting coverage means editing TOPICS below, and
concept ids stay stable because they are the topic slugs themselves.

LLM seam: claude_cli_choose_topic — mock it in tests.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field

from pipeline.validator import TOKEN_RE, canonical_tokens, stem

logger = logging.getLogger("pipeline.topics")

LLM_TIMEOUT = 120  # seconds, one-shot claude call per ambiguous fact

# Deterministic routing bands. A keyword PHRASE matches only when EVERY one
# of its canonical tokens is present in the claim — single words cannot leak
# out of a phrase ("login with amazon" cannot match on "amazon" alone), so
# one full-phrase match is already strong, decisive evidence.
TOPIC_MIN_HITS = 1      # matched phrases needed to win outright (ties -> LLM)
SUBTOPIC_MIN_HITS = 1   # matched phrases needed to claim a fact in a split
MAX_TOPIC_FACTS = 12    # catch-all cap: split by sub-topics beyond this


@dataclass(frozen=True)
class Topic:
    slug: str
    title: str
    keywords: tuple[str, ...]
    subtopics: tuple["Topic", ...] = field(default=())

    def keyword_phrases(self) -> tuple[frozenset[str], ...]:
        """Canonical token sets of every keyword phrase, deduped — synonyms
        that fold to the same set ('report', 'reporting', 'reports') count
        once, and stopwords are dropped EXACTLY as they are dropped from
        claim text (a phrase containing 'with' must still match a claim
        whose tokens no longer contain 'with'). A phrase matches a claim
        when the claim's token set contains ALL of its tokens."""
        return tuple(dict.fromkeys(
            canonical_tokens(kw) for kw in self.keywords))


# ---------------------------------------------------------------------------
# The taxonomy (adjust to what the sources actually cover; ids are stable)
# ---------------------------------------------------------------------------

TOPICS: tuple[Topic, ...] = (
    Topic(
        slug="sponsored-display",
        title="Sponsored Display",
        keywords=("sponsored display", "sponsored brands", "audience",
                  "get started"),
    ),
    Topic(
        slug="reporting-api",
        title="Reporting API",
        keywords=("reporting", "report", "reports", "asynchronous",
                  "asynchronously", "metrics", "cross-account",
                  "multi-dimensional", "report requests", "performance data",
                  "performance metrics", "snapshot"),
    ),
    Topic(
        slug="amazon-marketing-cloud-and-stream",
        title="Amazon Marketing Cloud and Stream",
        keywords=("marketing stream", "stream", "hourly", "near real time",
                  "real time", "realtime", "notifications", "aws account",
                  "marketing cloud", "cloud", "amc", "pseudonymized",
                  "analytics", "audience building", "clean room"),
        # merged 2026-09-30 from two 1-2-fact product topics to hold the
        # bundle at 15 documents; both products' claims stay distinct facts
    ),
    Topic(
        slug="bulksheets-and-bulk-operations",
        title="Bulksheets and Bulk Operations",
        keywords=("bulksheets", "bulksheet", "spreadsheet", "bulk operations",
                  "bulk", "sheets", "video tutorials", "tutorials",
                  "seller central", "vendor central"),
    ),
    Topic(
        slug="api-access-and-onboarding",
        title="API Access and Onboarding",
        keywords=("access", "onboarding", "onboard", "application", "apply",
                  "applying", "approval", "eligible",
                  "registration", "register", "test accounts", "test",
                  "business day", "integrators",
                  # "client application" is deliberately NOT here: it is
                  # Login-with-Amazon vocabulary and lives in that topic
                  # fees are part of getting/using access (merged 2026-09-30
                  # from a 1-fact api-fees-and-pricing topic to stay at 15
                  # documents): "no additional fees to use the API"
                  "fees", "no additional fees", "account fees",
                  "campaign costs", "free of charge"),
    ),
    Topic(
        slug="login-with-amazon",
        title="Login with Amazon",
        keywords=("login with amazon", "lwa", "client application",
                  "client id", "authorization", "credential"),
    ),
    Topic(
        slug="mcp-server-and-developer-tools",
        title="MCP Server and Developer Tools",
        keywords=("mcp", "model context protocol", "integration dashboard",
                  "developers portal", "developer portal",
                  "developer resources", "advanced tools",
                  "advanced tools center", "advanced tools guide",
                  "technology solutions", "server",
                  "portal", "code samples", "supplements",
                  # the Ads Events API GTM tag repo is Ads developer tooling
                  "gtm", "ads-events-api", "smarty"),
    ),
    Topic(
        slug="github-repos-and-sdks",
        title="GitHub Repositories and SDKs",
        keywords=("github", "repository", "repositories", "repo",
                  "organization", "amzn", "open-source", "followers",
                  "stars", "forks", "languages", "archived", "licensed",
                  "license", "mit", "apache"),
    ),
    Topic(
        slug="amazon-aps-swift-packages",
        title="Amazon APS Swift Packages",
        keywords=("swift", "package manager", "aps", "verve", "mobilefuse",
                  "inmobi", "publisher", "swift packages"),
    ),
    Topic(
        slug="selling-partner-api",
        title="Selling Partner API",
        keywords=("selling partner", "selling partner api", "sp-api",
                  "openapi", "postman", "agentic", "toolkit", "sdk",
                  "models", "stars", "forks"),
        subtopics=(
            Topic(slug="docs-and-models", title="Selling Partner API Docs and Models",
                  keywords=("docs", "documentation", "models", "openapi")),
            Topic(slug="sdks-and-tools", title="Selling Partner API SDKs and Tools",
                  keywords=("sdk", "postman", "agentic", "toolkit")),
        ),
    ),
    Topic(
        slug="programmatic-and-dsp",
        title="Programmatic Advertising and DSP",
        keywords=("programmatic", "dsp", "campaign management",
                  "export apis", "exports", "campaign model", "common model",
                  "endpoints", "cross ad product", "media buying",
                  "automating", "automation", "streaming tv",
                  "programmatically"),
    ),
    Topic(
        slug="api-release-notes",
        title="API Release Notes",
        keywords=("release notes", "release", "released", "rss", "feed",
                  "subscription", "subscribe", "changelog", "deprecation",
                  "announced", "deprecated", "removed", "added"),
        subtopics=(
            Topic(slug="version-updates", title="API Release Notes Version Updates",
                  keywords=("version", "versions", "v2", "v1", "released",
                            "updated", "endpoint", "endpoints", "deprecated",
                            "removed", "added", "breaking")),
            Topic(slug="subscription-feeds", title="API Release Notes Feeds",
                  keywords=("rss", "feed", "feeds", "subscribe",
                            "subscription")),
        ),
    ),
    Topic(
        slug="developer-guides-and-events",
        title="Developer Guides and Events",
        keywords=("developer guide", "developer guides", "translations",
                  "languages", "unboxed", "developer track",
                  "breakout sessions", "workshops", "hackathon", "expansion",
                  "locales", "global expansion", "opportunities",
                  "advancing", "further their technological"),
    ),
    Topic(
        slug="partner-directory-and-support",
        title="Partner Directory and Support",
        keywords=("partner directory", "partners", "support", "technical",
                  "case study", "service model", "marketplaces", "filter",
                  "agencies", "solution providers", "bidx", "saras"),
    ),
    Topic(
        slug="amazon-ads-api-overview",
        title="Amazon Ads API Overview",
        keywords=("rest api", "rest", "use cases", "custom", "rules",
                  "optimizations", "supported products", "signals", "supply",
                  "retrieve", "programmatic access", "plan", "activate",
                  "measure", "advertising solution", "technology"),
    ),
)

TOPIC_BY_SLUG: dict[str, Topic] = {}


def _index_topics() -> None:
    TOPIC_BY_SLUG.clear()
    for topic in TOPICS:
        TOPIC_BY_SLUG[topic.slug] = topic
        for sub in topic.subtopics:
            TOPIC_BY_SLUG[f"{topic.slug}-{sub.slug}"] = sub


_index_topics()

FALLBACK_TOPIC = "amazon-ads-api-overview"  # the general topic; the relevance
# gate (pipeline/relevance.py) drops off-topic claims BEFORE routing, so an
# on-topic claim that scores zero keywords everywhere still belongs here.


def topic_title(slug: str) -> str | None:
    """Declared title for a topic or sub-topic slug (None if unknown)."""
    topic = TOPIC_BY_SLUG.get(slug)
    return topic.title if topic else None


def routeable_slugs() -> list[str]:
    """Every concept id the taxonomy can produce, top-level topics only
    (sub-topic ids are only minted by the catch-all cap)."""
    return [t.slug for t in TOPICS]


# ---------------------------------------------------------------------------
# LLM seam — topic choice for ambiguous facts. Mock this in tests.
# ---------------------------------------------------------------------------

CHOOSE_TOPIC_PROMPT = """You are the topic-routing seam of an Amazon Ads knowledge pipeline.
Decide which ONE topic a single factual claim belongs to. Topics are the fixed
identities of the knowledge documents; the claim's wording must NOT influence
the topic slug — only pick from the list.

Candidates (slug — title):
{candidates}

Rules:
- Pick the topic the claim is MOST about, even if it only partially fits.
- Never invent a slug outside the list; return one of the candidates.
- Do not judge truth, confidence, or conflicts; you only choose a topic.

Return ONLY one JSON object, no prose: {{"topic": "<slug>"}}

CLAIM: {claim}
"""


def claude_cli_choose_topic(claim: str, candidates: list[tuple[str, str]]) -> str:
    """LLM seam: one-shot headless Claude call choosing a topic slug, pinned
    to the read-only topic-router agent (.claude/agents/topic-router.md)."""
    if shutil.which("claude") is None:
        raise ValueError("no LLM backend available (claude CLI not found)")
    listing = "\n".join(f"- {slug} — {title}" for slug, title in candidates)
    prompt = CHOOSE_TOPIC_PROMPT.format(candidates=listing, claim=claim)
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--agent", "topic-router"],
            capture_output=True, text=True, timeout=LLM_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ValueError(f"LLM timed out after {LLM_TIMEOUT}s") from None
    if proc.returncode != 0:
        raise ValueError(f"claude CLI exited {proc.returncode}: "
                         f"{proc.stderr.strip()[:200]}")
    return _parse_topic(proc.stdout, [slug for slug, _ in candidates])


def _parse_topic(text: str, candidates: list[str]) -> str:
    stripped = text.strip()
    if "```" in stripped:
        blocks = [b for b in stripped.split("```") if b.strip()]
        stripped = max(blocks, key=len).strip()
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        raise ValueError("LLM returned invalid JSON") from None
    if isinstance(data, dict) and data.get("topic") in candidates:
        return data["topic"]
    raise ValueError(
        f'LLM JSON must be {{"topic": ...}} with a slug from the candidate '
        f"list, got {text.strip()[:120]!r}")


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _text_tokens(text: str) -> frozenset[str]:
    """Canonical (stemmed) tokens of the routing text (claim + topic hint)."""
    return canonical_tokens(text)


def score_topics(text: str, topics: tuple[Topic, ...] = TOPICS) -> list[tuple[int, str]]:
    """(matched-phrase-count, slug) for every topic against the text, sorted
    best-first (ties broken by declared taxonomy order — deterministic)."""
    tokens = _text_tokens(text)
    scored = []
    for position, topic in enumerate(topics):
        hits = sum(1 for phrase in topic.keyword_phrases() if phrase <= tokens)
        scored.append((-hits, position, topic.slug))
    scored.sort()
    return [(-neg_hits, slug) for neg_hits, _, slug in scored]


def route_fact(
    content: str,
    topic_hint: str | None = None,
    topics: tuple[Topic, ...] = TOPICS,
    choose_llm=claude_cli_choose_topic,
) -> tuple[str, str]:
    """Route one claim to (slug, decided_by) where decided_by is
    'keywords' | 'llm' | 'fallback'. Deterministic scoring first; exactly ONE
    LLM call, and only in the ambiguous band. Seam failure falls back to the
    general overview topic (never a guess from wording)."""
    text = f"{content} {topic_hint or ''}"
    ranked = score_topics(text, topics)
    best_hits, best_slug = ranked[0]
    second_hits = ranked[1][0] if len(ranked) > 1 else 0
    if best_hits >= TOPIC_MIN_HITS and best_hits > second_hits:
        return best_slug, "keywords"
    title_for = {t.slug: t.title for t in topics}
    candidates = [(slug, title_for[slug]) for hits, slug in ranked if hits >= 1]
    if not candidates:
        candidates = [(t.slug, t.title) for t in topics]
    try:
        slug = choose_llm(content, candidates)
    except ValueError as exc:
        logger.warning("topic routing seam failed for %r (%s); falling back "
                       "to %s", content[:60], exc, FALLBACK_TOPIC)
        return FALLBACK_TOPIC, "fallback"
    if slug not in title_for:
        logger.warning("seam returned unknown topic %r; falling back to %s",
                       slug, FALLBACK_TOPIC)
        return FALLBACK_TOPIC, "fallback"
    return slug, "llm"


def split_over_cap(concept: dict) -> list[dict]:
    """Catch-all cap applied to ONE MERGED concept: when its fact count
    exceeds MAX_TOPIC_FACTS and its topic declares sub-topics, re-route the
    facts by sub-topic keyword hits (>= 1 hit claims a fact; facts matching
    no sub-topic stay in the parent). Returns the resulting concept list
    ([the original] when no split applies). Conflict history stays with the
    parent concept. Splitting is by taxonomy sub-topics ONLY — never by
    sentence — and is deterministic, so re-runs split identically."""
    cid = concept.get("id", "")
    topic = TOPIC_BY_SLUG.get(cid)
    if topic is None or not topic.subtopics:
        return [concept]
    if len(concept.get("facts", [])) <= MAX_TOPIC_FACTS:
        return [concept]

    buckets: dict[str, list[dict]] = {topic.slug: []}
    for sub in topic.subtopics:
        buckets[f"{topic.slug}-{sub.slug}"] = []
    for fact in concept["facts"]:
        text = fact.get("content", "")
        tokens = _text_tokens(text)
        best: tuple[int, Topic] | None = None
        for sub in topic.subtopics:
            hits = sum(1 for phrase in sub.keyword_phrases()
                       if phrase <= tokens)
            if hits >= SUBTOPIC_MIN_HITS and (best is None or hits > best[0]):
                best = (hits, sub)
        if best is not None:
            buckets[f"{topic.slug}-{best[1].slug}"].append(fact)
        else:
            buckets[topic.slug].append(fact)

    out: list[dict] = []
    for slug, facts in buckets.items():
        if not facts:
            continue
        sub = TOPIC_BY_SLUG.get(slug)
        out.append({
            "id": slug,
            "title": (sub or topic).title,
            "type": "concept",
            "facts": facts,
            "conflicts": list(concept.get("conflicts", [])) if slug == topic.slug else [],
        })
    if len(buckets[topic.slug]) > MAX_TOPIC_FACTS:
        logger.warning("topic %s still holds %d facts after its sub-topic "
                       "split; extend its sub-topics in the taxonomy",
                       topic.slug, len(buckets[topic.slug]))
    if not buckets[topic.slug]:
        logger.warning("topic %s split left the parent empty; its conflict "
                       "history moves to the largest sub-topic", topic.slug)
        largest = max(out, key=lambda c: len(c["facts"]))
        largest["conflicts"] = list(concept.get("conflicts", []))
    return out
