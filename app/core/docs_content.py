"""
Documentation Content Loader

Reads markdown files from docs/content/, parses metadata headers,
and builds an in-memory index for the documentation API.
"""

import html
import re
from pathlib import Path
from dataclasses import dataclass, field

DOCS_DIR = Path(__file__).parent.parent.parent / "docs" / "content"

# Category ordering and labels
CATEGORIES = [
    {"id": "getting-started", "label": "Getting Started", "order": 0},
    {"id": "connectors", "label": "Connectors", "order": 1},
    {"id": "features", "label": "Features", "order": 2},
    {"id": "api-reference", "label": "API Reference", "order": 3},
]

CATEGORY_MAP = {c["id"]: c for c in CATEGORIES}

# Frontmatter pattern: <!-- key: value --> at the top of the file
FRONTMATTER_RE = re.compile(
    r"^<!--\s*\n((?:\s*\w[\w-]*:\s*.+\n)+)\s*-->\s*\n",
    re.MULTILINE,
)
FIELD_RE = re.compile(r"^\s*([\w-]+):\s*(.+?)\s*$", re.MULTILINE)


@dataclass
class DocEntry:
    slug: str
    title: str
    description: str
    category: str
    order: int
    content: str  # raw markdown (without frontmatter)
    text: str = field(default="", repr=False)  # readable text of ``content``
    words: str = field(default="", repr=False)  # title, description and text as ``_words``

    @property
    def full_slug(self) -> str:
        return f"{self.category}/{self.slug}"


@dataclass
class DocSection:
    id: str
    title: str
    level: int


def _parse_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    """Extract HTML-comment frontmatter and return (metadata, content)."""
    match = FRONTMATTER_RE.match(raw)
    if not match:
        return {}, raw

    block = match.group(1)
    meta = {}
    for fm in FIELD_RE.finditer(block):
        meta[fm.group(1).strip()] = fm.group(2).strip()

    content = raw[match.end():]
    return meta, content


def _extract_headings(content: str) -> list[DocSection]:
    """Pull h2/h3 headings from markdown for table-of-contents."""
    sections: list[DocSection] = []
    for m in re.finditer(r"^(#{2,3})\s+(.+)$", content, re.MULTILINE):
        level = len(m.group(1))
        title = m.group(2).strip()
        # Generate slug from title
        slug = re.sub(r"[^\w\s-]", "", title.lower())
        slug = re.sub(r"[\s]+", "-", slug).strip("-")
        sections.append(DocSection(id=slug, title=title, level=level))
    return sections


# Readable text for search, as the Docs panel's ``markdownToPlainText`` reads
# it (#1554): no table pipes, link targets, anchors, emphasis marks or HTML.
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_LINK_DEF_RE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*\S")
_RULE_RE = re.compile(r"^\s{0,3}(?:([-*_])(?:\s*\1){2,}|=+)\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}(?:\s+(.*?))?(?:\s+#+)?\s*$")
_QUOTE_RE = re.compile(r"^\s*(?:>\s?)+")
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?")
_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")
_CODE_SPAN_RE = re.compile(r"(`+)(.+?)\1")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((?:[^()]|\([^)]*\))*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\](?:\((?:[^()]|\([^)]*\))*\)|\[[^\]]*\])")
_AUTOLINK_RE = re.compile(r"<((?:https?://|mailto:)[^>\s]+)>")
_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_EMPHASIS_RES = (
    re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1"),
    re.compile(r"(~~)(?=\S)(.+?)(?<=\S)\1"),
    re.compile(r"(?<![\w*])(\*)(?=\S)(.+?)(?<=\S)\1(?![\w*])"),
    re.compile(r"(?<![\w_])(_)(?=\S)(.+?)(?<=\S)\1(?![\w_])"),
)
_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")
_PLACEHOLDER_RE = re.compile("\x00(\\d+)\x00")


