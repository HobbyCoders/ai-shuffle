"""Real checkout bindings and checkpoint readers keep the session's cwd."""
import json

import pytest

from app.api import commands
from app.core import project_resolver
from app.core.config import settings
from app.core.jsonl_rewind import JSONLRewindService
from app.core.message_resolver import resolve_working_dir
from app.core.provider.fluid_transcript import (
    CLI_PROJECT_DIR_MAX,
    _write_cli_transcript,
    build_cli_transcript_entries,
    cli_project_dir_name,
)
from app.db import checkouts, database, projects_v2, sessions
from tests.git_repo_helpers import git, hermetic_git_env, init_repo


@pytest.mark.parametrize("hashed", [False, True])
def test_jsonl_reader_prefers_the_cli_writer_cwd_over_stale_copy(tmp_path, hashed):
    from app.core.jsonl_parser import get_project_dir_name

    root = tmp_path / "cli-projects"
    cwd = "/repo/.fluid/a_b" + ("x" * CLI_PROJECT_DIR_MAX if hashed else "")
    sid = "11111111-2222-4333-8444-555555555555"
    # The old reader preferred this punctuation-preserving spelling; a
    # directory scan could also choose another checkout's stale copy first.
    stale = root / get_project_dir_name(cwd)
    stale.mkdir(parents=True)
    (stale / f"{sid}.jsonl").write_text('{"type":"user","message":{"role":"user","content":"stale"}}\n')
    if hashed:
        # Match the real CLI's existing shortened folder, which the reseed
        # producer reuses instead of inventing a different hash algorithm.
        (root / (cli_project_dir_name(cwd)[:CLI_PROJECT_DIR_MAX] + "-hash")).mkdir()
    entries = build_cli_transcript_entries([
        {"type": "user", "message": {"role": "user", "content": "retained"}}
    ], sid, cwd)
    written = _write_cli_transcript(entries, sid, cwd, root)
    assert written.parent != stale
    service = JSONLRewindService()
    service.claude_projects_dir = root
    assert service._get_jsonl_path(sid, cwd) == written
    assert service.get_checkpoints(sid, cwd)[0].full_message == "retained"


@pytest.mark.parametrize("binding", ["base", "v2-branch", "legacy-worktree"])
async def test_checkpoint_discovery_uses_real_session_checkout(mock_db, monkeypatch, tmp_path, binding):
    hermetic_git_env(monkeypatch, tmp_path)
    base = init_repo(tmp_path / "base")
    branch = "feature/rewind"
    worktree = tmp_path / "branch"
    git(base, "worktree", "add", "-b", branch, str(worktree))
    database.create_profile("agent", "Agent", None, {"model": "sonnet"})
    database.create_project("project", "Project", None, str(base))
    project = projects_v2.upsert_project("local:project", name="Project")
    checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(base))
    checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(worktree), branch=branch)
    fields = {"project_v2_id": project["id"]}
    if binding == "legacy-worktree":
        repository = database.create_git_repository("repo", "project")
        database.create_worktree("worktree", repository["id"], branch, str(worktree))
        fields.update(worktree_id="worktree", worktree_branch=branch)
    database.create_session("session", "agent", project_id="project", **fields)
    if binding == "v2-branch":
        sessions.set_session_branch("session", branch, project["id"])
    database.update_session("session", sdk_session_id="11111111-2222-4333-8444-555555555555")
    row = database.get_session("session")
    expected = str(base if binding == "base" else worktree)
    assert project_resolver.resolve_session_cwd(row) == expected
    assert commands.get_working_dir_for_project("project", row.get("worktree_id"), "session") == expected
    assert resolve_working_dir(row) == expected

    # Exercise the real CLI-file producer and JSONL discovery rather than
    # stubbing _get_checkpoints with a preselected cwd. Duplicate the UUID in
    # the base folder so a lost branch identity reads the WRONG checkpoint.
    root = tmp_path / "cli-projects"
    sid = row["sdk_session_id"]
    service = JSONLRewindService()
    service.claude_projects_dir = root
    for cwd, text in [(str(base), "base prompt"), (expected, "session prompt")]:
        entries = build_cli_transcript_entries([
            {"type": "user", "message": {"role": "user", "content": text}}
        ], sid, cwd)
        folder = root / cli_project_dir_name(cwd)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{sid}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
    monkeypatch.setattr(commands, "jsonl_rewind_service", service)
    result = await commands.get_rewind_checkpoints("session")
    assert result.success and result.checkpoints[0].full_message == "session prompt"
    # The legacy execute route also discovers its checkpoint in this cwd,
    # even when it only cancels without touching the provider transcript.
    cancelled = await commands.execute_rewind_legacy("session", commands.RewindRequest(checkpoint_index=0, restore_option=4))
    assert cancelled.success

    if binding != "base":
        git(worktree, "checkout", "-b", "wrong-branch")
        assert project_resolver.resolve_session_cwd(database.get_session("session")) is None
        # Read-only history/command discovery keeps its historical fallback.
        # The mutation boundary below, not this helper, protects CLI reseeds.
        assert commands.get_working_dir_for_project("project", row.get("worktree_id"), "session")


