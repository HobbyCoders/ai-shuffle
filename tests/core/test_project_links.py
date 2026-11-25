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
# One identity for the HTTPS and git@ spellings of a remote
# ---------------------------------------------------------------------------

_DEMO = "https://github.com/example-org/demo"


@pytest.mark.parametrize("spelling", [
    "https://github.com/example-org/demo.git",
    "https://github.com/Example-Org/Demo/",
    "HTTPS://GitHub.com/example-org/demo",
    "git@github.com:example-org/demo.git",
    "git@github.com:Example-Org/demo",
    " git@github.com:example-org/demo/ ",
])
def test_the_https_and_git_at_spellings_have_one_identity(spelling):
    assert project_links.canonical_remote(spelling) == _DEMO
    assert auto_link_group_id(spelling) == auto_link_group_id(_DEMO)


@pytest.mark.parametrize("spelling", [
    # Only scp ``git@host:path`` with a relative path is rewritten; everything
    # else hashes exactly as it always did (``normalize_remote_url``).
    "http://github.com/example-org/demo.git",
    "https://github.com:443/example-org/demo",
    "https://someone:pw-example@github.com/example-org/demo.git",
    "ssh://git@github.com/example-org/demo.git",
    "ssh://git@github.com:22/example-org/demo",
    "git+ssh://git@github.com/example-org/demo",
    "git://github.com/example-org/demo",
    "GIT@github.com:example-org/demo",
    "alice@code.example.test:repos/app.git",
    "bob@code.example.test:repos/app.git",
    "code.example.test:repos/app.git",
    "git@code.example.test:/srv/repos/app.git",
    "git@code.example.test:~/repos/app.git",
    "git@[2001:db8::1]:org/app.git",
    # A self-hosted server's git@ keeps its own identity: it may serve HTTPS
    # under a prefix, so ``https://host/team/demo`` may be another repository.
    "git@forge.example:code/team/demo.git",
    "git@git.example.com:example-org/demo.git",
    "git@GitHub.com:example-org/demo.git",  # a public forge's host exactly
    "git@github.com:example-org/sub/demo.git",  # nesting is GitLab's only
    "git@github.com:example-org/%64emo.git",
    "https://[2001:db8::1]/org/app.git",
    "/srv/repos/demo.git",
    "file:///srv/repos/demo.git",
    "C:/repos/demo.git",
    "C:\\repos\\demo.git",
    "../demo.git",
])
def test_every_other_spelling_keeps_the_identity_it_always_had(spelling):
    assert project_links.canonical_remote(spelling) == normalize_remote_url(spelling)
    assert auto_link_group_id(spelling) == project_links.legacy_link_group_id(spelling)
    assert project_links.remote_link_ids(spelling) == {project_links.legacy_link_group_id(spelling)}


def test_two_ssh_logins_on_one_host_are_two_repositories():
    alice, bob = "alice@code.example.test:repos/app.git", "bob@code.example.test:repos/app.git"
    assert auto_link_group_id(alice) != auto_link_group_id(bob)
    assert project_links.remote_key(alice) != project_links.remote_key(bob)
    assert not project_links.same_remote(alice, bob)
    assert not project_links.same_remote("git@code.example.test:repos/app.git", "https://code.example.test/repos/app")
    assert project_links.same_remote("git@github.com:example-org/demo.git", "https://github.com/example-org/demo")


def test_a_self_hosted_servers_two_transports_are_two_identities():
    # A GitLab under /code serves git@forge.example:team/demo at
    # https://forge.example/code/team/demo, so these two may be two repositories.
    https, ssh = "https://forge.example/code/team/demo", "git@forge.example:code/team/demo"
    assert auto_link_group_id(https) != auto_link_group_id(ssh)
    assert project_links.remote_link_ids(https).isdisjoint(project_links.remote_link_ids(ssh))
    assert project_links.remote_key(https) != project_links.remote_key(ssh)
    assert not project_links.other_transport_of(https, ssh)
    assert project_links.joined_counterpart(https) is None and project_links.joined_counterpart(ssh) is None
    assert project_links.joined_pair(https) is None and project_links.joined_pair(ssh) is None
    assert project_links.joined_pair("git@gitlab.com:team/sub/demo.git") == ("ssh", "gitlab.com", "team/sub/demo.git")


def test_a_remote_key_never_holds_a_credential():
    key = project_links.remote_key("https://someone:pw-example@github.com/example-org/demo.git")
    assert key == _DEMO
    assert project_links.remote_key("ssh://alice:pw-example@code.example.test/repos/app") == \
        "ssh://code.example.test/repos/app"
    # An SSH login is not a credential: it stays and keeps two logins apart.
    assert project_links.remote_key("ssh://alice@code.example.test/repos/app") == \
        "ssh://alice@code.example.test/repos/app"
    assert project_links.remote_key("git@github.com:example-org/demo.git") == _DEMO
    assert project_links.remote_key(None) == ""


@pytest.mark.parametrize("url, key", [
    # Git reads a password with a literal ``[`` that a URL parser rejects.
    ("https://user:pa[ss-example@git.example.com/o/r.git", "https://git.example.com/o/r"),
    ("https://user:pa[ss@example@git.example.com/o/r", "https://git.example.com/o/r"),
    ("ssh://alice:pa[ss-example@code.example.test/repos/app", "ssh://code.example.test/repos/app"),
    ("https://user:pw-example@[2001:db8::1]:8443/o/r", "https://[2001:db8::1]:8443/o/r"),
    # No safe key: no marker in the dialog (the server's own check still refuses).
    ("https://user:pa/ss-example@git.example.com/o/r", ""),
    ("https://user:12/ss-example@git.example.com/o/r", ""),
    ("user:pa[ss-example@git.example.com:o/r", ""),
])
def test_a_remote_key_drops_any_password_or_has_no_key(url, key):
    assert project_links.remote_key(url) == key
    assert "pa[ss" not in project_links.remote_key(url) and "ss-example" not in project_links.remote_key(url)


