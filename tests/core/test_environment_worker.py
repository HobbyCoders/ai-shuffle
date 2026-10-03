"""Every agent prompt names the worker it runs on.

The owner runs several networked workers ("Unraid", "DesktopWorker") and drives
sessions on any of them from any device. Before this, an agent on Unraid could
not tell that a refusal came from DesktopWorker, or that the owner's screen was a
different machine: nothing in its environment said which machine it was.

These pin the ``Worker:`` line's states: a networked name with its peers, the
non-networked fallback, a failed lookup (no line, prompt still renders), its
place in the session-stable half, and a moved session rendering the new worker.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from fluid.tools.registry import ToolRegistry

import app.core.environment as env_mod
import app.core.provider.harness_adapter as ha
from app.core.environment import (
    generate_environment_details,
    generate_turn_environment_note,
    worker_identity_lines,
)
from app.core.provider.types import SessionOptions

SELF = "dev_aaaaaaaaaaaaaaaaaaaaaaaa"
PEER = "dev_bbbbbbbbbbbbbbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """No last-good answer leaks between tests; container detection pinned."""
    env_mod._WORKER_LINES_CACHE.clear()
    monkeypatch.setattr(env_mod, "_in_container", lambda: True)
    yield
    env_mod._WORKER_LINES_CACHE.clear()


def _member(device_id, name, kind="worker"):
    return SimpleNamespace(device_id=device_id, name=name, kind=kind)


def _network(monkeypatch, *, self_name="Unraid", peers=(("DesktopWorker", PEER),)):
    """A networked worker: a loaded context plus the member listing."""
    from app.db import network as store
    from app.network import engine

    members = {SELF: _member(SELF, self_name)}
    for name, device_id in peers:
        members[device_id] = _member(device_id, name)
    ctx = SimpleNamespace(
        device_id=SELF,
        member=members.get,
        active_members=lambda: list(members.values()),
    )
    monkeypatch.setattr(engine, "cached_context", lambda: ctx)
    rows = [
        {"device_id": m.device_id, "name": m.name, "kind": m.kind}
        for m in members.values()
    ]
    # A phone/browser client is a member too, but not a worker.
    rows.append({"device_id": "dev_client", "name": "Pixel", "kind": "client"})
    monkeypatch.setattr(store, "list_members", lambda **_kw: list(rows))
    return rows


def _not_networked(monkeypatch, *, install_name=None, install_raises=False):
    from app.network import engine

    monkeypatch.setattr(engine, "cached_context", lambda: None)
    monkeypatch.setattr(engine, "context", lambda **_kw: None)

    def get_install(_id):
        if install_raises:
            raise RuntimeError("installs table unavailable")
        return {"device_name": install_name} if install_name else None

    monkeypatch.setattr(env_mod.database, "get_install", get_install)


def _block(tmp_path, placement_where=None):
    return generate_environment_details(
        str(tmp_path),
        execution_mode="worktree",
        worktree_info={"branch": "claude/x", "base_branch": "main"},
        include_volatile=False,
        placement_where=placement_where,
    )


def test_networked_worker_is_named_with_its_device_and_peers(monkeypatch, tmp_path):
    _network(monkeypatch)
    block = _block(tmp_path)
    assert (
        f"Worker: Unraid (device {SELF}, in a container). Your shell, files and "
        "tools run on this worker."
    ) in block
    assert "Other workers in the owner's network: DesktopWorker." in block
    assert "did not come from Unraid" in block
    # Names only: a client member is not a worker, and no address leaks.
    assert "Pixel" not in block
    assert "http" not in block.split("Worker:", 1)[1]


def test_shared_rename_is_the_name_the_app_shows(monkeypatch, tmp_path):
    rows = _network(monkeypatch, self_name="old-hostname")
    rows[0]["name"] = "Unraid"  # list_members applies the shared rename
    assert "Worker: Unraid (" in _block(tmp_path)


def test_single_worker_network_has_no_peer_line(monkeypatch):
    _network(monkeypatch, peers=())
    lines = worker_identity_lines()
    assert len(lines) == 1 and lines[0].startswith("Worker: Unraid (")


def test_member_listing_failure_falls_back_to_the_loaded_context(monkeypatch):
    from app.db import network as store

    _network(monkeypatch)

    def boom(**_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "list_members", boom)
    lines = worker_identity_lines()
    assert lines[0].startswith("Worker: Unraid (device ")
    assert "DesktopWorker" in lines[1]


def test_non_networked_worker_uses_its_install_name(monkeypatch, tmp_path):
    _not_networked(monkeypatch, install_name="DesktopWorker")
    monkeypatch.setattr(env_mod, "_in_container", lambda: False)
    block = _block(tmp_path)
    instance_id = env_mod.settings.effective_instance_id
    assert f"Worker: DesktopWorker (instance {instance_id}, on the host)." in block
    assert "Other workers" not in block


def test_non_networked_without_install_row_falls_back_to_host_name(monkeypatch):
    _not_networked(monkeypatch, install_raises=True)
    monkeypatch.setattr("app.core.cluster.default_device_name", lambda: "my-box")
    assert worker_identity_lines()[0].startswith("Worker: my-box (instance ")


def test_failed_lookup_renders_the_prompt_without_the_line(monkeypatch, tmp_path):
    from app.network import engine

    def boom(**_kw):
        raise RuntimeError("network store unavailable")

    monkeypatch.setattr(engine, "cached_context", lambda: None)
    monkeypatch.setattr(engine, "context", boom)
    block = _block(tmp_path, "runner:ai-shuffle-4aac1979")
    assert "Worker:" not in block
    # Everything else still renders, placement semantics untouched.
    assert f"Working directory: {tmp_path}" in block
    assert "Runtime placement: runner:ai-shuffle-4aac1979 (shared runner)." in block


def test_failed_lookup_keeps_the_last_good_line(monkeypatch):
    """A transient failure must not blink the line out of a cached prefix."""
    from app.network import engine

    _network(monkeypatch)
    good = worker_identity_lines()

    def boom(**_kw):
        raise RuntimeError("blip")

    monkeypatch.setattr(engine, "cached_context", lambda: None)
    monkeypatch.setattr(engine, "context", boom)
    assert worker_identity_lines() == good


def test_worker_line_sits_in_the_stable_half_and_is_byte_identical(monkeypatch, tmp_path):
    _network(monkeypatch)
    monkeypatch.setattr(env_mod, "_today", lambda: "2026-01-01")
    one = _block(tmp_path, "runner:ai-shuffle-4aac1979")
    monkeypatch.setattr(env_mod, "_today", lambda: "2026-01-02")
    two = _block(tmp_path, "runner:ai-shuffle-4aac1979")
    assert "Worker: Unraid" in one
    assert hashlib.sha256(one.encode()).digest() == hashlib.sha256(two.encode()).digest()
    # After the placement line, inside the <env> block.
    assert one.index("Runtime placement:") < one.index("Worker:") < one.index("</env>")
    # The per-turn note never carries it.
    assert "Worker" not in generate_turn_environment_note("worktree", {"branch": "b"})


def test_moved_session_renders_the_new_worker(monkeypatch, tmp_path):
    """A move rebuilds the prompt in the adopting worker's process, which
    renders its own identity — never the previous worker's."""
    _network(monkeypatch, self_name="Unraid", peers=(("DesktopWorker", PEER),))
    before = _block(tmp_path, "runner:dev_aaaaaaaaaaaaaaaaaaaaaaaa")
    # The adopting worker: DesktopWorker is now "self", Unraid the peer.
    monkeypatch.setattr(env_mod, "_in_container", lambda: False)
    _network(monkeypatch, self_name="DesktopWorker", peers=(("Unraid", PEER),))
    after = _block(tmp_path, "runner:dev_aaaaaaaaaaaaaaaaaaaaaaaa")
    assert "Worker: Unraid (" in before
    assert "Worker: DesktopWorker (" in after and "on the host" in after
    assert "Other workers in the owner's network: Unraid." in after