@pytest.mark.parametrize("kind", ["delegated", "native", "orphan-native"])
async def test_only_delegated_reseed_refuses_unavailable_checkout(mock_db, monkeypatch, tmp_path, kind):
    from app.core import config
    from app.core.provider import fluid_transcript
    from app.core.provider.fluid_pg_store import PgFluidStore
    from fluid.messages.content import Message, Text
    from fluid.store.base import SessionNode

    hermetic_git_env(monkeypatch, tmp_path)
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path / "workspace")
    monkeypatch.setattr(config.settings, "claude_projects_dir", tmp_path / "cli-projects")
    base = init_repo(tmp_path / "base")
    database.create_profile("agent", "Agent", None, {"model": "sonnet"})
    database.create_project("project", "Project", None, str(base))
    project = projects_v2.upsert_project("local:project", name="Project")
    checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(base))
    database.create_session("session", "agent", project_id="project", project_v2_id=project["id"])
    # A branch with no checkout on this worker, or an orphan legacy project.
    if kind == "orphan-native":
        database.create_project("orphan", "Orphan", None, str(tmp_path / "gone"))
        from app.db.base import get_cursor, get_db
        with get_db() as conn:
            get_cursor(conn).execute("UPDATE sessions SET project_id='orphan', project_v2_id=NULL WHERE id='session'")
    else:
        sessions.set_session_branch("session", "feature/missing", project["id"])
    sdk = "ses_rewindcwd01"
    store = PgFluidStore()
    await store.create_session(sdk)
    parent = None
    for nid, role, text in [("u1", "user", "retain"), ("a1", "assistant", "answer"),
                            ("u2", "user", "remove"), ("a2", "assistant", "answer two")]:
        await store.append(sdk, SessionNode(node_id=nid, parent_id=parent,
                                          message=Message(role=role, content=[Text(text)])))
        parent = nid
    database.update_session("session", sdk_session_id=sdk)
    if kind == "delegated":
        await store.set_cli_session_id(sdk, "cli-original")
    # Discovery is read-only and can read the Fluid tree without a checkout.
    checkpoints = await commands.get_rewind_checkpoints("session")
    assert checkpoints.success and [cp.uuid for cp in checkpoints.checkpoints] == ["u1", "a1", "u2", "a2"]
    result = await commands.execute_rewind("session", commands.RewindRequestV2(target_uuid="u2", include_response=False))
    if kind == "delegated":
        assert not result.success and not result.chat_rewound
        assert "checkout" in result.error and "retry" in result.error
        assert len(fluid_transcript.load_fluid_path(sdk)) == 4
        assert fluid_transcript.get_cli_session_id(sdk) == "cli-original"
        assert not (tmp_path / "cli-projects").exists()
    else:
        assert result.success and result.chat_rewound
        assert len(fluid_transcript.load_fluid_path(sdk)) == 2