def test_an_https_identity_is_the_one_it_always_had():
    # Plain HTTPS rows keep their origin (and projects_v2 id): only other
    # spellings move. sha256("https://github.com/example-org/demo")[:16].
    assert auto_link_group_id("https://github.com/Example-Org/demo.git") == "git:7f0224ffa6b6d980"
    assert project_links.legacy_link_group_id("https://github.com/example-org/demo") == "git:7f0224ffa6b6d980"


def test_spellings_that_are_other_repositories_stay_apart():
    assert auto_link_group_id("git@github.com:example-org/demo") != auto_link_group_id(
        "git@github.com:example-org/other")
    assert auto_link_group_id("git@git.example.com:example-org/demo") != auto_link_group_id(
        "git@github.com:example-org/demo")


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


@pytest.mark.parametrize("url, other", [
    ("git@github.com:example-org/demo.git", "https://github.com/example-org/demo.git"),
    ("https://github.com/example-org/demo", "git@github.com:example-org/demo"),
    ("https://github.com/Example-Org/demo", "git@github.com:Example-Org/demo"),
    ("git@codeberg.org:example-org/demo", "https://codeberg.org/example-org/demo"),
    ("https://bitbucket.org/example-org/demo.git", "git@bitbucket.org:example-org/demo.git"),
    ("git@gitlab.com:example-org/sub-group/demo.git", "https://gitlab.com/example-org/sub-group/demo.git"),
    # Only a public forge: a self-hosted one may serve HTTPS under a prefix.
    ("https://forge.example/code/team/demo.git", None),
    ("git@git.example.com:example-org/demo.git", None),
    ("https://GitHub.com/example-org/demo", None),  # the host exactly
    ("https://github.com/example-org/sub/demo", None),  # nesting is GitLab's only
    ("https://github.com/demo", None),
    ("https://github.com/example-org/%64emo.git", None),  # HTTP decodes it, scp doesn't
    ("https://github.com/example-org//demo", None),
    ("https://github.com/example-org/../demo", None),
    ("https://github.com:8443/example-org/demo", None),  # a port: not a joined form
    ("https://user@github.com/example-org/demo", None),  # userinfo: strip it first
    ("git@github.com:/srv/demo.git", None),
    ("git@github.com:~/demo.git", None),
    ("ssh://git@github.com/example-org/demo.git", None),
    ("deploy@github.com:example-org/demo.git", None),
    ("http://github.com/example-org/demo", None),
    (None, None),
])
def test_only_a_joined_remote_has_another_spelling(url, other):
    assert project_links.joined_counterpart(url) == other


@pytest.mark.parametrize("first, url, same", [
    ("git@github.com:example-org/demo", "https://github.com/example-org/demo", True),
    ("https://github.com/example-org/demo.git", "git@github.com:example-org/demo", True),
    ("https://github.com/example-org/demo/", "git@github.com:example-org/demo.git", True),
    ("https://tok@github.com/team/demo.git", "git@github.com:team/demo.git", True),
    ("git@gitlab.com:team/sub/demo.git", "https://gitlab.com/team/sub/demo", True),
    ("https://github.com/team/demo.git", "git@github.com:team/Demo.git", False),  # path: exactly
    ("https://github.com/team/demo.git", "https://github.com/team/Demo.git", False),  # same transport
    ("https://github.com/team/demo.git", "https://github.com/team/demo", False),
    ("https://github.com/team/demo.git", "git@gitlab.com:team/demo.git", False),
    ("https://github.com/team/demo.git", "deploy@github.com:team/demo.git", False),
    ("https://github.com:8443/team/demo.git", "git@github.com:team/demo.git", False),
    ("https://github.com/team/demo.git", "ssh://git@github.com/team/demo.git", False),
    ("https://GitHub.com/team/demo.git", "git@github.com:team/demo.git", False),  # a forge's host exactly
    # Only on a public forge: a self-hosted one may serve HTTPS under a prefix.
    ("https://git.example.com/team/demo.git", "git@git.example.com:team/demo.git", False),
    ("https://forge.example/code/team/demo", "git@forge.example:code/team/demo", False),
    ("https://forge.example/code/team/demo", "git@forge.example:team/demo", False),
    (None, "git@github.com:team/demo.git", False),
])
def test_only_the_same_url_over_the_other_transport_is_tried_next(first, url, same):
    assert project_links.other_transport_of(first, url) is same
    assert project_links.other_transport_of(url, first) is same


@pytest.mark.parametrize("url, safe", [
    ("https://user:pa'ss@git.example.com/example-org/demo.git", "https://git.example.com/example-org/demo.git"),
    ("https://user:pa[ss@git.example.com/example-org/demo.git", "https://git.example.com/example-org/demo.git"),
    ("https://tok@en@git.example.com/example-org/demo.git", "https://git.example.com/example-org/demo.git"),
    ("ssh://git@git.example.com/example-org/demo.git", "ssh://git@git.example.com/example-org/demo.git"),
    ("ssh://git:secret@git.example.com/example-org/demo.git", "ssh://git.example.com/example-org/demo.git"),
    ("git@git.example.com:example-org/demo.git", "git@git.example.com:example-org/demo.git"),
    ("user:secret@git.example.com:example-org/demo.git", None),
    ("https://git.example.com/example-org/demo@main", None),
    ("", None),
])
def test_a_remote_is_sent_without_credentials_or_not_at_all(url, safe):
    assert project_links.strip_remote_userinfo(url) == safe

