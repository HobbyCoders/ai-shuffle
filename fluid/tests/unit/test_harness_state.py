"""Global state producers, canonical precedence and host-neutral fallback (#2042)."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fluid.state import (
    project_state_root,
    safe_key,
    scratch_dir,
    scratch_prompt,
    state_path,
)
from fluid.tools.builtins import ledger, note
from fluid.tools.registry import ToolCtx


async def test_standalone_producers_leave_fresh_git_repo_clean(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    await asyncio.to_thread(subprocess.run, ["git", "init", str(repo)], check=True, capture_output=True)
    ctx = ToolCtx(cwd=str(repo), session_id="session")
    assert not (await ledger.handler({"action": "add", "section": "state", "text": "pinned"}, ctx)).is_error
    assert not (await note.handler({"action": "add", "text": "plan"}, ctx)).is_error
    global_root = project_state_root(ctx)
    assert global_root.is_relative_to(Path.home() / ".fluid" / "projects")
    assert "pinned" in (global_root / "ledger/session.md").read_text()
    assert "plan" in (global_root / "notes/session.md").read_text()
    assert not (repo / ".fluid").exists()
    assert await asyncio.to_thread(subprocess.check_output, ["git", "status", "--porcelain"], cwd=repo) == b""
    resumed = ToolCtx(cwd=str(repo), session_id="session")
    assert "pinned" in ledger.read_ledger(resumed)
    assert "plan" in note.read_notes(resumed)
    assert str(scratch_dir(resumed)) in scratch_prompt(resumed)
    assert not scratch_dir(resumed).exists()  # guidance creates nothing


@pytest.mark.parametrize("hosted", [False, True])
async def test_native_cli_scratch_reminder_is_scoped_to_cli_host(tmp_path, hosted):
    from fluid.cli.host import CliHost
    from fluid.config.model import HarnessConfig
    from fluid.engine.native import NativeLoop
    from fluid.events import UserTurn
    from fluid.codemode.journal import strip_reminders
    from fluid.providers.base import ProviderCaps, StreamChunk
    from fluid.store.base import MemoryStore

    class Provider:
        name = "fixture"
        caps = ProviderCaps()

        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            for chunk in (
                StreamChunk("message_start", {"usage": {}}),
                StreamChunk("block_start", {"index": 0, "block_type": "text"}),
                StreamChunk("text_delta", {"index": 0, "text": "ok"}),
                StreamChunk("block_stop", {"index": 0}),
                StreamChunk("usage", {"usage": {}, "stop_reason": "end_turn"}),
                StreamChunk("message_stop", {}),
            ):
                yield chunk

    provider, store = Provider(), MemoryStore()
    extra = {"harness_state_root": tmp_path / "host-state"} if hosted else {}
    loop = NativeLoop(provider=provider, model="fixture", store=store,
                      ctx=ToolCtx(cwd=str(tmp_path), extra=extra))
    host = CliHost(HarnessConfig(cwd=str(tmp_path)), store).attach(loop)
    try:
        await loop.submit(UserTurn(content="owner's exact prompt"))
        await asyncio.wait_for(loop.wait_turn(), 5)
        [request] = provider.requests
        text = request.messages[-1].text()
        assert ("# Harness scratch" in text) is not hosted
        assert strip_reminders(text) == "owner's exact prompt"
        nodes = await store.load_path(loop.session_id)
        assert strip_reminders(nodes[0].message.text()) == "owner's exact prompt"
        if not hosted:
            assert str(scratch_dir(loop.ctx)) in text
        assert not scratch_dir(loop.ctx).exists()
    finally:
        await host.aclose()
        await loop.aclose()


def test_scratch_guidance_uses_existing_reminder_consumers(tmp_path):
    from fluid.codemode.journal import strip_reminders
    from fluid.context.compaction import strip_system_reminders

    ctx = ToolCtx(cwd=str(tmp_path / "repo"), session_id="session")
    reminder = scratch_prompt(ctx)
    assert reminder.startswith("<system-reminder>\n# Harness scratch\n")
    assert reminder.endswith("\n</system-reminder>")
    assert str(scratch_dir(ctx)) in reminder
    for strip in (strip_reminders, strip_system_reminders):
        assert strip(reminder) == ""
        assert strip("owner's exact prompt\n\n" + reminder) == "owner's exact prompt"
    assert not scratch_dir(ctx).exists()


@pytest.mark.parametrize("kind", ["ledger", "notes"])
async def test_legacy_copy_never_overwrites_new_or_cleared_state(tmp_path, kind):
    repo = tmp_path / "repo"
    legacy = repo / ".fluid" / kind / "s.md"
    legacy.parent.mkdir(parents=True)
    old = "# Session ledger\n## state\n- old" if kind == "ledger" else "- old"
    legacy.write_text(old)
    ctx = ToolCtx(cwd=str(repo), session_id="s", extra={"harness_state_root": tmp_path / "global"})
    reader = ledger.read_ledger if kind == "ledger" else note.read_notes
    assert "old" in reader(ctx)
    canonical = state_path(ctx, kind)
    assert canonical.read_text() == old
    if kind == "ledger":
        await ledger.handler({"action": "replace", "section": "state", "entries": ["new"]}, ctx)
    else:
        await note.handler({"action": "clear"}, ctx)
        await note.handler({"action": "add", "text": "new"}, ctx)
    restarted = ToolCtx(cwd=str(repo), session_id="s", extra=dict(ctx.extra))
    assert "new" in reader(restarted) and "old" not in reader(restarted)
    if kind == "ledger":
        await ledger.handler({"action": "replace", "section": "state", "entries": []}, ctx)
    else:
        await note.handler({"action": "clear"}, ctx)
    assert reader(restarted) == ""
    assert canonical.exists() and canonical.read_text() == ""
    assert legacy.read_text() == old


def test_safe_ids_and_project_session_isolation(tmp_path):
    ids = ["../escape", "/absolute", r"..\escape", "s/child", "normal", "normal-", ".", "é"]
    keys = [safe_key(s) for s in ids]
    assert len(set(keys)) == len(keys)
    assert all("/" not in s and "\\" not in s and s not in (".", "..") for s in keys)
    for sid in ids:
        ctx = ToolCtx(cwd=str(tmp_path), session_id=sid, extra={"harness_state_root": tmp_path / "global"})
        assert state_path(ctx, "ledger").parent == tmp_path / "global/ledger"
        assert scratch_dir(ctx).parent == tmp_path / "global/scratch"
    one = ToolCtx(cwd=str(tmp_path / "one"), session_id="s")
    two = ToolCtx(cwd=str(tmp_path / "two"), session_id="s")
    assert state_path(one, "ledger") != state_path(two, "ledger")
    # Embedders may already provide a canonical memory identity: honor it,
    # while keeping both project and session IDs confined to one component.
    one.extra["memory_project"] = two.extra["memory_project"] = "git:shared"
    assert project_state_root(one) == project_state_root(two)
    one.extra["memory_project"] = "../unsafe-project"
    assert project_state_root(one).parent == Path.home() / ".fluid/projects"


async def test_standalone_worktree_identity_survives_restart(tmp_path):
    repo, worktree = tmp_path / "repo", tmp_path / "worktree"
    repo.mkdir()
    await asyncio.to_thread(subprocess.run, ["git", "init", str(repo)], check=True, capture_output=True)
    await asyncio.to_thread(subprocess.run, ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "fixture"], cwd=repo, check=True, capture_output=True)
    await asyncio.to_thread(subprocess.run, ["git", "worktree", "add", "-b", "fixture", str(worktree)], cwd=repo, check=True, capture_output=True)
    ctx = ToolCtx(cwd=str(worktree), session_id="s")
    await ledger.handler({"action": "add", "section": "state", "text": "persisted"}, ctx)
    restarted = ToolCtx(cwd=str(repo), session_id="s")
    assert project_state_root(ctx) == project_state_root(restarted)
    assert "persisted" in ledger.read_ledger(restarted)
    assert not (worktree / ".fluid").exists()


def test_legacy_symlink_escape_is_not_copied(tmp_path):
    ctx = ToolCtx(cwd=str(tmp_path / "repo"), session_id="s", extra={"harness_state_root": tmp_path / "global"})
    secret = tmp_path / "outside.md"
    secret.write_text("not legacy state")
    legacy = tmp_path / "repo/.fluid/ledger/s.md"
    legacy.parent.mkdir(parents=True)
    legacy.symlink_to(secret)
    assert ledger.read_ledger(ctx) == ""
    assert not state_path(ctx, "ledger").exists()


@pytest.mark.parametrize("kind", ["ledger", "notes"])
async def test_legacy_read_survives_copy_failure_without_writing_old_file(tmp_path, monkeypatch, kind):
    import os

    ctx = ToolCtx(cwd=str(tmp_path / "repo"), session_id="s", extra={"harness_state_root": tmp_path / "global"})
    legacy = tmp_path / "repo/.fluid" / kind / "s.md"
    legacy.parent.mkdir(parents=True)
    old = "# Session ledger\n## state\n- old" if kind == "ledger" else "- old"
    legacy.write_text(old)
    reader = ledger.read_ledger if kind == "ledger" else note.read_notes
    def unsupported(*args):
        raise OSError("hard links unsupported")
    monkeypatch.setattr(os, "link", unsupported)
    assert "old" in reader(ctx)
    assert not state_path(ctx, kind).exists()
    if kind == "ledger":
        result = await ledger.handler({"action": "add", "section": "state", "text": "new"}, ctx)
    else:
        result = await note.handler({"action": "add", "text": "new"}, ctx)
    assert not result.is_error
    assert "old" in reader(ctx) and "new" in reader(ctx)
    assert legacy.read_text() == old
    assert not list((tmp_path / "global" / kind).glob("*.tmp"))


def test_concurrent_new_state_wins_legacy_publish_race(tmp_path, monkeypatch):
    import os

    ctx = ToolCtx(cwd=str(tmp_path / "repo"), session_id="s", extra={"harness_state_root": tmp_path / "global"})
    legacy = tmp_path / "repo/.fluid/ledger/s.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("# Session ledger\n## state\n- old")
    real_link = os.link
    def race(src, dst):
        Path(dst).write_text("# Session ledger\n## state\n- new")
        real_link(src, dst)
    monkeypatch.setattr(os, "link", race)
    assert "new" in ledger.read_ledger(ctx) and "old" not in ledger.read_ledger(ctx)
    assert "old" in legacy.read_text()
    assert not list((tmp_path / "global/ledger").glob("*.tmp"))


@pytest.mark.parametrize("leg", ["native", "delegated"])
async def test_fresh_engine_stamps_session_for_global_producers_and_resume(tmp_path, leg):
    from fluid.engine.delegated import DelegatedLoop
    from fluid.engine.native import NativeLoop
    from fluid.store.base import MemoryStore

    repo = tmp_path / "repo"
    repo.mkdir()
    store = MemoryStore()
    def create(ctx, sid=None):
        if leg == "native":
            return NativeLoop(provider=None, model="fixture", store=store, ctx=ctx, session_id=sid)
        return DelegatedLoop(model="fixture", store=store, ctx=ctx, session_id=sid, claude_cmd=["not-run"])
    first, second = create(ToolCtx(cwd=str(repo))), create(ToolCtx(cwd=str(repo)))
    try:
        meta = await first._ensure_session()
        other = await second._ensure_session()
        assert first.ctx.session_id == meta.session_id != other.session_id == second.ctx.session_id
        await ledger.handler({"action": "add", "section": "state", "text": "pinned"}, first.ctx)
        await note.handler({"action": "add", "text": "plan"}, first.ctx)
        assert ledger.read_ledger(second.ctx) == "" and note.read_notes(second.ctx) == ""
        assert scratch_dir(first.ctx) != scratch_dir(second.ctx)
        restarted = create(ToolCtx(cwd=str(repo)), meta.session_id)
        try:
            await restarted._ensure_session()
            assert restarted.ctx.session_id == meta.session_id
            assert "pinned" in ledger.read_ledger(restarted.ctx)
            assert "plan" in note.read_notes(restarted.ctx)
        finally:
            await restarted.aclose()
        assert not (repo / ".fluid").exists()
    finally:
        await first.aclose()
        await second.aclose()


async def test_standalone_disk_restart_from_real_worktree_without_app(tmp_path):
    """A new interpreter resumes producer-written state, not seeded expectations."""
    repo, worktree = tmp_path / "repo", tmp_path / "worktree"
    repo.mkdir()
    await asyncio.to_thread(subprocess.run, ["git", "init", str(repo)], check=True, capture_output=True)
    await asyncio.to_thread(subprocess.run, ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "fixture"], cwd=repo, check=True, capture_output=True)
    await asyncio.to_thread(subprocess.run, ["git", "worktree", "add", "-b", "fixture", str(worktree)], cwd=repo, check=True, capture_output=True)
    script = """
