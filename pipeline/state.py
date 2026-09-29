"""Persistent fetch-state: remembers what was fetched and classifies change.

Deterministic file I/O + hash comparison only — no AI/LLM judgment. For each
source URL, the last fetch outcome is stored as human-readable JSON; on a
re-fetch, the new content hash is classified against the stored hash:

  new        no committed hash (first fetch, or only failed attempts so far)
  unchanged  new hash equals the committed hash
  changed    new hash differs from the committed hash
  error      this fetch attempt failed (a previously committed hash is kept)

TWO-PHASE COMMIT (crash safety): a newly fetched hash is recorded as
*pending*, not committed. It is promoted to the committed hash only by
commit_state(), which the orchestrator calls AFTER the pipeline published
successfully. If anything downstream of Fetch fails (extract, merge,
publish, a crash), the committed hash still holds the previous content, so
the next run classifies the source as new/changed again and retries — a
failed run can never leave a source looking "already processed".

State file shape (JSON, keys sorted, 2-space indent — byte-identical output
for identical state):

    {
      "https://example.com/page": {
        "fetched_at": "2026-09-26T12:00:00+00:00",
        "sha256": "<hex digest>",
        "status": "ok",
        "strategy": "tvly-advanced",
        "pending_sha256": "<hex digest>",      # present until committed
        "pending_fetched_at": "2026-09-26T13:00:00+00:00",
        "pending_strategy": "tvly-advanced"
      }
    }

A missing state file is an empty state; a corrupt one is a hard error (never
silently reset — that would lose the change-detection baseline).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


class StateError(RuntimeError):
    """Raised when an existing state file cannot be parsed."""


@dataclass
class SourceState:
    """Last known fetch outcome for one source URL."""

    url: str
    status: str  # "ok" | "error"
    fetched_at: str  # ISO-8601 instant of the latest attempt
    sha256: str | None = None  # committed hash; None until first commit
    strategy: str | None = None
    error: str | None = None
    # Pending fetch: seen and cached, but the pipeline has not yet published
    # it successfully. current_content_sha256() prefers it.
    pending_sha256: str | None = None
    pending_fetched_at: str | None = None
    pending_strategy: str | None = None

    @property
    def current_content_sha256(self) -> str | None:
        """Hash of the content the pipeline should be working on: the pending
        version when one exists (fetched, awaiting publication), else the
        committed one."""
        return self.pending_sha256 or self.sha256

    def to_json(self) -> dict:
        out: dict = {"status": self.status, "fetched_at": self.fetched_at}
        if self.sha256 is not None:
            out["sha256"] = self.sha256
        if self.strategy is not None:
            out["strategy"] = self.strategy
        if self.error is not None:
            out["error"] = self.error
        if self.pending_sha256 is not None:
            out["pending_sha256"] = self.pending_sha256
        if self.pending_fetched_at is not None:
            out["pending_fetched_at"] = self.pending_fetched_at
        if self.pending_strategy is not None:
            out["pending_strategy"] = self.pending_strategy
        return out

    @classmethod
    def from_json(cls, url: str, data: object) -> SourceState:
        if not isinstance(data, dict):
            raise StateError(f"state entry for {url!r} is not a JSON object")
        for key in ("status", "fetched_at"):
            if key not in data:
                raise StateError(f"state entry for {url!r} is missing {key!r}")
        return cls(
            url=url,
            status=data["status"],
            fetched_at=data["fetched_at"],
            sha256=data.get("sha256"),
            strategy=data.get("strategy"),
            error=data.get("error"),
            pending_sha256=data.get("pending_sha256"),
            pending_fetched_at=data.get("pending_fetched_at"),
            pending_strategy=data.get("pending_strategy"),
        )


def load_state(path: str | Path) -> dict[str, SourceState]:
    """Load {url: SourceState}. Missing file -> empty state; corrupt -> error."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateError(f"{path}: corrupt state JSON ({exc})") from None
    if not isinstance(data, dict):
        raise StateError(f"{path}: top-level JSON must be an object")
    return {url: SourceState.from_json(url, entry) for url, entry in data.items()}


