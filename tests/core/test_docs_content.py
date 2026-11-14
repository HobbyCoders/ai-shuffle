"""DocsIndex over a throwaway docs tree: frontmatter, ordering, headings, search."""

import pytest

from app.core import docs_content as dc
from app.core.docs_content import DocsIndex


def _doc(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def docs(tmp_path, monkeypatch):
    root = tmp_path / "content"
    monkeypatch.setattr(dc, "DOCS_DIR", root)
    _doc(root, "features/voice-mode.md",
         "<!--\ntitle: Voice Mode\ndescription: Talk to agents\norder: 2\n-->\n"
         "Intro about voice.\n\n## Getting Set Up!\ntext\n### Mic (USB) options\n#### too deep\n")
    _doc(root, "features/alpha.md", "<!--\norder: 1\n-->\n# Alpha\nThe alpha feature.\n")
    # A non-numeric order must not take the whole index down.
    _doc(root, "features/bad-order.md", "<!--\ntitle: Bad Order\norder: first\n-->\nbody\n")
    _doc(root, "connectors/slack.md", "No frontmatter here. Slack slack SLACK.\n")
    _doc(root, "connectors/_index.md", "Index page\n")
    _doc(root, "unknown-category/x.md", "ignored\n")
    _doc(root, "top-level.md", "ignored\n")
    return DocsIndex()


def test_missing_docs_dir_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(dc, "DOCS_DIR", tmp_path / "nope")
    idx = DocsIndex()
    assert all(c["docs"] == [] for c in idx.list_all())
    assert idx.search("anything") == []


def test_list_all_orders_and_filters(docs):
    cats = {c["id"]: c for c in docs.list_all()}
    assert list(cats) == ["getting-started", "connectors", "features", "api-reference"]
    assert [d["slug"] for d in cats["features"]["docs"]] == ["alpha", "voice-mode", "bad-order"]
    assert {d["slug"] for d in cats["connectors"]["docs"]} == {"slack", "_index"}
    assert cats["features"]["docs"][1]["title"] == "Voice Mode"
    # Title defaults to the humanized slug.
    assert cats["features"]["docs"][0]["title"] == "Alpha"


def test_malformed_order_sorts_last(docs):
    docs.reload()
    assert docs.docs["features/bad-order"].order == 99
    assert docs.docs["features/bad-order"].title == "Bad Order"


def test_get_doc_and_headings(docs):
    doc = docs.get_doc("features", "voice-mode")
    assert doc["description"] == "Talk to agents"
    assert doc["content_markdown"].startswith("Intro about voice.")
    assert doc["sections"] == [
        {"id": "getting-set-up", "title": "Getting Set Up!", "level": 2},
        {"id": "mic-usb-options", "title": "Mic (USB) options", "level": 3},
    ]
    assert docs.get_doc("features", "missing") is None
    assert docs.get_doc("nope", "voice-mode") is None


def test_get_category_docs(docs):
    assert docs.get_category_docs("nope") is None
    assert docs.get_category_docs("api-reference") == []
    # Equal orders keep the (sorted) file order.
    assert [d["slug"] for d in docs.get_category_docs("connectors")] == ["_index", "slack"]


def test_search_scores_and_snippets(docs):
    assert docs.search("") == []
    assert docs.search("   ") == []
    results = docs.search("slack")
    assert results[0]["slug"] == "slack"
    # Humanized-slug title hit (10) + three case-insensitive content hits.
    assert results[0]["score"] == 13.0
    assert results[0]["snippets"] == ["No frontmatter here. Slack slack SLACK."]

    voice = docs.search("Voice agents")
    assert voice[0]["slug"] == "voice-mode"
    # "voice": title 10 + content 1; "agents": description 5.
    assert voice[0]["score"] == 16.0
    assert docs.search("zzz-no-match") == []


def test_search_snippet_ellipses_and_limit(tmp_path, monkeypatch):
    root = tmp_path / "content"
    monkeypatch.setattr(dc, "DOCS_DIR", root)
    filler = "x" * 100
    _doc(root, "features/long.md", f"{filler} needle {filler}")
    for i in range(3):
        _doc(root, f"features/n{i}.md", "needle")
    idx = DocsIndex()
    long_hit = [r for r in idx.search("needle") if r["slug"] == "long"][0]
    snippet = long_hit["snippets"][0]
    assert snippet.startswith("...") and snippet.endswith("...") and "needle" in snippet
    assert len(idx.search("needle", limit=2)) == 2


def test_reload_picks_up_changes(docs):
    assert docs.get_doc("features", "new") is None
    _doc(dc.DOCS_DIR, "features/new.md", "fresh")
    # Cached until reload.
    assert docs.get_doc("features", "new") is None
    docs.reload()
    assert docs.get_doc("features", "new")["content_markdown"] == "fresh"


def test_frontmatter_parser_edges():
    assert dc._parse_frontmatter("plain") == ({}, "plain")
    meta, body = dc._parse_frontmatter("<!--\n  my-key:  spaced value  \n-->\nrest")
    assert meta == {"my-key": "spaced value"} and body == "rest"
    # Not at the very top -> not frontmatter.
    raw = "\n<!--\ntitle: x\n-->\nrest"
    assert dc._parse_frontmatter(raw) == ({}, raw)


def test_search_snippets_are_readable_text_not_markdown(tmp_path, monkeypatch):
    """Spotlight shows the first snippet as the row description (#1554)."""
    root = tmp_path / "content"
    monkeypatch.setattr(dc, "DOCS_DIR", root)
    _doc(root, "getting-started/cloud.md", "\n".join([
        "<!--\ntitle: AI Shuffle Cloud\n-->",
        "## Paid plans", "",
        "| Plan | Workers |", "| --- | :---: |", "| **Ultimate** | 30 |", "",
        "See [the plans](/docs/getting-started/cloud#paid-plans), `code` and <span>this</span>.",
        "- [x] ~~old~~ _new_ ![logo](/img/logo.png)", "",
        "> quoted", "", "```sh", "echo hi", "```", "",
        "[ref]: https://example.com/target",
    ]))
    index = DocsIndex()
    [hit] = index.search("Ultimate")
    assert index.docs["getting-started/cloud"].text == (
        "Paid plans Plan Workers Ultimate 30 See the plans, code and this. "
        "old new logo quoted echo hi"
    )
    assert hit["snippets"] == [
        "Paid plans Plan Workers Ultimate 30 See the plans, code and this. old new logo quoted echo h..."
    ]
    # Link targets are not readable text, so they don't match either.
    assert DocsIndex().search("example") == []
    assert DocsIndex().search("docs/getting-started") == []


def test_plain_text_keeps_words_that_only_look_like_markup():
    assert dc._markdown_to_plain_text("set snake_case_name to 2*3*4 then # not a heading") == (
        "set snake_case_name to 2*3*4 then # not a heading"
    )
    assert dc._markdown_to_plain_text("Line one\nline two\n\n***\n\n# Title #\nbody") == (
        "Line one line two Title body"
    )


def test_search_ranks_the_exact_phrase_first(tmp_path, monkeypatch):
    """A pasted sentence finds the page it came from above loose word matches (#1581)."""
    root = tmp_path / "content"
    monkeypatch.setattr(dc, "DOCS_DIR", root)
    _doc(root, "features/loose.md",
         "<!--\ntitle: Prompt tools\n-->\nWrite a prompt. Choose tools. Its tools write prompts. "
         "Instructions for tools and prompts, its tools, its prompt.\n")
    _doc(root, "getting-started/quick-start.md",
         "<!--\ntitle: Quick Start\n-->\n"
         + "Intro words. " * 10
         + "3. Give it a **name**, write its **prompt** (its instructions), and choose its **tools**.\n")
    query = "write its prompt (its instructions), and choose its tools"
    results = DocsIndex().search(query)
    assert [r["slug"] for r in results] == ["quick-start", "loose"]
    # The snippet shows the phrase, not the first place one of its words occurs.
    assert query in results[0]["snippets"][0].lower()
    # A phrase whose last word is still being typed counts too.
    assert DocsIndex().search("choose its too")[0]["slug"] == "quick-start"


def test_search_keeps_a_phrase_match_whose_punctuation_differs(tmp_path, monkeypatch):
    """`remote, access!` finds the page saying "remote access", though its raw terms don't occur there."""
    root = tmp_path / "content"
    monkeypatch.setattr(dc, "DOCS_DIR", root)
    _doc(root, "features/loose.md", "<!--\ntitle: Hosts\n-->\nRemote, hosts. Access! permissions.\n")
    _doc(root, "features/exact.md", "<!--\ntitle: Other\n-->\nTurn on remote access in Settings.\n")
    for query in ("remote, access!", "remote-access"):
        assert [r["slug"] for r in DocsIndex().search(query)][:1] == ["exact"], query


def test_search_snippet_shows_the_phrase_not_an_earlier_lone_word(tmp_path, monkeypatch):
    root = tmp_path / "content"
    monkeypatch.setattr(dc, "DOCS_DIR", root)
    _doc(root, "features/page.md",
         "<!--\ntitle: Page\n-->\nRemote hosts are supported. "
         + "Padding unrelated context. " * 12 + "Enable remote-access here.\n")
    [page] = DocsIndex().search("remote access")
    assert "Enable remote-access here" in page["snippets"][0]
    assert "Remote hosts" not in page["snippets"][0]
    # Offsets are the text's own, though punctuation sits between the words.
    text = "A remote accessory. Then remote,  ACCESS it."
    assert dc._phrase_span(text, "remote access") == (text.index("remote,"), text.index(" it."))
    assert dc._phrase_span("remote accessory", "remote access") == (0, len("remote access"))
    assert dc._phrase_span(text, "remote") is None


def test_real_docs_find_a_pasted_sentence_first():
    index = DocsIndex()
    [first, *_] = index.search("write its prompt (its instructions), and choose its tools")
    assert first["slug"] == "quick-start"
    # Short words together find the FAQ first, as the Docs panel does.
    assert index.search("do I")[0]["slug"] == "faq"
    [cloud] = [r for r in index.search("Ultimate") if r["slug"] == "cloud"]
    assert not any(ch in cloud["snippets"][0] for ch in "|*`") and "](" not in cloud["snippets"][0]
