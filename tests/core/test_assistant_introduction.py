"""Detected introduction/Git-author compatibility stays unchanged when memory is fed."""
from __future__ import annotations
from typing import Any
from pytest import MonkeyPatch
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import assistant_introduction as introductions, assistant_service as service
from app.core.watch_manager import WatchManager
from app.db import assistant as store, checkouts, database, projects_v2
from tests.db.test_assistant_store import assistant_fixture  # noqa: F401

# conftest stubs github_login for every test; keep the real one for its own test.
REAL_GITHUB_LOGIN = introductions.github_login


def introduction():
    """The effective stored introduction (what the default git author reads)."""
    return introductions.effective((store.get_account() or {}).get("introduction") or {})


def test_corrections_win_and_empty_goes_back_to_detection():
    stored = introductions.with_detected({}, {"preferred_name": "ada", "github_username": "ada-gh"})
    stored = introductions.correct(stored, {"preferred_name": "Ada Lovelace"})
    assert introductions.effective(stored) == {"preferred_name": "Ada Lovelace", "github_username": "ada-gh"}
    # A later detection pass never overwrites a correction.
    stored = introductions.with_detected(stored, {"preferred_name": "ada2", "github_username": "ada-new"})
    assert introductions.effective(stored) == {"preferred_name": "Ada Lovelace", "github_username": "ada-new"}
    stored = introductions.correct(stored, {"preferred_name": ""})
    assert introductions.effective(stored)["preferred_name"] == "ada2"
    with pytest.raises(ValueError):
        introductions.correct(stored, {"projects": ["typed"]})


def test_an_introduction_typed_before_detection_is_kept_as_corrections():
    legacy = {"preferred_name": "Ada", "repository_refs": ["ada/project"], "notes": "Brief", "github_username": ""}
    detected, corrections = introductions.split(legacy)
    assert detected == {} and corrections == {"preferred_name": "Ada", "repository_refs": ["ada/project"], "notes": "Brief"}
    stored = introductions.with_detected(legacy, {"preferred_name": "ada", "email": "ada@example.com"})
    assert introductions.effective(stored) == {"preferred_name": "Ada", "email": "ada@example.com",
                                               "repository_refs": ["ada/project"], "notes": "Brief"}


def test_detect_reads_admin_projects_checkout_remotes_github_and_email(monkeypatch, tmp_path):
    from app.core.github_service import github_service
    database.create_admin("ada", "hash")
    project = projects_v2.upsert_project("git:abc", name="Shuffle")
    runner = service.assistant_id()
    checkouts.upsert_checkout(project["id"], runner, str(tmp_path / "shuffle"))
    checkouts.upsert_checkout(project["id"], runner, str(tmp_path / "copy"))
    checkouts.upsert_checkout(project["id"], "other-worker", str(tmp_path / "elsewhere"))
    remotes = {str(tmp_path / "shuffle"): "ada/shuffle", str(tmp_path / "copy"): "ada/shuffle",
               str(tmp_path / "elsewhere"): "someone/else"}
    monkeypatch.setattr(github_service, "get_repo_from_remote", lambda path: remotes.get(path))
    monkeypatch.setattr(introductions, "github_login", lambda unknown=None: "ada-gh")
    found = introductions.detect(runner, ["", "not an address", "ada@example.com"])
    assert found == {"preferred_name": "ada", "github_username": "ada-gh", "email": "ada@example.com",
                     "projects": ["Shuffle"], "repository_refs": ["ada/shuffle"]}


def test_detect_leaves_out_what_it_cannot_find():
    assert introductions.detect(service.assistant_id()) == {}


def test_github_login_needs_a_signed_in_cli(monkeypatch):
    from app.core import auth
    monkeypatch.setattr(auth, "is_github_authenticated_cached", lambda probe=None: False)
    monkeypatch.setattr(auth.auth_service, "github_login", lambda: pytest.fail("gh api ran while signed out"))
    assert REAL_GITHUB_LOGIN() is None
    monkeypatch.setattr(auth, "is_github_authenticated_cached", lambda probe=None: True)
    monkeypatch.setattr(auth.auth_service, "github_login", lambda: "https://evil")
    assert REAL_GITHUB_LOGIN() is None
    monkeypatch.setattr(auth.auth_service, "github_login", lambda: "ada-gh")
    assert REAL_GITHUB_LOGIN() == "ada-gh"


def test_detection_is_stored_shown_and_publishes_only_on_change(assistant_fixture, monkeypatch):
    published = []
    monkeypatch.setattr(service, "_publish", lambda: published.append(1))
    with store.get_db() as conn:
        store.write_account(conn, {"introduction": introductions.correct({}, {"preferred_name": "Ada"})})
    assert service.apply_detection({"preferred_name": "ada", "github_username": "ada-gh"})
    assert service.apply_detection({"preferred_name": "ada", "github_username": "ada-gh"}) is False
    assert len(published) == 1
    assert introduction() == {"preferred_name": "Ada", "github_username": "ada-gh"}
    assert introductions.split(store.get_account()["introduction"]) == (
        {"preferred_name": "ada", "github_username": "ada-gh"}, {"preferred_name": "Ada"})


def test_each_configured_worker_detects_locally_and_publishes_only_changes(assistant_fixture, monkeypatch):
    published = []
    monkeypatch.setattr(service, "_publish", lambda: published.append(True))
    assert service.detection_wanted()
    assert service.apply_detection({"preferred_name": "local-owner"})
    assert store.get_account()["introduction"]["detected"]["preferred_name"] == "local-owner"
    assert not service.apply_detection({"preferred_name": "local-owner"})
    assert published == [True]


def test_nothing_is_written_before_setup(monkeypatch):
    monkeypatch.setattr(service, "_publish", lambda: pytest.fail("published without an assistant"))
    assert service.apply_detection({"preferred_name": "ada"}) is False
    assert store.get_account() is None


