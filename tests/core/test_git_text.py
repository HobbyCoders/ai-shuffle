"""Commit / PR text sanitizing (audit P12).

A mid-sentence generation artifact once reached a merged commit body, where it
cannot be corrected without rewriting history. The sanitizer removes only what
cannot plausibly be prose; the must-NOT-touch cases below are the point of the
module, so they outnumber the removal cases on purpose.
"""

import logging

from app.core.git_text import (
    sanitize_commit_text,
    sanitize_pr_body,
    sanitize_pr_title,
)


# ─── things it must strip ────────────────────────────────────────────────────


def test_strips_a_lone_vim_write_line():
    text = "fix(api): tighten the retry window\n\nThe old bound was a guess.\n:wq"
    assert sanitize_commit_text(text) == (
        "fix(api): tighten the retry window\n\nThe old bound was a guess."
    )


def test_strips_every_bare_vim_variant():
    for artifact in (":w", ":q", ":q!", ":wq", ":wq!", ":x", ":xa", ":wqa"):
        text = f"subject line\n\nbody text\n{artifact}"
        assert sanitize_commit_text(text) == "subject line\n\nbody text", artifact


def test_strips_literal_no_output_lines():
    text = "chore: bump deps\n\n(no output)\nEverything still builds."
    assert sanitize_commit_text(text) == "chore: bump deps\n\nEverything still builds."


def test_no_output_match_is_case_insensitive_but_whole_line_only():
    assert sanitize_commit_text("subject\n\n(NO OUTPUT)\nbody") == "subject\n\nbody"


def test_strips_an_unbalanced_trailing_code_fence():
    text = "fix: handle the empty case\n\nThe guard was missing.\n```"
    assert sanitize_commit_text(text) == (
        "fix: handle the empty case\n\nThe guard was missing."
    )


def test_strips_trailing_whitespace_and_edge_blank_lines():
    text = "\n\nsubject line   \n\nbody line\t\n\n\n"
    assert sanitize_commit_text(text) == "subject line\n\nbody line"


def test_collapses_a_repeated_identical_line():
    text = "subject\n\nThe cache was never invalidated.\nThe cache was never invalidated."
    assert sanitize_commit_text(text) == "subject\n\nThe cache was never invalidated."


def test_strips_several_artifacts_in_one_pass():
    text = "feat: add the thing\n\nIt works now.\n(no output)\n:wq\n```"
    assert sanitize_commit_text(text) == "feat: add the thing\n\nIt works now."


# ─── things it must NOT touch ────────────────────────────────────────────────


def test_leaves_ordinary_prose_byte_for_byte():
    text = (
        "refactor(store): fold the replay cache into the loader\n"
        "\n"
        "Two callers built the same dict from the same rows, and they drifted:\n"
        "one skipped deleted nodes, the other did not. Folding them means the\n"
        "skip rule lives in one place.\n"
        "\n"
        "- loader owns the cache now\n"
        "- the second builder is gone"
    )
    assert sanitize_commit_text(text) == text


def test_leaves_the_word_output_alone():
    text = "fix: quiet the runner\n\nThe command produced no output, which the\nparser read as a failure. It is not (no output matters here)."
    assert sanitize_commit_text(text) == text


def test_leaves_prose_mentioning_a_vim_command():
    text = "docs: explain the editor flow\n\nSave and quit with :wq when you are done."
    assert sanitize_commit_text(text) == text


def test_leaves_a_vim_command_inside_a_code_block():
    text = "docs: add the editing recipe\n\n```sh\nvim config.toml\n:wq\n```\n\nThat is the whole flow."
    assert sanitize_commit_text(text) == text


def test_leaves_no_output_inside_a_code_block():
    text = "docs: record the transcript\n\n```\n$ ./run --quiet\n(no output)\n```"
    assert sanitize_commit_text(text) == text


def test_leaves_balanced_code_fences():
    text = "fix: correct the snippet\n\n```python\nprint('hi')\n```"
    assert sanitize_commit_text(text) == text


def test_leaves_repeated_identical_lines_inside_a_code_block():
    """Two identical closing braces are ordinary code, not an artifact."""
    text = "fix: close the scope\n\n```c\nif (a) {\n  if (b) {\n    go();\n  }\n  }\n}\n}\n```"
    assert sanitize_commit_text(text) == text


def test_leaves_repeated_identical_indented_lines():
    """A four-space markdown code block repeats lines legitimately."""
    text = "subject\n\n    }\n    }\n\ntail"
    assert sanitize_commit_text(text) == text


def test_leaves_a_mid_text_unbalanced_fence_alone():
    """Guessing which fence is the stray one risks eating real content."""
    text = "subject\n\n```\ncode\n\nand then some trailing prose."
    assert sanitize_commit_text(text) == text


def test_leaves_repeated_blank_lines_between_paragraphs():
    text = "subject\n\nfirst paragraph\n\nsecond paragraph"
    assert sanitize_commit_text(text) == text


def test_leaves_non_adjacent_repeats():
    """Only an immediate repeat is an artifact; a refrain is prose."""
    text = "subject\n\nIt was slow.\nWe fixed the index.\nIt was slow.\nSo we cached it."
    assert sanitize_commit_text(text) == text


def test_never_empties_text_that_had_content():
    """An empty commit message fails the commit — worse than the artifact."""
    assert sanitize_commit_text("(no output)") == "(no output)"
    assert sanitize_commit_text(":wq") == ":wq"


def test_handles_empty_and_blank_input():
    assert sanitize_commit_text("") == ""
    assert sanitize_commit_text(None) == ""
    assert sanitize_commit_text("   \n  ") == "   \n  "


def test_normalizes_crlf_without_otherwise_changing_the_text():
    assert sanitize_commit_text("subject\r\n\r\nbody") == "subject\n\nbody"


# ─── PR title / body wrappers ────────────────────────────────────────────────


def test_pr_title_flattens_to_one_line():
    assert sanitize_pr_title("feat: add the thing\nand more") == (
        "feat: add the thing and more"
    )


def test_pr_title_strips_artifacts_and_collapses_spacing():
    assert sanitize_pr_title("  feat:  add   the thing  \n:wq  ") == (
        "feat: add the thing"
    )


def test_pr_title_leaves_a_clean_title_alone():
    assert sanitize_pr_title("fix(db): retry the pool handshake") == (
        "fix(db): retry the pool handshake"
    )


def test_pr_title_never_empties_a_title_that_had_content():
    assert sanitize_pr_title(":wq") == ":wq"


def test_pr_body_keeps_multiline_structure():
    body = "## What\n\nFolds the two loaders.\n\n## Why\n\nThey drifted."
    assert sanitize_pr_body(body) == body


def test_pr_body_strips_a_trailing_unclosed_fence():
    assert sanitize_pr_body("## What\n\nDoes the thing.\n```") == (
        "## What\n\nDoes the thing."
    )


# ─── logging ─────────────────────────────────────────────────────────────────


def test_logs_at_debug_when_something_was_stripped(caplog):
    with caplog.at_level(logging.DEBUG, logger="app.core.git_text"):
        sanitize_commit_text("subject\n\nbody\n:wq", context="commit message")
    messages = [rec.getMessage() for rec in caplog.records]
    assert any("sanitized commit message" in m for m in messages)
    assert any("vim artifact" in m for m in messages)


def test_does_not_log_when_nothing_changed(caplog):
    with caplog.at_level(logging.DEBUG, logger="app.core.git_text"):
        sanitize_commit_text("subject\n\nbody")
    assert not [r for r in caplog.records if "sanitized" in r.getMessage()]
