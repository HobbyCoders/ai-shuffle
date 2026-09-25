"""
Documentation Content Loader

Reads markdown files from docs/content/, parses metadata headers,
and builds an in-memory index for the documentation API.
"""

import re
from pathlib import Path
from dataclasses import dataclass

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
            order = int(meta.get("order", 99))

            entry = DocEntry(
                slug=slug,
                title=title,
                description=description,
                category=category,
                order=order,
                content=content,
            )
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
        """Simple full-text search across all docs."""
        self._ensure_loaded()
        if not query or not query.strip():
            return []

        query_lower = query.lower().strip()
        terms = query_lower.split()
        results: list[tuple[float, DocEntry, list[str]]] = []

        for entry in self.docs.values():
            score = 0.0
            snippets: list[str] = []
            title_lower = entry.title.lower()
            content_lower = entry.content.lower()

            for term in terms:
                # Title matches score higher
                if term in title_lower:
                    score += 10.0
                # Description matches
                if term in entry.description.lower():
                    score += 5.0
                # Content matches
                count = content_lower.count(term)
                if count > 0:
                    score += min(count, 10)  # cap at 10 occurrences

                    # Extract snippet around first match
                    idx = content_lower.index(term)
                    start = max(0, idx - 60)
                    end = min(len(entry.content), idx + len(term) + 60)
                    snippet = entry.content[start:end].strip()
                    if start > 0:
                        snippet = "..." + snippet
                    if end < len(entry.content):
                        snippet = snippet + "..."
                    snippets.append(snippet)

            if score > 0:
                results.append((score, entry, snippets[:3]))

        results.sort(key=lambda x: -x[0])
        return [
            {
                "slug": entry.slug,
                "title": entry.title,
                "category": entry.category,
                "description": entry.description,
                "score": score,
                "snippets": snippets,
            }
            for score, entry, snippets in results[:limit]
        ]


# Singleton instance
docs_index = DocsIndex()
