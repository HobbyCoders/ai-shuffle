"""Where the chat lane's read fence is actually ENFORCED.

``tests/core/test_session_modes.py`` tests the predicate. A predicate nobody
calls is not a boundary, and that was the real defect this file exists to catch:
neither runtime consults the app's ``can_use_tool`` callback for a call it has
already decided to allow, so a fence living only there would have been dead code
that passed its own unit tests.

* fluid's ``PermissionEngine`` auto-allows a ``risk="safe"`` tool under the
  ``on_write`` policy a chat session runs on, emitting no permission event at
  all — so the native leg needs a RULE, which is checked before the policy.
* the pinned claude CLI auto-approves a BARE name in ``--allowed-tools``, and
  separately auto-approves reads inside its working directory. Its builtin Read
  must be disabled and replaced with the same handle-confined broker used by
  native execution; permission matching alone cannot close the race.

Every test here therefore asserts against the object the runtime will actually
consult, not against our own helper's return value.
"""

from unittest.mock import patch

import pytest

from app.core.provider.harness_adapter import (
    HarnessAdapter,
    _chat_mode_rules,
    _delegated_allowed,
    _delegated_disallowed,
    _session_mode,
    _tool_allowlist,
)
from app.core.provider.types import SessionOptions
from app.core.session_modes import (
    CHAT_MODE_DENIED_TOOLS,
    CHAT_MODE_TOOL_SURFACE,
    chat_uploads_dir,
)

CHAT_SURFACE = list(CHAT_MODE_TOOL_SURFACE)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Point the workspace root at ``tmp_path`` and return the uploads dir."""
    import app.core.config as config

    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    uploads = chat_uploads_dir()
    uploads.mkdir(parents=True, exist_ok=True)
    return uploads


def _options(mode, **kw):
    opts = SessionOptions(model="gpt-5", tool_allowlist=list(CHAT_SURFACE), **kw)
    opts.extra["session_mode"] = mode
    return opts


def _read_spec():
    """fluid's real ``Read`` ToolSpec — the object the engine is handed."""
    import fluid.tools.builtins.read as read_module

    for value in vars(read_module).values():
        if getattr(value, "name", None) == "Read":
            return value
    raise AssertionError("fluid no longer exposes a Read ToolSpec")


# =============================================================================
# Native leg: the permission engine the adapter hands to NativeLoop
# =============================================================================

def _engine_for(mode, permission_mode="default"):
    engine = HarnessAdapter._build_permissions(
        HarnessAdapter(), _options(mode, permission_mode=permission_mode)
    )
    return engine


def test_the_policy_alone_would_have_allowed_everything(workspace):
    """The bug this fence exists for, pinned so it cannot come back quietly.

    Without a rule, fluid's engine allows a chat session's Read of ANY path and
    emits no permission event — so nothing downstream, callback included, is
    ever asked. If this test starts failing because fluid changed Read's risk or
    its policy table, the rule below may no longer be load-bearing; check before
    deleting anything."""
    from fluid.permissions.engine import PermissionEngine

    bare = PermissionEngine(approval_policy="on_write")
    verdict = bare.decide(_read_spec(), {"file_path": "C:/secrets/id_rsa"}, cwd=str(workspace))
    assert verdict.decision == "allow"


def test_chat_read_outside_the_uploads_dir_is_denied_by_the_engine(workspace, tmp_path):
    engine = _engine_for("chat")
    for escape in (
        str(tmp_path / "secret.env"),
        str(workspace.parent / f"{workspace.name}-evil" / "loot.txt"),
        str(workspace / ".." / "secret.env"),
        "/etc/passwd",
    ):
        verdict = engine.decide(_read_spec(), {"file_path": escape}, cwd=str(workspace))
        assert verdict.decision == "deny", escape


def test_chat_read_of_an_attachment_is_allowed_without_a_prompt(workspace):
    """The lane has to be able to do the one thing it was widened for, and do it
    silently — a prompt to read the file the user just attached to this chat is
    a question with only one answer."""
    engine = _engine_for("chat")
    for spelling in (
        str(workspace / "notes.txt"),
        str(workspace / "nested" / "notes.txt"),
        "notes.txt",  # relative to the session cwd, which IS the uploads dir
    ):
        verdict = engine.decide(_read_spec(), {"file_path": spelling}, cwd=str(workspace))
        assert verdict.decision == "allow", spelling


def test_a_read_with_no_path_is_denied(workspace):
    """A call the rule's path glob cannot evaluate falls to the bare deny."""
    engine = _engine_for("chat")
    assert engine.decide(_read_spec(), {}, cwd=str(workspace)).decision == "deny"


@pytest.mark.parametrize("mode", ["code", "voice"])
def test_other_lanes_get_no_read_rules(workspace, mode):
    assert _chat_mode_rules(_options(mode)) is None
    engine = _engine_for(mode)
    verdict = engine.decide(_read_spec(), {"file_path": "/etc/passwd"}, cwd=str(workspace))
    assert verdict.decision == "allow"


