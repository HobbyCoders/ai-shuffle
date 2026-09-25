"""Edge cases for the ``.git`` alias check and the plain-branch-name check.

``_is_dotgit_alias`` mirrors git's ``is_ntfs_dotgit`` / ``is_hfs_dotgit``
(read.c / utf8.c): NTFS folds ASCII case, ignores trailing dots and spaces
and an ``:stream`` suffix, and knows only ``GIT~1`` as the 8.3 name of
``.git``; HFS+ drops a fixed set of ignorable code points. Nothing broader:
a name git accepts must stay usable.

``_is_plain_branch_name`` mirrors ``git check-ref-format --branch`` plus a
refusal of a leading ``+`` (forced refspec).
"""

from __future__ import annotations

import unicodedata

import pytest

from app.core.git_commit_ops import _is_dotgit_alias, _is_plain_branch_name, _push_refspec


# ── .git aliases ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        ".git", ".GIT", ".Git", ".gIt",
        ".git.", ".git ", ".git. . ", ".git...",
        ".git::$INDEX_ALLOCATION", ".git:stream", ".GIT . :x",
        "git~1", "GIT~1", "Git~1.", "GIT~1 ",
        ".g\u200cit", ".\u200dgit", ".git\u200e", "\u200f.git", ".gi\u202at",
        ".g\u206fit", "\ufeff.git", ".G\u200cI\u200dT",
        unicodedata.normalize("NFD", ".git"),
    ],
)
def test_names_git_reads_as_dotgit(name):
    assert _is_dotgit_alias(name)


@pytest.mark.parametrize(
    "name",
    [
        "git", ".gitignore", ".gitmodules", ".gitattributes", ".git2", ".git-x",
        "x.git", " .git", "..git", ".git~1", "git~", "git~0",
        # Only GIT~1 is .git's 8.3 name in git's check; GIT~2 names some
        # other long name and stays usable.
        "GIT~2", "git~10", "git~1x",
        # Not an ignorable code point in git's HFS+ list.
        ".g\u200bit",   # ZERO WIDTH SPACE
        ".g\u00adit",   # SOFT HYPHEN
        ".g\u2060it",   # WORD JOINER
        # Look-alikes that neither filesystem folds to ASCII.
        "\uff0e\uff47\uff49\uff54",  # full-width .git
        ".g\u0131t",    # dotless i
        ".git\u0301",  # t + combining acute
        "",
    ],
)
def test_names_that_are_not_dotgit(name):
    assert not _is_dotgit_alias(name)


def test_hfs_ignorable_set_matches_git_exactly():
    ignorable = [0x200C, 0x200D, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x206A, 0x2070), 0xFEFF]
    for cp in ignorable:
        assert _is_dotgit_alias(".gi" + chr(cp) + "t"), hex(cp)
    for cp in (0x200B, 0x2029, 0x202F, 0x2069, 0x2070, 0xFEFE, 0xFFFE):
        assert not _is_dotgit_alias(".gi" + chr(cp) + "t"), hex(cp)


# ── plain branch names ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "main", "feature/x", "a/b/c", "a+b", "x.y", "a./b", "v1.0.0", "user@host",
        "@x", "a@b", "caf\u00e9", "\u65e5\u672c", "LOCK", "x.LOCK", "x.lockx", "x.lock.y",
        "a-", "a_b", "HEAD-x", "x" * 1000,
    ],
)
def test_plain_branch_names(name):
    assert _is_plain_branch_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "", "@", "-x", "--mirror", "+main", "+x:main", ":main", "HEAD:refs/tags/v1",
        "a:b", "a..b", "..", "a/../b", "x@{1}", "@{-1}", "a@{u}",
        "a b", "a\tb", "a\nb", "a\x00b", "a\x1fb", "a\x7fb",
        "a~1", "a^", "a?", "a*", "a[b", "a\\b",
        "x.lock", "a/b.lock", "a.lock/b", ".hidden", "a/.b", "/a", "a/", "a//b", "a.",
        "a/.", ".",
    ],
)
def test_not_plain_branch_names(name):
    assert not _is_plain_branch_name(name)


def test_push_refspec_is_fully_qualified_on_both_sides():
    assert _push_refspec("feature/x") == "refs/heads/feature/x:refs/heads/feature/x"
