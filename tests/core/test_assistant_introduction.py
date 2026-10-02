"""The owner introduction is detected, shown and correctable; never typed in."""
import json
from types import SimpleNamespace

import pytest

from app.core import assistant_introduction as introductions, assistant_service as service
from app.core.assistant_manager import AssistantManager
from app.db import assistant as store, checkouts, database, projects_v2
from tests.db.test_assistant_store import assistant_fixture  # noqa: F401

# conftest stubs github_login for every test; keep the real one for its own test.
REAL_GITHUB_LOGIN = introductions.github_login


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
    monkeypatch.setattr(introductions, "github_login", lambda: "ada-gh")
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
    service.configure({"introduction": {"preferred_name": "Ada"}})
    published.clear()
    assert service.apply_detection({"preferred_name": "ada", "github_username": "ada-gh"}, {"gmail": True, "github": True})
    assert service.apply_detection({"preferred_name": "ada", "github_username": "ada-gh"}, {"gmail": True, "github": True}) is False
    assert len(published) == 1
    state = service.get_state()
    assert state["introduction"] == {"preferred_name": "Ada", "github_username": "ada-gh"}
    assert state["introduction_detected"] == {"preferred_name": "ada", "github_username": "ada-gh"}
    assert state["introduction_corrected"] == ["preferred_name"]
    context = service.build_role_context(assistant_fixture["id"])
    assert '"preferred_name": "Ada"' in context and "ada-gh" in context and "not access grants" in context


def test_connection_suggestions_follow_the_last_pass(assistant_fixture, monkeypatch):
    monkeypatch.setattr(service, "_connections", {})
    assert service.get_state()["suggested_connections"] == []  # nothing checked yet
    service.apply_detection({}, {"gmail": False, "github": False})
    assert service.get_state()["suggested_connections"] == ["gmail", "github"]
    service.apply_detection({}, {"gmail": True, "github": False})
    assert service.get_state()["suggested_connections"] == ["github"]


def test_only_the_worker_that_runs_the_assistant_writes_detection(assistant_fixture, monkeypatch):
    monkeypatch.setattr(service, "_home_kind", lambda account, home: "remote")
    monkeypatch.setattr(service, "_publish", lambda: pytest.fail("a non-runner published detection"))
    assert service.apply_detection({"preferred_name": "elsewhere"}, {"gmail": False, "github": False}) is False
    assert "preferred_name" not in (store.get_account().get("introduction") or {}).get("detected", {})
    assert service.get_state()["suggested_connections"] == []


def test_nothing_is_written_before_setup(monkeypatch):
    monkeypatch.setattr(service, "_publish", lambda: pytest.fail("published without an assistant"))
    assert service.apply_detection({"preferred_name": "ada"}, {"gmail": False, "github": False}) is False
    assert store.get_account() is None


async def test_manager_pass_reads_the_running_gmail_address_not_mail(assistant_fixture, monkeypatch):
    from app.core.connectors.manager import connector_manager

    async def running(connector_id):
        assert connector_id == "conn-mail"
        return SimpleNamespace(_email_address="ada@example.com")
    monkeypatch.setattr(connector_manager, "get_running_connector", running)
    manager = AssistantManager()
    task = manager.detect_owner()
    assert manager.detect_owner() is task  # single flight
    await task
    assert manager.detect_owner(max_age=3600) is None  # fresh enough
    state = service.get_state()
    assert state["introduction"]["email"] == "ada@example.com"
    assert state["suggested_connections"] == ["github"]


def test_dedicated_agent_gets_default_tools_and_every_enabled_plugin(monkeypatch, tmp_path):
    """A home turn on the dedicated agent loads the install's plugins like any agent."""
    from app.core import mcp_config, plugin_service
    from app.core.query_options import build_options_from_profile
    plugin_dir = tmp_path / "helper"
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    installed = tmp_path / "installed_plugins.json"
    installed.write_text(json.dumps({"plugins": {"helper@market": [{"installPath": str(plugin_dir)}]}}), encoding="utf-8")
    monkeypatch.setattr(plugin_service, "get_plugin_service", lambda: SimpleNamespace(
        INSTALLED_FILE=installed, get_enabled_plugins=lambda: {"helper@market": True, "off@market": False}))
    state = service.configure({"create_agent": True})
    agent = database.get_profile(state["agent_id"])
    config = agent["config"]
    assert config["tool_selection_mode"] == "all" and config["agent_invoke_mode"] == "all"
    assert "enabled_plugins" not in config and "setting_sources" not in config
    home = service.open_home()
    options, _ = build_options_from_profile(agent, session_id=home["id"])
    assert {"type": "local", "path": str(plugin_dir)} in (options.plugins or [])
    assert mcp_config._build_plugins_list(config.get("enabled_plugins"), config.get("setting_sources")) == options.plugins
