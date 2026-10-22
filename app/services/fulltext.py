"""Full text of arXiv papers, from arXiv's HTML rendering (LaTeXML).

Used only to give the summarizer concrete detail beyond the abstract; see
docs/rag-plan.md phase E. How this stays within arXiv's terms
(https://info.arxiv.org/help/api/tou.html, /help/bulk_data.html):

- Fetches go to `export.arxiv.org`, the host arXiv asks programmatic
  clients to use, through the same process-wide gate as the search API
  (`app.services.arxiv.throttle`: one request at a time, at least
  `arxiv_page_delay` seconds apart).
- Only newly ingested papers are fetched - a few dozen a day - never the
  corpus. arXiv points bulk full-text access to its S3 / Kaggle dumps.
- Text is stored locally for personal use ("retrieve, store, and use the
  content ... for your own personal use"). It is never sent anywhere:
  emails carry only the model's own summary and a link back to arXiv,
  because serving e-print content needs the copyright holder's permission.

No DB imports: fetch, parse and chunk are plain functions, so they test
without a database or network.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

import httpx

from app.config import Settings, get_settings
from app.services.arxiv import throttle

USER_AGENT = "research-digest-agent/1.0 (self-hosted personal research digest)"

# Containers whose text never goes into chunks: front matter (authors, the
# abstract we already have), references, footnotes, navigation, tables.
_SKIP_CLASSES = {
    "ltx_authors", "ltx_abstract", "ltx_bibliography", "ltx_note", "ltx_page_navbar",
    "ltx_dates", "ltx_tabular", "ltx_page_footer", "ltx_role_footnote", "ltx_TOC",
}
_HEADING_CLASSES = ("ltx_title_section", "ltx_title_subsection", "ltx_title_appendix")
_VOID = {"br", "img", "hr", "meta", "link", "input", "col", "area", "base", "source", "wbr"}
_MAX_MATH_CHARS = 60  # longer formulas become "[formula]"


@dataclass(frozen=True)
class Section:
    title: str
    paragraphs: tuple[str, ...]


@dataclass(frozen=True)
class Chunk:
    section: str
    text: str


class _LaTeXMLText(HTMLParser):
    """Collects section titles and paragraph/caption text from LaTeXML HTML.
    Math is replaced by its TeX `alttext` (short formulas) so equations read
    as `$u_t = \\Delta u$` instead of a MathML dump."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: list[tuple[str, list[str]]] = [("", [])]
        self._stack: list[tuple[str, bool]] = []  # (tag, opened a skip/heading/text region)
        self._skip = 0
        self._math = 0
        self._heading: list[str] | None = None
        self._para: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            return
        a = dict(attrs)
        classes = set((a.get("class") or "").split())
        marker = ""
        if self._skip or classes & _SKIP_CLASSES or tag in ("nav", "header", "footer", "script", "style"):
            self._skip += 1
            marker = "skip"
        elif tag == "math":
            self._math += 1
            marker = "math"
            tex = (a.get("alttext") or "").strip()
            target = self._heading if self._heading is not None else self._para
            if target is not None and self._math == 1:
                target.append(f"${tex}$" if tex and len(tex) <= _MAX_MATH_CHARS else "[formula]")
        elif self._math:
            pass
        elif tag in ("h2", "h3", "h4") and any(c in classes for c in _HEADING_CLASSES):
            self._heading = []
            marker = "heading"
        elif tag == "p" and "ltx_p" in classes and self._heading is None:
            self._para = []
            marker = "para"
        elif tag == "figcaption":
            self._para = ["Caption:"]
            marker = "para"
        self._stack.append((tag, marker))

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        # Pop to the matching tag (LaTeXML output is well formed; this just
        # tolerates the odd stray end tag).
        while self._stack:
            open_tag, marker = self._stack.pop()
            self._close(marker)
            if open_tag == tag:
                break

    def _close(self, marker: str) -> None:
        if marker == "skip":
            self._skip -= 1
        elif marker == "math":
            self._math -= 1
        elif marker == "heading" and self._heading is not None:
            title = _clean(" ".join(self._heading))
            self.sections.append((title, []))
            self._heading = None
        elif marker == "para" and self._para is not None:
            text = _clean(" ".join(self._para))
            if text and text != "Caption:":
                self.sections[-1][1].append(text)
            self._para = None

    def handle_data(self, data):
        if self._skip or self._math:
            return
        if self._heading is not None:
            self._heading.append(data)
        elif self._para is not None:
            self._para.append(data)


def _clean(text: str) -> str:
    text = " ".join(text.split())
    return re.sub(r"\s+([,.;:)])", r"\1", text)


def parse_sections(html: str) -> list[Section]:
    """The paper's body as (section title, paragraphs), in order. Skips the
    abstract, references, footnotes and tables; keeps figure captions."""
    parser = _LaTeXMLText()
    parser.feed(html)
    parser.close()
    return [Section(title, tuple(paras)) for title, paras in parser.sections if paras]


def chunk_sections(sections: list[Section], max_chars: int = 1200, max_chunks: int = 80) -> list[Chunk]:
    """Pack each section's paragraphs into chunks of about `max_chars`,
    never crossing a section boundary, so every chunk knows its section. A
    single over-long paragraph is split at sentence ends. At most
    `max_chunks` (long papers are mostly appendices by then)."""
    chunks: list[Chunk] = []
    for section in sections:
        buf = ""
        for para in section.paragraphs:
            for piece in _split_long(para, max_chars):
                if buf and len(buf) + 1 + len(piece) > max_chars:
                    chunks.append(Chunk(section.title, buf))
                    buf = piece
                else:
                    buf = f"{buf} {piece}".strip()
        if buf:
            chunks.append(Chunk(section.title, buf))
    return chunks[:max_chunks]


def _split_long(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    out, buf = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if buf and len(buf) + 1 + len(sentence) > max_chars:
            out.append(buf)
            buf = sentence
        else:
            buf = f"{buf} {sentence}".strip()
    if buf:
        out.append(buf)
    return [piece[:max_chars] for piece in out]


def fetch_html(
    arxiv_id: str, *, settings: Settings | None = None, client: httpx.Client | None = None
) -> str | None:
    """The paper's LaTeXML HTML, or None when arXiv has none for it (not
    every submission converts). Other failures raise, for the caller's
    retry policy."""
    settings = settings or get_settings()
    throttle(settings.arxiv_page_delay)
    owns_client = client is None
    client = client or httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        resp = client.get(
            f"{settings.arxiv_html_url.rstrip('/')}/{arxiv_id}", headers={"User-Agent": USER_AGENT}
        )
    finally:
        if owns_client:
            client.close()
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    # A converted paper is LaTeXML output; anything else (an error or
    # "no HTML for this paper" page served with 200) counts as unavailable.
    return resp.text if "ltx_page_main" in resp.text or "LaTeXML" in resp.text else None
