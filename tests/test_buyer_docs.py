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


def test_frontend_bundles_the_same_docs_everywhere():
    """Every docs section, not only the buyer pages: connector guides drifted once."""
    content, frontend = ROOT / "docs/content", ROOT / "frontend/src/lib/docs/content"
    ours = {p.relative_to(content): p.read_bytes() for p in content.rglob("*.md")}
    bundled = {p.relative_to(frontend): p.read_bytes() for p in frontend.rglob("*.md")}
    assert set(ours) == set(bundled), f"pages only on one side: {sorted(map(str, set(ours) ^ set(bundled)))}"
    drifted = sorted(str(name) for name in ours if ours[name] != bundled[name])
    assert not drifted, f"differ between docs/content and the frontend copy: {drifted}"


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


CONTENT = ROOT / "docs/content"
MD_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def heading_ids(text: str) -> set[str]:
    """Heading ids exactly as the in-app renderer (DocsContent.svelte) builds them."""
    ids, in_code = set(), False
    for line in text.splitlines():
        if line.startswith("```"):
            in_code = not in_code
            continue
        match = None if in_code else re.match(r"^#{1,6}\s+(.*?)\s*#*$", line)
        if match:
            slug = re.sub(r"<[^>]*>", "", match.group(1).lower())
            slug = re.sub(r"[^\w\s-]", "", slug)
            ids.add(re.sub(r"-+", "-", re.sub(r"\s+", "-", slug)).strip())
    return ids


def test_every_in_app_link_opens_a_page_in_the_docs_panel():
    """The Docs panel only resolves `/docs/<category>[/<slug>][#anchor]` and `#anchor` links.

    Anything else relative (`./x.md`, `/api/...`) would leave the app and 404.
    """
    problems = []
    for page in sorted(CONTENT.rglob("*.md")):
        text, in_code = page.read_text(encoding="utf-8"), False
        for number, line in enumerate(text.splitlines(), 1):
            if line.startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                continue
            for url in MD_LINK.findall(line):
                where = f"{page.relative_to(CONTENT)}:{number} {url}"
                if re.match(r"^(https?:|mailto:)", url):
                    continue
                path, _, anchor = url.partition("#")
                if not path:
                    target = page
                elif match := re.fullmatch(r"/docs/([\w-]+)(?:/([\w-]+))?/?", path):
                    target = CONTENT / match.group(1) / f"{match.group(2) or '_index'}.md"
                else:
                    problems.append(f"not a /docs/ link: {where}")
                    continue
                if not target.is_file():
                    problems.append(f"missing page: {where}")
                elif anchor and anchor not in heading_ids(target.read_text(encoding="utf-8")):
                    problems.append(f"missing heading: {where}")
    assert not problems, "\n".join(problems)


# DOMPurify (SANITIZE_DOM, on by default) drops an `id` whose value is already a
# property of `document` or of a <form>, so a heading with such an id loses it
# and its table-of-contents entry scrolls nowhere ("## Remove" -> id "remove").
# Lowercase single-word names only (heading ids are lowercase slugs); the
# `on<event>` handler names are left out. Probed against the Chromium the app
# ships (152); another engine may strip a few more.
DOM_CLOBBER_IDS = frozenset("""
action after all anchors animate append applets attributes autocapitalize autocomplete autofocus
before blur body charset children clear click close closest constructor contains cookie dataset
dir doctype domain draggable elements embeds encoding enctype evaluate focus fonts forms
fullscreen head hidden id images implementation inert lang length links location matches method
name nonce normalize open part plugins popover prefix prepend prerendering pseudo referrer rel
remove reset role scripts scroll slot spellcheck style submit target timeline title translate
when write writeln
""".split())


def test_section_heading_ids_survive_the_sanitizer():
    problems = []
    for page in sorted(CONTENT.rglob("*.md")):
        in_code = False
        for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1):
            if line.startswith("```"):
                in_code = not in_code
                continue
            if in_code or not re.match(r"^#{2,6}\s", line):
                continue
            (slug,) = heading_ids(line)
            if slug in DOM_CLOBBER_IDS:
                problems.append(f"{page.relative_to(CONTENT)}:{number} {line!r} -> id {slug!r}")
    assert not problems, "headings whose id the in-app sanitizer strips (rename them):\n" + "\n".join(problems)