def test_the_pinned_permission_mode_does_not_dissolve_the_rules(workspace):
    """Rules beat the policy axis in both directions. Even if a chat session
    somehow arrived on ``bypassPermissions`` (``never_ask``), the fence holds."""
    engine = _engine_for("chat", permission_mode="bypassPermissions")
    verdict = engine.decide(_read_spec(), {"file_path": "/etc/passwd"}, cwd=str(workspace))
    assert verdict.decision == "deny"


@pytest.mark.asyncio
@pytest.mark.parametrize("link_kind", ["file", "directory"])
async def test_native_read_execution_rejects_outside_symlink(workspace, tmp_path, link_kind):
    """Real Read through NativeLoop's permission AND dispatch path, no provider IO."""
    from types import SimpleNamespace
    from fluid.engine.native import NativeLoop
    from fluid.providers.base import ProviderCaps
    from fluid.store.base import MemoryStore
    from fluid.tools.registry import ToolCtx, ToolRegistry

    inside = workspace / "inside.txt"
    inside.write_text("permitted attachment marker")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside file marker must not be returned")
    try:
        if link_kind == "file":
            (workspace / "escape").symlink_to(outside)
            (workspace / "alias").symlink_to(inside)
            escape, alias = "escape", "alias"
        else:
            (workspace / "escape").symlink_to(tmp_path, target_is_directory=True)
            (workspace / "alias").symlink_to(workspace, target_is_directory=True)
            escape, alias = "escape/outside.txt", "alias/inside.txt"
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    registry = await HarnessAdapter()._build_registry(_options("chat"), native=True)
    loop = NativeLoop(provider=SimpleNamespace(name="test", caps=ProviderCaps()),
                      store=MemoryStore(), model="test", registry=registry,
                      permissions=_engine_for("chat"), ctx=ToolCtx(cwd=str(workspace)),
                      allowed_tools=CHAT_SURFACE)
    for index, spelling in enumerate([str(inside), "inside.txt", alias, escape]):
        outcome = await loop._gated_call("test-session", "test-turn", "Read", str(index), {"file_path": spelling})
        if spelling == escape:
            assert outcome.is_error
            assert "outside file marker" not in outcome.text
        else:
            assert not outcome.is_error
            assert "permitted attachment marker" in outcome.text




@pytest.mark.asyncio
@pytest.mark.parametrize("native", [True, False])
async def test_uploaded_attachment_and_hardlink_through_actual_registry(workspace, tmp_path, native):
    import os
    from types import SimpleNamespace
    from app.core.confined_files import upload
    from app.core.attachment_read import DELEGATED_READ
    from fluid.engine.native import NativeLoop
    from fluid.engine.delegated import DelegatedLoop
    from fluid.providers.base import ProviderCaps
    from fluid.store.base import MemoryStore
    from fluid.tools.registry import ToolCtx

    attachment = upload(workspace.parent, workspace.name, "notes.txt", b"uploaded marker")
    outside = tmp_path / "private.txt"
    outside.write_text("outside marker")
    os.link(outside, workspace / "hard.txt")
    options = _options("chat")
    registry = await HarnessAdapter()._build_registry(options, native=native)
    ctx = ToolCtx(cwd=str(workspace))
    if native:
        loop = NativeLoop(provider=SimpleNamespace(name="test", caps=ProviderCaps()),
            store=MemoryStore(), model="test", registry=registry,
            permissions=_engine_for("chat"), ctx=ctx, allowed_tools=_tool_allowlist(options))
        async def call(path):
            return await loop._gated_call("session", "turn", "Read", path.name, {"file_path": str(path)})
    else:
        loop = DelegatedLoop(store=MemoryStore(), model="test", registry=registry,
            allowed_tools=_delegated_allowed(options), disallowed_tools=_delegated_disallowed(options),
            cwd=str(workspace))
        assert not loop._bridge_tool_permitted("Read")
        assert loop._bridge_tool_permitted(DELEGATED_READ)
        assert registry.get("Read") is None
        async def call(path):
            return await registry.dispatch(DELEGATED_READ, {"file_path": str(path)}, ctx)
    good = await call(attachment)
    assert not good.is_error
    assert "uploaded marker" in good.text
    for path in (outside, workspace / "hard.txt"):
        denied = await call(path)
        assert denied.is_error
        assert "outside marker" not in denied.text


