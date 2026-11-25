"""Unit tests for app.core.project_links — link-group derivation + auto-link."""

import os

import pytest
from unittest.mock import patch

from app.core import project_links
from app.core.project_links import (
    normalize_remote_url,
    auto_link_group_id,
    is_link_locked,
    maybe_auto_link_project,
    resolve_runnable_local_project,
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
# One identity for every spelling of a network remote
# ---------------------------------------------------------------------------

_DEMO = "https://github.com/example-org/demo"


@pytest.mark.parametrize("spelling", [
    "https://github.com/example-org/demo.git",
    "https://github.com/Example-Org/Demo/",
    "HTTPS://GitHub.com/example-org/demo",
    "http://github.com/example-org/demo.git",
    "http://github.com:80/example-org/demo",
    "https://someone@github.com/example-org/demo.git",
    "https://someone:pw-example@github.com/example-org/demo.git",
    "https://github.com:443/example-org/demo",
    "git@github.com:example-org/demo.git",
    "github.com:example-org/demo",
])
def test_every_forge_spelling_of_a_remote_has_one_identity(spelling):
    assert project_links.canonical_remote(spelling) == _DEMO
    assert auto_link_group_id(spelling) == auto_link_group_id(_DEMO)


def _same(a, b):
    return auto_link_group_id(a) == auto_link_group_id(b)


def test_an_ssh_path_relative_to_the_login_is_not_the_absolute_one():
    relative, absolute = "git@git.example.test:repos/app.git", "git@git.example.test:/repos/app.git"
    assert not _same(relative, absolute)
    assert _same(relative, "https://git.example.test/repos/app")
    # An absolute path is the same whichever SSH form names it, and not the web one.
    assert project_links.canonical_remote(absolute) == "ssh://git.example.test/repos/app"
    for spelling in ("ssh://git@git.example.test/repos/app.git", "ssh://git@git.example.test:22/repos/app",
                     "git+ssh://git@git.example.test/repos/app", "ssh+git://git.example.test/repos/app"):
        assert _same(spelling, absolute), spelling
    assert not _same("ssh://git@git.example.test/repos/app", "https://git.example.test/repos/app")
    assert _same("git@git.example.test:~/repos/app", "ssh://git@git.example.test/~/repos/app")
    assert not _same("git@git.example.test:~/repos/app", absolute)


def test_only_the_schemes_own_default_port_is_dropped():
    web = "https://git.example.test/org/app"
    assert not _same("https://git.example.test:80/org/app", web)
    assert not _same("http://git.example.test:443/org/app", web)
    assert _same("https://git.example.test:443/org/app", web)
    assert _same("http://git.example.test:80/org/app", web)
    assert not _same("ssh://git@git.example.test:2222/org/app", "ssh://git@git.example.test/org/app")
    assert not _same("ssh://git@git.example.test:2222/org/app", "git@git.example.test:org/app")
    assert _same("git://git.example.test:9418/org/app", "git://git.example.test/org/app")
    assert not _same("git://git.example.test/org/app", web)


def test_a_bracketed_ipv6_host_is_one_host():
    web = "https://[2001:db8::1]/org/app"
    assert project_links.canonical_remote("https://[2001:db8::1]/org/app.git") == web
    assert project_links.canonical_remote("git@[2001:db8::1]:org/app.git") == web
    assert project_links.canonical_remote("ssh://git@[2001:db8::1]/org/app.git") == "ssh://[2001:db8::1]/org/app"
    assert _same("ssh://git@[2001:db8::1]/org/app.git", "git@[2001:db8::1]:/org/app")
    assert project_links.canonical_remote("https://[2001:db8::1]:8443/org/app") == "https://[2001:db8::1]:8443/org/app"
    assert _same("https://user:pw-example@[2001:db8::1]/org/app", web)


@pytest.mark.parametrize("spelling", [
    "https://user:pw-example@git.example.test/org/app",
    "https://user:pw@example@git.example.test/org/app",
    "https://user:pw-example@[2001:db8::1]/org/app",
    "ssh://user:pw-example@git.example.test/org/app",
    "ssh://user:pw-example@[2001:db8::1]:2222/org/app",
    "https://user:pw-example@git.example.test/org/app?ref=main",
])
def test_no_login_reaches_the_identity(spelling):
    canonical = project_links.canonical_remote(spelling)
    assert "pw" not in canonical and "user" not in canonical and "@" not in canonical


def test_an_https_identity_is_the_one_it_always_had():
    # Plain HTTPS rows keep their origin (and projects_v2 id): only other
    # spellings move. sha256("https://github.com/example-org/demo")[:16].
    assert auto_link_group_id("https://github.com/Example-Org/demo.git") == "git:7f0224ffa6b6d980"
    assert project_links.legacy_link_group_id("https://github.com/example-org/demo") == "git:7f0224ffa6b6d980"


def test_spellings_that_are_other_repositories_stay_apart():
    assert auto_link_group_id("git@github.com:example-org/demo") != auto_link_group_id(
        "git@github.com:example-org/other")
    assert auto_link_group_id("https://git.example.com:8443/example-org/demo") != auto_link_group_id(
        "https://git.example.com/example-org/demo")
    assert auto_link_group_id("git@git.example.com:example-org/demo") != auto_link_group_id(
        "git@github.com:example-org/demo")


@pytest.mark.parametrize("path", ["/srv/repos/demo.git", "file:///srv/repos/demo.git",
                                  "C:/repos/demo.git", "C:\\repos\\demo.git", "../demo.git"])
def test_a_local_repository_keeps_its_old_identity(path):
    assert project_links.canonical_remote(path) == normalize_remote_url(path)
    assert auto_link_group_id(path) == project_links.legacy_link_group_id(path)


def test_a_remote_names_its_canonical_and_its_old_identity():
    ssh = "git@github.com:example-org/demo.git"
    ids = project_links.remote_link_ids(ssh)
    assert ids == {auto_link_group_id(_DEMO), project_links.legacy_link_group_id(ssh)}
    assert len(ids) == 2
    assert project_links.remote_link_ids(_DEMO) == {auto_link_group_id(_DEMO)}
    assert project_links.remote_link_ids("") == set()


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


# ---------------------------------------------------------------------------
# Cross-install project resolution
# ---------------------------------------------------------------------------

_ME = "install-me"
_OTHER = "install-other"
# A directory guaranteed to exist on disk for the path-exists / runnable checks.
_EXISTING_DIR = os.path.dirname(__file__)


def _proj(pid, instance_id=None, link_group_id=None, path="/does/not/exist"):
    return {
        "id": pid,
        "name": pid,
        "path": path,
        "settings": {},
        "instance_id": instance_id,
        "link_group_id": link_group_id,
    }


@pytest.fixture
def resolve_env():
    """Patch the DB facade + pin this install's id to _ME."""
    with patch("app.core.project_links.database") as db, \
         patch("app.core.project_links.settings") as st:
        st.effective_instance_id = _ME
        db.get_projects_in_link_group.return_value = []
        yield db


class TestResolveRunnableLocalProject:
    def test_unknown_id_returns_none_none(self, resolve_env):
        resolve_env.get_project.return_value = None
        assert resolve_runnable_local_project("missing") == (None, None)

    def test_own_row_with_existing_dir_is_runnable(self, resolve_env):
        mine = _proj("proj-A", instance_id=_ME, path=_EXISTING_DIR)
        resolve_env.get_project.return_value = mine
        stored, local = resolve_runnable_local_project("proj-A")
        assert stored == mine
        assert local == mine

    def test_own_row_with_missing_dir_is_not_runnable(self, resolve_env):
        """A matching instance-id alone is not enough: the runnable resolver
        additionally requires the checkout path to exist on disk."""
        mine = _proj("proj-A", instance_id=_ME, path="/definitely/not/here")
        resolve_env.get_project.return_value = mine
        stored, local = resolve_runnable_local_project("proj-A")
        assert stored == mine
        assert local is None

    def test_sibling_with_existing_dir_is_runnable(self, resolve_env):
        foreign = _proj("proj-A", instance_id=_OTHER, link_group_id="git:abc")
        sibling = _proj(
            "proj-B", instance_id=_ME, link_group_id="git:abc", path=_EXISTING_DIR
        )
        resolve_env.get_project.return_value = foreign
        resolve_env.get_projects_in_link_group.return_value = [foreign, sibling]
        stored, local = resolve_runnable_local_project("proj-A")
        assert stored == foreign
        assert local == sibling

    def test_windows_path_on_posix_is_not_runnable(self, resolve_env):
        """A Windows-style stored path on a POSIX host is not a dir here → not
        runnable even though it is this install's own row."""
        mine = _proj("proj-A", instance_id=_ME, path=r"C:\Users\dev\repo")
        resolve_env.get_project.return_value = mine
        stored, local = resolve_runnable_local_project("proj-A")
        assert stored == mine
        assert local is None
