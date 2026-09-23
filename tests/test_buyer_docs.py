"""The buyer documentation set (plan item P9) stays complete, indexed and in sync.

The in-app Docs panel renders the frontend's bundled copy of docs/content
(frontend/src/lib/docs/content); the backend documentation API reads
docs/content itself. Both copies of the buyer pages must stay identical.
"""
from pathlib import Path
import re

import pytest

from app.core.docs_content import DocsIndex

ROOT = Path(__file__).resolve().parents[1]
BUYER = ROOT / "docs/content/getting-started"
FRONTEND_BUYER = ROOT / "frontend/src/lib/docs/content/getting-started"

REQUIRED = {
    "_index", "overview", "requirements", "installation", "install-unraid", "install-docker-compose",
    "first-run", "modes-and-costs", "quick-start", "remote-access", "updates", "license",
    "troubleshooting", "faq", "changelog",
}
FRONTMATTER = re.compile(r"^<!--\n((?:[\w-]+: .+\n)+)-->\n")
MODEL_NAMES = re.compile(r"\b(opus|sonnet|haiku|fable|gpt-?\d|gemini|grok-\d|o\d-mini)\b", re.IGNORECASE)
DOC_LINK = re.compile(r"\(/docs/getting-started/([\w-]+)\)")


def pages() -> dict[str, str]:
    return {path.stem: path.read_text(encoding="utf-8") for path in sorted(BUYER.glob("*.md"))}


def meta(text: str) -> dict[str, str]:
    match = FRONTMATTER.match(text)
    assert match, "missing <!-- title/description/order --> frontmatter"
    return dict(line.split(": ", 1) for line in match.group(1).splitlines())


def test_every_buyer_page_exists_with_metadata():
    found = pages()
    assert REQUIRED <= set(found), f"missing buyer pages: {sorted(REQUIRED - set(found))}"
    orders = {}
    for slug, text in found.items():
        fields = meta(text)
        assert fields.get("title") and fields.get("description"), slug
        orders[slug] = int(fields["order"])
    assert orders["_index"] == 0
    assert len(set(orders.values())) == len(orders), f"duplicate order values: {orders}"


def test_frontend_bundles_the_same_buyer_pages():
    ours = {p.name: p.read_bytes() for p in BUYER.glob("*.md")}
    bundled = {p.name: p.read_bytes() for p in FRONTEND_BUYER.glob("*.md")}
    assert set(ours) == set(bundled), "docs/content and the frontend copy list different pages"
    for name in ours:
        assert ours[name] == bundled[name], f"{name} differs between docs/content and the frontend copy"


def test_index_links_every_page_and_every_link_resolves():
    found = pages()
    linked = set(DOC_LINK.findall(found["_index"]))
    assert set(found) - {"_index"} <= linked, f"not linked from Start Here: {sorted(set(found) - {'_index'} - linked)}"
    for slug, text in found.items():
        for target in DOC_LINK.findall(text):
            assert target in found, f"{slug} links to a missing page {target}"


def test_backend_docs_index_serves_the_buyer_pages_in_order():
    index = DocsIndex()
    listed = next(c for c in index.list_all() if c["id"] == "getting-started")["docs"]
    slugs = [d["slug"] for d in listed]
    assert slugs[0] == "_index"
    assert REQUIRED <= set(slugs)
    assert slugs.index("requirements") < slugs.index("install-unraid") < slugs.index("first-run")
    doc = index.get_doc("getting-started", "first-run")
    assert not doc["content_markdown"].startswith("<!--\ntitle")
    assert any(s["title"].endswith("Owner password") for s in doc["sections"])


def test_compose_page_embeds_the_buyer_install_files_verbatim():
    page = pages()["install-docker-compose"]
    for name in ("compose.yaml", "env.example"):
        source = (ROOT / "docs/install/compose" / name).read_text(encoding="utf-8").rstrip("\n")
        assert source in page, f"install-docker-compose.md no longer matches docs/install/compose/{name}"


@pytest.mark.parametrize("slug", sorted(REQUIRED))
def test_buyer_pages_name_no_models_and_no_vendor_features(slug):
    text = pages()[slug]
    assert not MODEL_NAMES.search(text), MODEL_NAMES.search(text).group(0)
    title = meta(text)["title"]
    assert "claude" not in title.lower() and "anthropic" not in title.lower()
    assert "Agent SDK" not in text


@pytest.mark.parametrize("slug", sorted(REQUIRED))
def test_prose_is_not_hard_wrapped(slug):
    # The in-app renderer uses `breaks: true`, so a wrapped paragraph shows as
    # broken lines. Paragraphs must be one line each.
    body = FRONTMATTER.sub("", pages()[slug])
    in_code = False
    previous = ""
    structural = re.compile(r"^(\s*[-*>|#]|\s*\d+\.|<!--|```)")
    for line in body.splitlines():
        if line.startswith("```"):
            in_code = not in_code
            previous = ""
            continue
        if in_code:
            continue
        if line.strip() and previous.strip() and not structural.match(line) and not structural.match(previous):
            pytest.fail(f"{slug}: hard-wrapped paragraph at {line!r}")
        previous = line


def test_readme_is_for_buyers():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/content/getting-started/_index.md" in readme
    assert "https://hobbycoders.com/ai-shuffle" in readme
    for build_step in ("git clone", "--build", "npm ", "pip install", "run_local", "pytest"):
        assert build_step not in readme, f"README still has a build-from-source step: {build_step}"
    assert not MODEL_NAMES.search(readme)
    assert "docs/internal" in readme


def test_contributor_material_lives_in_docs_internal_with_pointers():
    internal = ROOT / "docs/internal"
    for name in ("CONTRIBUTING.md", "LOCAL_SETUP.md", "DEVELOPMENT.md", "architecture.md",
                 "changelog-template.md", "README.md"):
        assert (internal / name).is_file(), name
    for pointer in ("CONTRIBUTING.md", "LOCAL_SETUP.md"):
        text = (ROOT / pointer).read_text(encoding="utf-8")
        assert f"docs/internal/{pointer}" in text
        assert len(text.splitlines()) < 15, f"{pointer} should be a short pointer"
    assert not (BUYER / "architecture.md").exists()