@pytest.mark.asyncio
async def test_uploaded_image_reaches_provider_with_bounded_pixels(workspace):
    """Exercise Chat's real upload and broker, not just Fluid's builtin Read."""
    import base64
    import io
    from types import SimpleNamespace

    from PIL import Image as PILImage
    from app.core.confined_files import upload
    from fluid.engine.native import NativeLoop
    from fluid.events import ToolResult
    from fluid.messages import Message
    from fluid.providers.base import ProviderCaps, ProviderRequest
    from fluid.providers.openai_responses.serialize import build_request_body
    from fluid.store.base import MemoryStore
    from fluid.tools.images import MAX_IMAGE_EDGE
    from fluid.tools.registry import ToolCtx

    buffer = io.BytesIO()
    with PILImage.new("RGB", (6240, 6240), "white") as image:
        image.save(buffer, format="WEBP", lossless=True)
    original = buffer.getvalue()
    attachment = upload(workspace.parent, workspace.name, "large.webp", original)
    registry = await HarnessAdapter()._build_registry(_options("chat"), native=True)
    loop = NativeLoop(
        provider=SimpleNamespace(name="test", caps=ProviderCaps()),
        store=MemoryStore(), model="test", registry=registry,
        permissions=_engine_for("chat"), ctx=ToolCtx(cwd=str(workspace)),
        allowed_tools=CHAT_SURFACE,
    )
    try:
        result = await loop._dispatch_tool("session", "turn", {
            "tool_use_id": "read-image", "name": "Read",
            "input": {"file_path": str(attachment)},
        })
        assert not result.is_error
        body = build_request_body(ProviderRequest(
            model="test", messages=[Message(role="user", content=[result])],
        ))
        [image_part] = [part for part in body["input"][0]["output"]
                        if part["type"] == "input_image"]
        data = image_part["image_url"].split(",", 1)[1]
        with PILImage.open(io.BytesIO(base64.b64decode(data))) as decoded:
            assert decoded.size == (MAX_IMAGE_EDGE, MAX_IMAGE_EDGE)
        # Display/event history and the downloadable upload retain the original.
        [event] = [e for e in loop.bus.log("session") if isinstance(e, ToolResult)]
        assert base64.b64decode(event.images[0]["data"]) == original
        assert attachment.read_bytes() == original
    finally:
        await loop.aclose()


# =============================================================================
# Delegated leg: what the claude CLI is handed on its command line
# =============================================================================

def test_read_is_withheld_from_the_cli_grant_for_chat(workspace):
    """A bare name in ``--allowed-tools`` is a blanket pre-approval — the CLI
    says so itself. Granting Read there would retire the fence."""
    opts = _options("chat")
    assert set(_delegated_allowed(opts)) == set(CHAT_MODE_TOOL_SURFACE) - {"Read"}
    assert "Read" not in _delegated_allowed(opts)
    assert "Read" in (_tool_allowlist(opts) or [])


def test_cli_builtin_read_is_removed_but_broker_stays_callable(workspace):
    """The CLI must not auto-approve its own unsafe cwd reader."""
    opts = _options("chat")
    with patch(
        "app.core.cli_tool_surface.cached_cli_tools",
        return_value=["Read", "Bash", "Grep", "WebSearch", "WebFetch"],
    ):
        banned = _delegated_disallowed(opts)
    assert "Read" in banned
    assert "mcp__attachments__Read" not in banned
    assert "mcp__attachments__Read" in _delegated_allowed(opts)
    assert "Bash" in banned and "Grep" in banned


def test_code_sessions_keep_the_whole_grant(workspace):
    opts = _options("code")
    assert _delegated_allowed(opts) == _tool_allowlist(opts)


def test_read_only_surface_grants_only_the_broker(workspace):
    opts = _options("chat")
    opts.tool_allowlist = ["Read"]
    assert _delegated_allowed(opts) == ["mcp__attachments__Read"]


@pytest.mark.parametrize(
    "extra,expected",
    [({}, "code"), ({"session_mode": "chat"}, "chat"), ({"session_mode": ""}, "code")],
)
def test_session_mode_defaults_to_code(extra, expected):
    opts = SessionOptions(model="x")
    opts.extra.update(extra)
    assert _session_mode(opts) == expected


# =============================================================================
# The code-mode kernel the allow list cannot take away
# =============================================================================

def test_the_kernel_names_are_denied_outright(workspace):
    """fluid exempts its code-mode surface from the CE12 allowlist, so a chat
    session's narrower surface leaves ``run_python`` callable. Only a deny gets
    it back — and the adapter separately declines to build the kernel at all."""
    assert "run_python" in CHAT_MODE_DENIED_TOOLS
    from fluid.engine.native import NativeLoop

    loop = NativeLoop.__new__(NativeLoop)
    loop._codemode = object()
    loop.registry = None  # short-circuits the exemption branch's registry lookup
    loop.allowed_tools = CHAT_SURFACE
    loop.disallowed_tools = list(CHAT_MODE_DENIED_TOOLS)
    assert loop._tool_permitted("run_python") is False
    assert loop._tool_permitted("Read") is True
