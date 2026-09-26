"""OKF (Open Knowledge Format) frontmatter parsing, validation, serialization.

Frontmatter is a deliberately small YAML subset: `key: value` lines plus
`- item` lists under a key. Full YAML is intentionally not supported.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

CONFIDENCE_LEVELS = ("low", "medium", "high")
STATUSES = ("official", "community", "inferred")

# Keys in canonical output order. `sources` is a list; everything else scalar.
KEY_ORDER = ("id", "title", "sources", "confidence", "status", "last_checked")

REQUIRED = set(KEY_ORDER)


class OkfError(ValueError):
    """Raised when a document violates the OKF contract."""


def _fail(path: Path | None, msg: str) -> OkfError:
    where = f"{path}: " if path else ""
    return OkfError(f"{where}{msg}")


def parse(text: str, path: Path | None = None) -> tuple[dict, str]:
    """Parse document text into (meta, body). Raises OkfError."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise _fail(path, "missing frontmatter (must start with '---')")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise _fail(path, "unterminated frontmatter (no closing '---')") from None

    meta: dict = {}
    current_key: str | None = None
    for lineno, raw in enumerate(lines[1:end], start=2):
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            if current_key is None:
                raise _fail(path, f"line {lineno}: list item outside any key")
            meta[current_key].append(_unquote(stripped[2:].strip()))
            continue
        if ":" not in stripped:
            raise _fail(path, f"line {lineno}: not 'key: value' or '- item': {stripped!r}")
        key, _, value = stripped.partition(":")
        key, value = key.strip(), value.strip()
        if key not in REQUIRED:
            raise _fail(path, f"line {lineno}: unknown key {key!r}")
        if key in meta:
            raise _fail(path, f"line {lineno}: duplicate key {key!r}")
        if value:
            meta[key] = _unquote(value)
            current_key = None
        else:
            meta[key] = []
            current_key = key

    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    validate(meta, path=path)
    return meta, body


def validate(meta: dict, path: Path | None = None) -> None:
    missing = REQUIRED - meta.keys()
    if missing:
        raise _fail(path, f"missing required keys: {sorted(missing)}")

    if not (isinstance(meta["id"], str) and SLUG_RE.fullmatch(meta["id"])):
        raise _fail(path, f"id must match {SLUG_RE.pattern}, got {meta['id']!r}")

    sources = meta["sources"]
    if not isinstance(sources, list) or not sources:
        raise _fail(path, "sources must be a non-empty list of URLs")
    for url in sources:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise _fail(path, f"source is not an http(s) URL: {url!r}")

    if meta["confidence"] not in CONFIDENCE_LEVELS:
        raise _fail(path, f"confidence must be one of {CONFIDENCE_LEVELS}")
    if meta["status"] not in STATUSES:
        raise _fail(path, f"status must be one of {STATUSES}")
    if not (isinstance(meta["last_checked"], str) and DATE_RE.fullmatch(meta["last_checked"])):
        raise _fail(path, f"last_checked must be YYYY-MM-DD, got {meta['last_checked']!r}")
    try:
        datetime.date.fromisoformat(meta["last_checked"])
    except ValueError:
        raise _fail(path, f"last_checked is not a real date: {meta['last_checked']!r}") from None


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def serialize(meta: dict, body: str) -> str:
    """Render meta+body back to document text in canonical key order."""
    validate(meta)
    out = ["---"]
    for key in KEY_ORDER:
        value = meta[key]
        if isinstance(value, list):
            out.append(f"{key}:")
            out.extend(f"  - {item}" for item in value)
        else:
            out.append(f"{key}: {value}")
    out.append("---")
    body = body.strip("\n")
    return "\n".join(out) + "\n\n" + body + "\n"