def save_state(path: str | Path, states: dict[str, SourceState]) -> None:
    """Write sorted, indented JSON atomically (temp file + rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {url: state.to_json() for url, state in states.items()}
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def classify(stored: SourceState | None, result: dict) -> str:
    """Classify one fetch_many() result entry against the stored record.

    Only the COMMITTED hash participates: a pending hash from a run that
    failed to publish must not make the next run think the source is done.
    """
    if not result.get("ok"):
        return "error"
    if stored is None or stored.sha256 is None:
        return "new"
    if stored.sha256 == result.get("sha256"):
        return "unchanged"
    return "changed"


def apply_result(states: dict[str, SourceState], result: dict, fetched_at: str) -> str:
    """Merge one fetch result into `states`; returns the verdict.

    new/changed content is recorded as PENDING — the committed hash moves
    only via commit_state() after the pipeline published successfully.
    """
    url = result["url"]
    prior = states.get(url)
    verdict = classify(prior, result)
    when = result.get("fetched_at", fetched_at)
    if verdict == "error":
        # Keep the committed hash (and any pending hash) so change detection
        # survives transient failures.
        states[url] = SourceState(
            url=url, status="error", fetched_at=when,
            sha256=prior.sha256 if prior else None,
            strategy=prior.strategy if prior else None,
            error=result.get("error", "fetch failed"),
            pending_sha256=prior.pending_sha256 if prior else None,
            pending_fetched_at=prior.pending_fetched_at if prior else None,
            pending_strategy=prior.pending_strategy if prior else None,
        )
    elif verdict == "unchanged":
        # The committed hash was re-seen: refresh the attempt timestamp and
        # drop any stale pending version (that content is no longer live).
        states[url] = SourceState(
            url=url, status="ok", fetched_at=when,
            sha256=prior.sha256, strategy=result.get("strategy"),
        )
    else:
        # new or changed: stage the hash as pending, committed untouched.
        states[url] = SourceState(
            url=url, status="ok", fetched_at=when,
            sha256=prior.sha256 if prior else None,
            strategy=prior.strategy if prior else None,
            pending_sha256=result["sha256"],
            pending_fetched_at=when,
            pending_strategy=result.get("strategy"),
        )
    return verdict


def update_state(path: str | Path, results: list[dict], fetched_at: str) -> dict[str, str]:
    """Record a batch of fetch results; returns {url: verdict}."""
    states = load_state(path)
    verdicts = {r["url"]: apply_result(states, r, fetched_at) for r in results}
    save_state(path, states)
    return verdicts


def commit_state(path: str | Path, urls: list[str] | None = None) -> list[str]:
    """Promote pending fetches to committed. Called by the orchestrator only
    after the pipeline published successfully for those URLs.

    `urls` limits the commit to those sources (a URL whose extraction failed
    must keep its pending state so the next run retries it); None commits
    every pending entry. Returns the list of URLs actually promoted.
    """
    states = load_state(path)
    promoted: list[str] = []
    for url, state in states.items():
        if urls is not None and url not in urls:
            continue
        if state.pending_sha256 is None:
            continue
        states[url] = SourceState(
            url=url, status="ok",
            fetched_at=state.pending_fetched_at or state.fetched_at,
            sha256=state.pending_sha256,
            strategy=state.pending_strategy or state.strategy,
        )
        promoted.append(url)
    if promoted:
        save_state(path, states)
    return promoted


def cache_content(
    cache_dir: str | Path, content: str, sha256: str, content_type: str = "markdown"
) -> Path:
    """Persist fetched content to <cache_dir>/<sha256>.<ext> (content-addressed).

    Idempotent: a file for an already-seen hash is never rewritten, so
    historical versions accumulate instead of being overwritten. Only
    successful fetches reach this function.
    """
    ext = "md" if content_type == "markdown" else "html"
    path = Path(cache_dir) / f"{sha256}.{ext}"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return path
