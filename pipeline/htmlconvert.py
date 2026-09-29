"""HTML -> Markdown conversion with ONLY the standard library (html.parser).

Used by the Fetch stage (review step 6): pages that arrive as HTML (the
direct-HTTP fallback for JS-rendered documentation such as the API release
notes) are converted here instead of being refused at Fetch. Best-effort and
honest: what converts to nothing meaningful stays labeled HTML and the
pipeline still stops rather than extracting from noise.

Kept: headings (h1-h6 -> #-######), paragraphs, lists (ul/ol/li -> "-" with
nesting indent), links ([text](href)), tables (rows as "cell | cell" lines).
Dropped: script/style/noscript/template/nav/header/footer/aside/form content
(boilerplate), comments, and all tags themselves.

Deterministic: same input -> same output, no network, no LLM.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# Below this much visible text the page is a shell (JS app, empty reply);
# converting it would hand the Extractor noise, so the fetcher keeps the
# honest "html" verdict and the pipeline stops there.
MIN_TEXT_CHARS = 40

SKIP_TAGS = frozenset({
    "script", "style", "noscript", "template", "nav", "header", "footer",
    "aside", "form", "svg", "iframe", "button", "select",
})
HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
BLOCK_TAGS = frozenset({"p", "div", "section", "article", "main", "table",
                        "tr", "blockquote", "figure", "dl", "dt", "dd",
                        "hgroup"}) | set(HEADING_TAGS)
LIST_TAGS = frozenset({"ul", "ol"})
WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
NEWLINES_RE = re.compile(r"\n{3,}")


class _Markdownizer(HTMLParser):
    """Streaming converter; call .markdown() when fed."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []       # finished lines
        self._text: list[str] = []      # in-flight inline text of one block
        self._skip_depth = 0            # inside a dropped subtree
        self._skip_list_depth = 0       # inside <ul>/<ol> inside a skip
        self._list_depth = 0
        self._in_link: str | None = None  # href of the open <a>
        self._link_text: list[str] = []
        self._table_cell: list[str] | None = None  # collecting td/th text
        self._table_row: list[str] | None = None
        self._in_heading: int | None = None
        self._in_li = 0

    # -- text assembly ------------------------------------------------

    def _flush_text(self) -> str:
        text = WHITESPACE_RE.sub(" ", "".join(self._text)).strip()
        self._text = []
        return text

    def _emit(self, line: str) -> None:
        if line.strip():
            self._out.append(line)

    def _emit_block(self) -> None:
        """Close the current in-flight block as a plain paragraph."""
        text = self._flush_text()
        if not text:
            return
        if self._in_li and self._list_depth:
            indent = "  " * (self._list_depth - 1)
            self._emit(f"{indent}- {text}")
        elif self._in_heading:
            self._emit("#" * self._in_heading + " " + text)
        else:
            self._emit(text)

    # -- parser hooks ---------------------------------------------------

    def handle_starttag(self, tag: str, attrs) -> None:
        if self._skip_depth:
            if tag in SKIP_TAGS:
                self._skip_depth += 1
            return
        if tag in SKIP_TAGS:
            self._emit_block()
            self._skip_depth = 1
            return
        if tag == "a":
            if self._in_link is None:  # nested <a>: keep the outer link
                self._in_link = dict(attrs).get("href")
                self._link_text = []
            return
        if tag in HEADING_TAGS:
            self._emit_block()
            self._in_heading = HEADING_TAGS[tag]
            return
        if tag == "li":
            self._emit_block()
            self._in_li += 1
            return
        if tag in LIST_TAGS:
            self._emit_block()
            self._list_depth += 1
            return
        if tag == "br":
            self._emit_block()
            return
        if tag == "td" or tag == "th":
            self._flush_text()
            if self._table_row is None:
                self._table_row = []
            self._table_cell = []
            return
        if tag == "tr":
            self._emit_block()
            self._table_row = []
            return
        if tag in BLOCK_TAGS:
            self._emit_block()

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth:
            if tag in SKIP_TAGS:
                self._skip_depth -= 1
            return
        if tag == "a":
            if self._in_link is not None:
                text = WHITESPACE_RE.sub(
                    " ", "".join(self._link_text)).strip()
                self._link_text = []
                href = self._in_link
                self._in_link = None
                if href and href.startswith(("http://", "https://", "#", "/")):
                    # visible link text becomes markdown; bare hrefs ride on
                    self._text.append(f"[{text or href}]({href})")
                elif text:
                    self._text.append(text)
            return
        if tag in HEADING_TAGS and self._in_heading:
            self._emit_block()
            self._in_heading = None
            return
        if tag == "li":
            self._emit_block()
            self._in_li = max(0, self._in_li - 1)
            return
        if tag in LIST_TAGS:
            self._emit_block()
            self._list_depth = max(0, self._list_depth - 1)
            return
        if tag in ("td", "th") and self._table_cell is not None:
            cell = WHITESPACE_RE.sub(" ", "".join(self._table_cell)).strip()
            self._table_cell = None
            if self._table_row is not None:
                self._table_row.append(cell or " ")
            return
        if tag == "tr" and self._table_row is not None:
            cells, self._table_row = self._table_row, None
            if cells:
                self._emit("| " + " | ".join(cells) + " |")
            return
        if tag == "table":
            self._emit_block()
            return
        if tag in BLOCK_TAGS:
            self._emit_block()

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data:
            return
        if self._table_cell is not None:
            self._table_cell.append(data)
            return
        if self._in_link is not None:
            self._link_text.append(data)
            return
        self._text.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._skip_depth:
            return
        self._text.append(f"&{name};")  # convert_charrefs handles most cases

    # -- entry ------------------------------------------------------------

    def markdown(self) -> str:
        self._emit_block()
        text = "\n\n".join(self._out)
        text = NEWLINES_RE.sub("\n\n", text)
        return text.strip() + "\n" if text.strip() else ""


def html_to_markdown(html: str) -> str:
    """Convert one HTML document to Markdown-ish text. Deterministic."""
    parser = _Markdownizer()
    parser.feed(html)
    parser.close()
    return parser.markdown()


def convertible(html: str) -> bool:
    """True when the HTML has enough visible text to be worth extracting:
    the Fetch stage converts only convertible pages and keeps the honest
    'html' label (pipeline stop) for shells and empty replies."""
    visible = html_to_markdown(html)
    return len(visible.strip()) >= MIN_TEXT_CHARS