# ─── the harness adapter: native compile, delegated legs ─────────────────────

WORKER = (
    f"Worker: Unraid (device {SELF}, in a container). Your shell, files and "
    "tools run on this worker."
)


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    from app.core import guidance_surface

    monkeypatch.setenv(guidance_surface.GUIDANCE_HOME_ENV, str(home))
    return home


def test_native_compile_puts_the_worker_in_fluids_environment_section(isolated_home, tmp_path):
    options = SessionOptions(model="fake", cwd=str(tmp_path))
    compiled = ha.HarnessAdapter._compile_prompt_sections(
        "AGENT", None, options, native=True, worker=WORKER
    )
    env_sections = [s for s in compiled.sections if s.text.startswith("# Environment")]
    assert len(env_sections) == 1 and WORKER in env_sections[0].text
    assert compiled.text.count("Worker: Unraid") == 1


def test_native_compile_with_app_env_block_does_not_double_the_line(isolated_home, tmp_path):
    system = f"<env>\nWorking directory: {tmp_path}\n{WORKER}\n</env>"
    options = SessionOptions(model="fake", cwd=str(tmp_path))
    compiled = ha.HarnessAdapter._compile_prompt_sections(
        system, None, options, native=True, worker=WORKER
    )
    assert compiled.text.count("Worker: Unraid") == 1