async def test_manager_pass_reads_the_running_gmail_address_not_mail(
    assistant_fixture: Any, monkeypatch: MonkeyPatch
) -> None:
    from app.core.connectors.manager import connector_manager

    async def running(connector_id):
        assert connector_id == "conn-mail"
        return SimpleNamespace(_email_address="ada@example.com")
    monkeypatch.setattr(connector_manager, "get_running_connector", running)
    manager = WatchManager()
    await manager.detect_owner()
    await manager.detect_owner()  # fresh enough: cached periodic detection
    assert introduction()["email"] == "ada@example.com"


def test_ordinary_project_agent_gets_default_tools_and_enabled_plugins(
    assistant_fixture: Any, monkeypatch: MonkeyPatch, tmp_path: Path,
) -> None:
    """#557: plugin defaults belong to ordinary agents, not a retired research agent."""
    from app.core import mcp_config, plugin_service
    from app.core.query_options import build_options_from_profile
    plugin_dir = tmp_path / "helper"
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    installed = tmp_path / "installed_plugins.json"
    installed.write_text(
        json.dumps({"plugins": {"helper@market": [{"installPath": str(plugin_dir)}]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(plugin_service, "get_plugin_service", lambda: SimpleNamespace(
        INSTALLED_FILE=installed, get_enabled_plugins=lambda: {"helper@market": True, "off@market": False}))
    agent = database.create_profile("ordinary-agent", "Agent", None, {
        "tool_selection_mode": "all", "agent_invoke_mode": "all",
    })
    config = agent["config"]
    assert "enabled_plugins" not in config and "setting_sources" not in config
    database.create_project("plugin-project", "Project", None, str(tmp_path))
    run = database.create_session(
        "project-run", "ordinary-agent", project_id="plugin-project", is_active=True,
    )
    options, _ = build_options_from_profile(agent, session_id=run["id"])
    assert {"type": "local", "path": str(plugin_dir)} in (options.plugins or [])
    assert mcp_config._build_plugins_list(
        config.get("enabled_plugins"), config.get("setting_sources"),
    ) == options.plugins


def test_a_source_that_cannot_answer_keeps_its_last_value():
    stored = introductions.with_detected({}, {"preferred_name": "ada-account", "github_username": "ada-gh",
                                              "email": "ada@example.com"})
    # gh offline, the hub unreachable (admin fallback found) and Gmail still starting.
    after = introductions.with_detected(stored, {"preferred_name": "admin"},
                                        unknown={"github_username", "preferred_name", "email"})
    assert after == stored
    # A source that answered "nothing" does drop the value.
    assert introductions.with_detected(stored, {})["detected"] == {}
    # Nothing earlier to keep: the fallback is used.
    fallback = introductions.with_detected({}, {"preferred_name": "admin"}, unknown={"preferred_name"})
    assert introductions.effective(fallback) == {"preferred_name": "admin"}


def test_detect_reports_sources_that_could_not_answer(monkeypatch):
    from app.core import auth
    monkeypatch.setattr(introductions, "github_login", REAL_GITHUB_LOGIN)
    monkeypatch.setattr(auth, "is_github_authenticated_cached", lambda probe=None: True)
    monkeypatch.setattr(auth.auth_service, "github_login", lambda: None)  # gh api user failed

    def broken(runner):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(introductions, "projects_and_repositories", broken)
    unknown = set()
    assert introductions.detect(service.assistant_id(), [""], unknown) == {}
    assert unknown == {"github_username", "email", "projects", "repository_refs"}


def test_repositories_are_sorted_and_worktrees_do_not_crowd_them_out(monkeypatch, tmp_path):
    from app.core.github_service import github_service
    runner = service.assistant_id()
    remotes = {}
    for name in ("zeta", "alpha", "mid"):
        project = projects_v2.upsert_project("git:" + name, name=name)
        base = str(tmp_path / name)
        checkouts.upsert_checkout(project["id"], runner, base)
        remotes[base] = "ada/" + name
        for n in range(introductions.MAX_ITEMS * 2):
            worktree = str(tmp_path / f"{name}-wt{n}")
            checkouts.upsert_checkout(project["id"], runner, worktree, branch=f"wt{n}")
            remotes[worktree] = "ada/" + name
    monkeypatch.setattr(github_service, "get_repo_from_remote", lambda path: remotes.get(path))
    repositories = introductions.projects_and_repositories(runner)[1]
    assert repositories == ["ada/alpha", "ada/mid", "ada/zeta"]
    # Touching a checkout (which bumps updated_at) does not reorder them.
    checkouts.upsert_checkout(projects_v2.upsert_project("git:zeta", name="zeta")["id"], runner, str(tmp_path / "zeta"))
    assert introductions.projects_and_repositories(runner)[1] == repositories


async def test_signed_in_gh_that_cannot_answer_is_not_a_missing_connection(
    assistant_fixture: Any, monkeypatch: MonkeyPatch
) -> None:
    from app.core import auth
    service.apply_detection({"github_username": "ada-gh"})
    monkeypatch.setattr(introductions, "github_login", REAL_GITHUB_LOGIN)
    monkeypatch.setattr(auth, "is_github_authenticated_cached", lambda probe=None: True)
    monkeypatch.setattr(auth.auth_service, "github_login", lambda: None)
    await WatchManager().detect_owner()
    assert introduction()["github_username"] == "ada-gh"


async def test_no_detection_without_an_assistant_running_here(monkeypatch: MonkeyPatch) -> None:
    def unexpected(*args, **kwargs):
        pytest.fail("detected without an assistant")
    monkeypatch.setattr(introductions, "detect", unexpected)
    await WatchManager().detect_owner()