import asyncio, json, sys
from pathlib import Path
from fluid.engine.native import NativeLoop
from fluid.permissions.engine import PermissionEngine
from fluid.state import project_state_root, scratch_dir
from fluid.store.jsonl import JsonlStore
from fluid.tools.builtins import ledger, note
from fluid.tools.registry import ToolCtx, ToolRegistry

async def main():
    registry = ToolRegistry()
    for module in (ledger, note):
        registry.register_builtin(module.SPEC, module.handler)
    ctx = ToolCtx(cwd=str(Path.cwd()))
    store = JsonlStore(Path(sys.argv[1]))
    loop = NativeLoop(provider=None, store=store, model="not-called",
                      ctx=ctx, registry=registry, permissions=PermissionEngine("on_write"),
                      session_id=sys.argv[2] if len(sys.argv) > 2 else None)
    try:
        meta = await loop._ensure_session()
        if len(sys.argv) == 2:
            for name, args in [("Ledger", {"action": "add", "section": "state", "text": "disk pinned"}),
                               ("Note", {"action": "add", "text": "disk plan"})]:
                result = await loop._gated_call(meta.session_id, "turn", name, name, args)
                assert not result.is_error, result.text
        assert ctx.session_id == meta.session_id
        assert not any(name == "app" or name.startswith("app.") for name in sys.modules)
        print(json.dumps({"sid": meta.session_id, "root": str(project_state_root(ctx)),
                          "scratch": str(scratch_dir(ctx)), "ledger": loop._ledger_reminder(),
                          "notes": loop._notes_reminder()}))
    finally:
        await loop.aclose()
        await store.aclose()
asyncio.run(main())
"""
    # Only Fluid is importable from this checkout; cwd is the disposable repo.
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    store = tmp_path / "store"
    first = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script, str(store)], cwd=worktree, env=env, check=True, capture_output=True, text=True, timeout=30)
    written = json.loads(first.stdout)
    second = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script, str(store), written["sid"]], cwd=repo, env=env, check=True, capture_output=True, text=True, timeout=30)
    resumed = json.loads(second.stdout)
    assert resumed == written
    assert "disk pinned" in resumed["ledger"] and "disk plan" in resumed["notes"]
    assert Path(resumed["root"]).is_relative_to(Path.home() / ".fluid/projects")
    for checkout in (repo, worktree):
        assert not (checkout / ".fluid").exists()
        assert await asyncio.to_thread(subprocess.check_output, ["git", "status", "--porcelain"], cwd=checkout) == b""
