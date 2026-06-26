"""Unit tests for app.core.project_links — link-group derivation + auto-link."""

import pytest

from app.core import project_links
from app.core.project_links import (
    normalize_remote_url,
    auto_link_group_id,
    is_link_locked,
    maybe_auto_link_project,
    LINK_LOCKED_KEY,
)


# ---------------------------------------------------------------------------
# normalize_remote_url
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, ""),
        ("", ""),
        ("  ", ""),
        ("https://github.com/Owner/Repo.git", "https://github.com/owner/repo"),
        ("https://github.com/owner/repo/", "https://github.com/owner/repo"),
        ("https://github.com/owner/repo", "https://github.com/owner/repo"),
        ("git@github.com:Owner/Repo.git", "git@github.com:owner/repo"),
    ],
)
def test_normalize_remote_url(raw, expected):
    assert normalize_remote_url(raw) == expected


def test_normalize_strips_single_git_suffix_only():
    # Only a single trailing ".git" is stripped.
    assert normalize_remote_url("https://x/y.git.git") == "https://x/y.git"


# ---------------------------------------------------------------------------
# auto_link_group_id
# ---------------------------------------------------------------------------

def test_auto_link_group_id_deterministic_and_prefixed():
    gid = auto_link_group_id("https://github.com/owner/repo.git")
    assert gid is not None
    assert gid.startswith("git:")
    # 16 hex chars after the prefix
    assert len(gid) == len("git:") + 16
    # Stable across calls — this is what makes it identical on every install.
    assert gid == auto_link_group_id("https://github.com/owner/repo.git")


def test_auto_link_group_id_normalizes_before_hashing():
    # .git / trailing slash / case all collapse to one id.
    a = auto_link_group_id("https://github.com/Owner/Repo.git")
    b = auto_link_group_id("https://github.com/owner/repo/")
    c = auto_link_group_id("https://github.com/owner/repo")
    assert a == b == c


def test_auto_link_group_id_distinct_for_distinct_remotes():
    assert auto_link_group_id("https://github.com/a/b") != auto_link_group_id(
        "https://github.com/a/c"
    )


def test_auto_link_group_id_none_without_remote():
    assert auto_link_group_id(None) is None
    assert auto_link_group_id("") is None
    assert auto_link_group_id("   ") is None


# ---------------------------------------------------------------------------
# is_link_locked
# ---------------------------------------------------------------------------

def test_is_link_locked_variants():
    assert is_link_locked(None) is False
    assert is_link_locked({}) is False
    assert is_link_locked({"settings": None}) is False
    assert is_link_locked({"settings": {}}) is False
    assert is_link_locked({"settings": {LINK_LOCKED_KEY: False}}) is False
    assert is_link_locked({"settings": {LINK_LOCKED_KEY: True}}) is True
    # Non-dict settings must not blow up.
    assert is_link_locked({"settings": "oops"}) is False


# ---------------------------------------------------------------------------
# maybe_auto_link_project
# ---------------------------------------------------------------------------

class _Recorder:
    def __init__(self, project):
        self.project = project
        self.set_calls = []

    def get_project(self, pid):
        return self.project

    def set_project_link_group(self, pid, gid):
        self.set_calls.append((pid, gid))
        if self.project is not None:
            self.project["link_group_id"] = gid


def _patch_db(monkeypatch, project):
    rec = _Recorder(project)
    monkeypatch.setattr(project_links.database, "get_project", rec.get_project)
    monkeypatch.setattr(project_links.database, "set_project_link_group", rec.set_project_link_group)
    return rec


def test_maybe_auto_link_no_remote_is_noop(monkeypatch):
    rec = _patch_db(monkeypatch, {"id": "p1", "link_group_id": None, "settings": {}})
    assert maybe_auto_link_project("p1", None) is None
    assert rec.set_calls == []


def test_maybe_auto_link_missing_project_is_noop(monkeypatch):
    rec = _patch_db(monkeypatch, None)
    assert maybe_auto_link_project("p1", "https://github.com/a/b") is None
    assert rec.set_calls == []


def test_maybe_auto_link_sets_group(monkeypatch):
    proj = {"id": "p1", "link_group_id": None, "settings": {}}
    rec = _patch_db(monkeypatch, proj)
    gid = maybe_auto_link_project("p1", "https://github.com/a/b")
    assert gid == auto_link_group_id("https://github.com/a/b")
    assert rec.set_calls == [("p1", gid)]


def test_maybe_auto_link_already_equal_is_noop(monkeypatch):
    gid = auto_link_group_id("https://github.com/a/b")
    proj = {"id": "p1", "link_group_id": gid, "settings": {}}
    rec = _patch_db(monkeypatch, proj)
    assert maybe_auto_link_project("p1", "https://github.com/a/b") == gid
    assert rec.set_calls == []


def test_maybe_auto_link_respects_lock(monkeypatch):
    proj = {
        "id": "p1",
        "link_group_id": "manual:deadbeef",
        "settings": {LINK_LOCKED_KEY: True},
    }
    rec = _patch_db(monkeypatch, proj)
    # Locked: keeps the manual group, never overwrites from the remote.
    assert maybe_auto_link_project("p1", "https://github.com/a/b") == "manual:deadbeef"
    assert rec.set_calls == []
