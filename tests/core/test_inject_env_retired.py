"""The ``inject_env_details`` switch is retired: it controls nothing.

It once added the app's own ``<env>`` block to a custom system prompt. The
fluid prompt compiler now supplies the environment for every agent (a stable
``# Environment`` section, carrying the session's worktree and placement
lines, plus a per-turn date/git reminder), so a stored
true, false or missing value must make no difference, must not be written,
and must not be read back out of the agent store.

The producing-path tests run the real chain: a stored agent row ->
``build_options_from_profile`` -> ``HarnessAdapter.connect_with_sdk_options``
-> the fluid compiler -> fluid's per-turn reminder.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from unittest.mock import AsyncMock, patch

import pytest

import app.core.query_options as qo
from app.core.provider import harness_adapter as ha
from app.db import database
from app.db.base import get_db

ABSENT = object()
FLAG_VALUES = [True, False, ABSENT]
WORKER = "Worker: TestWorker (device dev_test, on the host). Your shell, files and tools run on this worker."


def _legacy_config(flag, content="Custom agent prompt"):
    prompt = {"type": "custom", "content": content}
    if flag is not ABSENT:
        prompt["inject_env_details"] = flag
    return {
        "system_prompt": prompt,
        "allowed_tools": ["Read"],
        "tool_selection_mode": "allow",
        "max_turns": 7,
    }


def _insert_raw(agent_id, config):
    """Store a row the way an older build (or an older peer) left it."""
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO agents (id, name, description, config, is_builtin, mcp_tools, "
            "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (agent_id, agent_id, None, json.dumps(config), False, "[]",
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )


def _raw_config(agent_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT config FROM agents WHERE id = %s", (agent_id,))
        raw = cursor.fetchone()["config"]
    return json.loads(raw) if isinstance(raw, str) else raw


@pytest.fixture
def repo(tmp_path):
    """A real checkout on a task branch, so the branch fact has to come from git."""
    path = tmp_path / "checkout"
    path.mkdir()
    for args in (
        ["init", "-q", "-b", "task/retire-env"],
        ["-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "init"],
    ):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    return path.resolve()


# ─── the agent store ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("flag", [True, False])
def test_stored_values_never_read_back(flag):
    agent_id = f"legacy-read-{str(flag).lower()}"
    _insert_raw(agent_id, _legacy_config(flag))

    expected = _legacy_config(ABSENT)
    assert database.get_profile(agent_id)["config"] == expected
    listed = {p["id"]: p for p in database.get_all_profiles()}
    assert listed[agent_id]["config"] == expected


def test_writes_never_store_the_key():
    database.create_profile("legacy-write", "W", None, _legacy_config(True))
    assert "inject_env_details" not in _raw_config("legacy-write")["system_prompt"]

    database.update_profile("legacy-write", config=_legacy_config(False, "Edited"))
    assert _raw_config("legacy-write") == _legacy_config(ABSENT, "Edited")


def test_migration_strips_stored_rows_and_keeps_everything_else():
    _insert_raw("legacy-mig-true", _legacy_config(True))
    _insert_raw("legacy-mig-false", _legacy_config(False))
    _insert_raw("legacy-mig-absent", _legacy_config(ABSENT))
    _insert_raw("legacy-mig-string", {"system_prompt": "plain text prompt"})

    applied: list = []
    with get_db() as conn:
        database._strip_retired_prompt_keys(conn.cursor(), applied)
    assert applied == ["agents.system_prompt.inject_env_details(2)"]
    for agent_id in ("legacy-mig-true", "legacy-mig-false", "legacy-mig-absent"):
        assert _raw_config(agent_id) == _legacy_config(ABSENT)
    assert _raw_config("legacy-mig-string") == {"system_prompt": "plain text prompt"}

    again: list = []
    with get_db() as conn:
        database._strip_retired_prompt_keys(conn.cursor(), again)
    assert again == []


def test_api_schemas_accept_and_drop_the_key():
    """An older client still sending it is accepted; the key never comes back."""
    from app.api.agents import ProfilePresentation
    from app.core.models import ProfileCreate, SystemPromptConfig

    assert "inject_env_details" not in SystemPromptConfig.model_fields
    created = ProfileCreate.model_validate({"name": "A", "config": _legacy_config(True)})
    dumped = created.config.model_dump(exclude_none=True)
    assert dumped["system_prompt"] == {"type": "custom", "preset": "claude_code",
                                       "content": "Custom agent prompt"}
    read = ProfilePresentation.model_validate({
        "id": "a", "name": "A", "config": _legacy_config(True),
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
    })
    assert "inject_env" not in read.model_dump_json()


# ─── query options: the value controls nothing ──────────────────────────────


def _build(profile, cwd):
    with patch.object(qo, "get_security_instructions", return_value="SECURITY"):
        options, _ = qo.build_options_from_profile(profile=profile, overrides={"cwd": str(cwd)})
    return options


@pytest.mark.parametrize("flag", FLAG_VALUES)
def test_query_options_ignore_the_value(flag, repo):
    """Built straight from the config dict (no store normalization), so the
    builder itself must ignore the key."""
    options = _build({"id": "t", "config": _legacy_config(flag)}, repo)
    assert options.system_prompt == "SECURITY\n\nCustom agent prompt"
    assert getattr(options, "aih_turn_env", None) is None
    assert options.aih_session_runtime == {
        "execution_mode": "local", "base_branch": None, "placement_where": None,
    }


# ─── the session's runtime facts reach every agent automatically ────────────


def test_runtime_lines_name_worktree_mode_base_branch_and_placement(monkeypatch):
    import app.core.environment as env_mod

    monkeypatch.setattr(env_mod, "_in_container", lambda: False)
    assert env_mod.session_runtime_lines(
        {"execution_mode": "local", "placement_where": None}
    ) == []
    assert env_mod.session_runtime_lines(
        {"execution_mode": "worktree", "base_branch": "develop", "placement_where": "anywhere"}
    ) == ["Execution mode: worktree", "Base branch: develop", "Runtime placement: anywhere"]
    runner = env_mod.session_runtime_lines({"placement_where": "runner:dev_abc"})
    assert runner == [
        f"Runtime placement: runner:dev_abc (shared runner). {env_mod.PLACEMENT_RUNNER_NOTE}"
    ]
    assert "Do not treat an app you launch here as live verification" in runner[0]
    # Inside a container the shared-runner note rides even the local lane.
    monkeypatch.setattr(env_mod, "_in_container", lambda: True)
    assert env_mod.session_runtime_lines({"execution_mode": "local"}) == [
        f"Runtime placement: local (shared runner). {env_mod.PLACEMENT_RUNNER_NOTE}"
    ]


def test_session_runtime_reads_worktree_and_placement(monkeypatch):
    monkeypatch.setattr(
        qo.database, "get_worktree_by_session", lambda sid: {"base_branch": "release"}
    )
    row = {"branch": "task/x", "placement": json.dumps({"where": "runner:dev_abc"})}
    assert qo._session_runtime("s1", row, row, "project-v2") == {
        "execution_mode": "worktree", "base_branch": "release",
        "placement_where": "runner:dev_abc",
    }
    # No legacy worktree row -> main.
    monkeypatch.setattr(qo.database, "get_worktree_by_session", lambda sid: None)
    assert qo._session_runtime(
        "s2", {"branch": "task/y"}, None, "project-v2"
    )["base_branch"] == "main"
    # A branch-less checkout, a non-v2 cwd and the chat lane are local.
    assert qo._session_runtime("s3", {}, None, "project-v2")["execution_mode"] == "local"
    assert qo._session_runtime("s3", row, row, "override")["execution_mode"] == "local"
    assert qo._session_runtime(
        "s4", row, row, "project-v2", chat=True
    )["execution_mode"] == "local"


@pytest.mark.asyncio
async def test_worktree_runtime_reaches_the_compiled_environment(monkeypatch, repo):
    """The producing path for worktree mode and placement: build options ->
    the adapter's projection -> its connect-time identity text -> fluid's
    ``# Environment`` section (native and delegated full-replace)."""
    import app.core.environment as env_mod

    monkeypatch.setattr(env_mod, "_in_container", lambda: False)
    monkeypatch.setattr(ha, "_worker_identity_text", lambda: WORKER)
    _insert_raw("legacy-runtime", _legacy_config(True))
    session = {"id": "ses-rt", "project_v2_id": "pv2-1", "branch": "task/retire-env",
               "placement": {"where": "runner:dev_abc"}}
    with patch.object(qo, "get_security_instructions", return_value="SECURITY"), \
            patch.object(qo.database, "get_session", return_value=session), \
            patch.object(qo.database, "get_worktree_by_session",
                         return_value={"base_branch": "develop"}), \
            patch("app.core.project_resolver.resolve_cwd", return_value=str(repo)):
        options, _ = qo.build_options_from_profile(
            profile=database.get_profile("legacy-runtime"), session_id="ses-rt",
        )
    adapter = ha.HarnessAdapter()
    adapter.connect = AsyncMock()
    await adapter.connect_with_sdk_options(options)
    projected = adapter.connect.await_args.args[0]
    assert projected.extra["session_runtime"]["execution_mode"] == "worktree"

    identity = ha._session_identity_text(projected.extra["session_runtime"])
    assert identity == "\n".join([
        WORKER, "Execution mode: worktree", "Base branch: develop",
        f"Runtime placement: runner:dev_abc (shared runner). {env_mod.PLACEMENT_RUNNER_NOTE}",
    ])
    system, append = ha._system_parts(projected.system_prompt)
    compiled = ha.HarnessAdapter._compile_prompt_sections(
        system, append, projected, native=True, worker=identity,
    )
    env = [s for s in compiled.sections if s.text.startswith("# Environment")]
    assert len(env) == 1 and identity in env[0].text
    assert compiled.text.count("Base branch: develop") == 1
    assert compiled.text.count(env_mod.PLACEMENT_RUNNER_NOTE) == 1
    delegated = ha.HarnessAdapter._compile_prompt(system, append, projected, identity, None)
    assert delegated.count("Execution mode: worktree") == 1
    assert delegated.count(env_mod.PLACEMENT_RUNNER_NOTE) == 1