def test_delegated_full_replace_compile_carries_the_worker(isolated_home, tmp_path):
    options = SessionOptions(model="fake", cwd=str(tmp_path))
    text = ha.HarnessAdapter._compile_prompt("AGENT", None, options, WORKER)
    assert WORKER in text


def test_delegated_preset_append_carries_a_worker_block(isolated_home, tmp_path):
    options = SessionOptions(
        model="fable",
        system_prompt={"type": "preset", "preset": "claude_code", "append": "APPEND TEXT"},
        cwd=str(tmp_path),
        extra={"fluid_registry": ToolRegistry()},
    )
    text = ha._delegated_append_text("APPEND TEXT", options, ToolRegistry(), worker=WORKER)
    assert text.startswith("APPEND TEXT\n\n# Worker\n" + WORKER)
    assert "# Worker" not in (
        ha._delegated_append_text("APPEND TEXT", options, ToolRegistry()) or ""
    )


def test_adapter_worker_lookup_failure_never_fails_connect(monkeypatch):
    def boom():
        raise RuntimeError("identity exploded")

    monkeypatch.setattr(env_mod, "worker_identity_text", boom)
    assert ha._worker_identity_text() is None


FAKE = Path(__file__).resolve().parents[2] / "fluid" / "tests" / "fixtures" / "fake_claude.py"


@pytest.mark.asyncio
async def test_delegated_connect_sends_this_workers_name_and_a_move_sends_the_new_one(
    monkeypatch, isolated_home, tmp_path
):
    """End to end through connect on the delegated (Claude CLI) leg: the
    appended prompt the CLI receives names the worker, identical across a CLI
    restart, and a reconnect on another worker (a move) names that one."""
    import fluid.engine.delegated as delegated_mod
    from fluid.engine.delegated import DelegatedLoop

    from app.core.provider.events import ResultMessage

    class _FileLoop(DelegatedLoop):
        prompts: ClassVar[list] = []

        def _build_argv(self):
            argv = super()._build_argv()
            flag = "--append-system-prompt-file"
            path = argv[argv.index(flag) + 1] if flag in argv else None
            type(self).prompts.append(Path(path).read_text() if path else None)
            return argv

    monkeypatch.setattr(delegated_mod, "_CLI_FLAGS_SINCE", "2.0.0")
    monkeypatch.setattr(ha, "DelegatedLoop", _FileLoop)
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: [sys.executable, str(FAKE)])
    _FileLoop.prompts = []

    def options():
        return SessionOptions(
            model="fable",
            system_prompt={"type": "preset", "preset": "claude_code", "append": "APPEND TEXT"},
            cwd=str(tmp_path),
            extra={"fluid_registry": ToolRegistry()},
        )

    async def turn(adapter, text):
        await adapter.send_query(text)
        async for message in adapter.receive_events():
            if isinstance(message, ResultMessage):
                return message
        raise AssertionError("turn ended without a result")

    monkeypatch.setattr(ha, "_worker_identity_text", lambda: WORKER)
    adapter = ha.HarnessAdapter()
    await adapter.connect(options())
    try:
        assert not (await turn(adapter, "first")).is_error
        await adapter.engine._stop_process()
        assert not (await turn(adapter, "second")).is_error
    finally:
        await adapter.disconnect()

    moved = "Worker: DesktopWorker (device dev_bbbbbbbbbbbbbbbbbbbbbbbb, on the host)."
    monkeypatch.setattr(ha, "_worker_identity_text", lambda: moved)
    again = ha.HarnessAdapter()
    await again.connect(options())
    try:
        assert not (await turn(again, "after the move")).is_error
    finally:
        await again.disconnect()

    first, second, after_move = _FileLoop.prompts
    assert "# Worker\n" + WORKER in first
    assert first == second
    assert moved in after_move and "Worker: Unraid" not in after_move