def _inline_text(line: str) -> str:
    """Readable text of one line: link text without its target, code without backticks, no HTML."""
    code: list[str] = []

    def keep(match: re.Match) -> str:
        code.append(match.group(2).strip())
        return f"\x00{len(code) - 1}\x00"

    line = _CODE_SPAN_RE.sub(keep, line)
    line = _IMAGE_RE.sub(r"\1", line)
    line = _LINK_RE.sub(r"\1", line)
    line = _AUTOLINK_RE.sub(r"\1", line)
    line = _BREAK_RE.sub(" ", line)
    line = _TAG_RE.sub("", line)
    for pattern in _EMPHASIS_RES:
        line = pattern.sub(r"\2", line)
    line = html.unescape(_ESCAPE_RE.sub(r"\1", line))
    return _PLACEHOLDER_RE.sub(lambda m: code[int(m.group(1))], line)


def _markdown_to_plain_text(markdown: str) -> str:
    """A document as the reader sees it, for search scores and snippets (#1554)."""
    lines = _COMMENT_RE.sub("", markdown.replace("\r\n", "\n")).split("\n")
    parts: list[str] = []
    fence = ""
    in_table = False
    for i, line in enumerate(lines):
        opener = _FENCE_RE.match(line)
        if fence:
            if opener and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence):
                fence = ""
            else:
                parts.append(line)
            continue
        if opener:
            fence = opener.group(1)
            continue
        if not line.strip() or "|" not in line:
            in_table = False
        if not line.strip() or _LINK_DEF_RE.match(line) or _RULE_RE.match(line):
            continue
        following = lines[i + 1] if i + 1 < len(lines) else ""
        if "|" in line and not in_table and "|" in following and _TABLE_SEP_RE.match(following):
            in_table = True  # this line is a table's header row
        if in_table:
            if _TABLE_SEP_RE.match(line):
                continue
            cells = _CELL_SPLIT_RE.split(line.strip().strip("|"))
            parts.append(" ".join(_inline_text(cell.strip()) for cell in cells))
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            line = heading.group(1) or ""
        else:
            line = _LIST_RE.sub("", _QUOTE_RE.sub("", line))
        parts.append(_inline_text(line))
    return " ".join(" ".join(part.split()) for part in parts if part.strip())


def _words(text: str) -> str:
    """Text as a run of lower-case words, so punctuation and markup between them don't matter."""
    return " ".join(re.sub(r"[\W]+", " ", text.lower()).split())


_SENTENCE_MARKS = ".,;:!?()[]{}\"'"


def _phrase_span(text: str, phrase: str) -> tuple[int, int] | None:
    """Where ``text`` holds ``phrase`` (already ``_words``) as words together, in
    order, with any punctuation or spacing between them, at the text's own
    offsets: the first whole match, else the first whose last word is still
    being typed."""
    parts = [re.escape(word) for word in phrase.split()]
    if len(parts) < 2:
        return None
    body = r"(?<!\w)" + r"\W+".join(parts)
    for pattern in (body + r"(?!\w)", body):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.start(), match.end()
    return None


def _snippet(text: str, at: int, length: int) -> str:
    start = max(0, at - 60)
    end = min(len(text), at + length + 60)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet


class DocsIndex:
    """In-memory documentation index."""

    def __init__(self) -> None:
        self.docs: dict[str, DocEntry] = {}  # keyed by "category/slug"
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._load_all()
            self._loaded = True

    def _load_all(self) -> None:
        if not DOCS_DIR.exists():
            return

        for md_file in sorted(DOCS_DIR.rglob("*.md")):
            rel = md_file.relative_to(DOCS_DIR)
            parts = rel.parts

            if len(parts) < 2:
                continue

            category = parts[0]
            if category not in CATEGORY_MAP:
                continue

            slug = md_file.stem
            if slug == "_index":
                slug = "_index"

            raw = md_file.read_text(encoding="utf-8")
            meta, content = _parse_frontmatter(raw)

            title = meta.get("title", slug.replace("-", " ").title())
            description = meta.get("description", "")
            # A malformed ``order`` sorts last instead of failing the whole
            # index (every documentation request would 500 until a restart).
            try:
                order = int(meta.get("order", 99))
            except ValueError:
                order = 99

            entry = DocEntry(
                slug=slug,
                title=title,
                description=description,
                category=category,
                order=order,
                content=content,
                text=_markdown_to_plain_text(content),
            )
            entry.words = _words(f"{title} {description} {entry.text}")
            self.docs[entry.full_slug] = entry

    def reload(self) -> None:
        """Force reload all docs from disk."""
        self.docs.clear()
        self._loaded = False
        self._ensure_loaded()

    def list_all(self) -> list[dict]:
        """Return all categories with their docs (metadata only)."""
        self._ensure_loaded()
        result = []
        for cat in CATEGORIES:
            cat_docs = [
                {
                    "slug": d.slug,
                    "title": d.title,
                    "description": d.description,
                    "category": d.category,
                }
                for d in sorted(self.docs.values(), key=lambda x: x.order)
                if d.category == cat["id"]
            ]
            result.append({
                "id": cat["id"],
                "label": cat["label"],
                "order": cat["order"],
                "docs": cat_docs,
            })
        return result

    def get_doc(self, category: str, slug: str) -> dict | None:
        """Get a single doc with full content."""
        self._ensure_loaded()
        key = f"{category}/{slug}"
        entry = self.docs.get(key)
        if not entry:
            return None

        return {
            "slug": entry.slug,
            "title": entry.title,
            "description": entry.description,
            "category": entry.category,
            "content_markdown": entry.content,
            "sections": [
                {"id": s.id, "title": s.title, "level": s.level}
                for s in _extract_headings(entry.content)
            ],
        }

    def get_category_docs(self, category: str) -> list[dict] | None:
        """List docs in a category with metadata."""
        self._ensure_loaded()
        if category not in CATEGORY_MAP:
            return None

        return [
            {
                "slug": d.slug,
                "title": d.title,
                "description": d.description,
                "category": d.category,
            }
            for d in sorted(self.docs.values(), key=lambda x: x.order)
            if d.category == category
        ]

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Simple full-text search across all docs, over their readable text."""
        self._ensure_loaded()
        if not query or not query.strip():
            return []

        query_lower = " ".join(query.lower().split())
        # Sentence punctuation around a word (`remote,` `(its`) isn't part of it.
        terms = [term.strip(_SENTENCE_MARKS) or term for term in query_lower.split()]
        # Several words together, as typed (the last may still be being typed).
        phrase = _words(query)
        if " " not in phrase:
            phrase = ""
        results: list[tuple[bool, float, DocEntry, list[str]]] = []

        for entry in self.docs.values():
            score = 0.0
            snippets: list[str] = []
            title_lower = entry.title.lower()
            text_lower = entry.text.lower()
            has_phrase = bool(phrase) and f" {phrase}" in f" {entry.words}"

            # The snippet shows the words together first, where ranking found them.
            span = _phrase_span(entry.text, phrase) if has_phrase else None
            if span:
                snippets.append(_snippet(entry.text, span[0], span[1] - span[0]))

            for term in terms:
                # Title matches score higher
                if term in title_lower:
                    score += 10.0
                # Description matches
                if term in entry.description.lower():
                    score += 5.0
                # Content matches
                count = text_lower.count(term)
                if count > 0:
                    score += min(count, 10)  # cap at 10 occurrences
                    snippet = _snippet(entry.text, text_lower.index(term), len(term))
                    if snippet not in snippets:
                        snippets.append(snippet)

            # A phrase match counts even when the raw terms, carrying the
            # query's punctuation (`remote, access!`), score nothing.
            if score > 0 or has_phrase:
                results.append((has_phrase, score, entry, snippets[:3]))

        # A document holding the words together ranks above any that only has
        # them apart (#1581); the scores order each group.
        results.sort(key=lambda x: (not x[0], -x[1]))
        return [
            {
                "slug": entry.slug,
                "title": entry.title,
                "category": entry.category,
                "description": entry.description,
                "score": score,
                "snippets": snippets,
            }
            for _, score, entry, snippets in results[:limit]
        ]


# Singleton instance
docs_index = DocsIndex()