# ─── the producing path: stored row -> adapter -> fluid compiler ────────────


async def _project(agent_id, repo):
    options = _build(database.get_profile(agent_id), repo)
    adapter = ha.HarnessAdapter()
    adapter.connect = AsyncMock()
    await adapter.connect_with_sdk_options(options)
    projected = adapter.connect.await_args.args[0]
    adapter._options = projected
    return adapter, projected


async def _turn_reminder(compiled, cwd):
    """Fluid's own per-turn reminder, keyed on the compiled sections exactly
    as ``NativeLoop`` does it."""
    from fluid.engine.native import NativeLoop

    loop = types.SimpleNamespace(
        system_sections=compiled,
        ctx=types.SimpleNamespace(cwd=str(cwd), extra={}),
        _runtime_facts_sent=None,
        _harvest_runtime_facts=lambda: None,
    )
    loop._gather_environment = types.MethodType(NativeLoop._gather_environment, loop)
    return await NativeLoop._environment_reminder(loop)


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", FLAG_VALUES)
async def test_every_stored_value_compiles_one_fluid_environment(flag, repo):
    agent_id = f"legacy-path-{flag if flag is not ABSENT else 'absent'}".lower()
    _insert_raw(agent_id, _legacy_config(flag))
    adapter, projected = await _project(agent_id, repo)

    assert projected.cwd == str(repo)
    assert "turn_env" not in (projected.extra or {})
    assert adapter._env_note() is None  # no app-side duplicate of the date/branch

    system, append = ha._system_parts(projected.system_prompt)
    assert system == "SECURITY\n\nCustom agent prompt" and "<env>" not in system
    card = adapter._build_workspace_card(projected)
    assert card and card.startswith("# Workspace facts")

    # Native leg.
    compiled = ha.HarnessAdapter._compile_prompt_sections(
        system, append, projected, native=True, worker=WORKER, workspace_card=card,
    )
    # Two environment-kind sections: fluid's facts and the workspace card.
    env = [s for s in compiled.sections if s.text.startswith("# Environment")]
    assert len(env) == 1 and env[0].kind == "environment"
    assert f"Working directory: {repo}" in env[0].text
    assert f"Platform: {sys.platform}" in env[0].text
    assert WORKER in env[0].text
    assert "Git repository: yes" in env[0].text
    text = compiled.text
    assert text.count("# Environment") == 1 and "<env>" not in text
    assert text.count(WORKER) == 1
    assert text.count("# Workspace facts") == 1
    assert "Custom agent prompt" in text

    # The per-turn half: date and the checkout's real branch.
    note = await _turn_reminder(compiled, repo)
    assert "Today's date:" in note
    assert "Current branch: task/retire-env" in note

    # Delegated full-replace leg compiles the same single environment.
    delegated = ha.HarnessAdapter._compile_prompt(system, append, projected, WORKER, card)
    assert delegated.count("# Environment") == 1 and "<env>" not in delegated
    assert delegated.count(WORKER) == 1
    assert delegated.count("# Workspace facts") == 1


@pytest.mark.asyncio
async def test_true_false_and_absent_compile_identically(repo):
    texts = []
    for flag in FLAG_VALUES:
        agent_id = f"legacy-same-{flag if flag is not ABSENT else 'absent'}".lower()
        _insert_raw(agent_id, _legacy_config(flag))
        _, projected = await _project(agent_id, repo)
        system, append = ha._system_parts(projected.system_prompt)
        texts.append(ha.HarnessAdapter._compile_prompt_sections(
            system, append, projected, native=True, worker=WORKER,
        ).text)
    assert texts[0] == texts[1] == texts[2]


def test_the_app_env_renderer_is_gone():
    """Nothing is left that could put the app's <env> block back."""
    import app.core.environment as env_mod

    assert not hasattr(env_mod, "generate_environment_details")
