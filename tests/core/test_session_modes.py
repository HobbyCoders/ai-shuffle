"""Session modes and the tool ceiling chat mode imposes.

The ceiling has two halves and both are tested here. The NAME half is
``CHAT_MODE_TOOL_SURFACE``: whatever the agent's own ``tool_selection_mode``
says, a chat session reaches web tools, attachment ``Read`` and account memory —
and when the agent already denies all of those, the answer is a ``["*"]`` deny,
never an empty allow list (ruling T2). The PATH half is
``chat_mode_read_permitted``, which fences that ``Read`` to the chat lane's
uploads directory; it is the security boundary of the attachment feature, so its
tests are written as attempts to get out of that directory.
"""

from pathlib import Path

import pytest

from app.core.session_modes import (
    CHAT_MODE_DENIED_TOOLS,
    CHAT_MODE_TOOL_SURFACE,
    CHAT_UPLOADS_DIRNAME,
    DEFAULT_SESSION_MODE,
    SESSION_MODES,
    chat_mode_read_glob,
    chat_mode_read_permitted,
    chat_uploads_dir,
    ensure_chat_uploads_dir,
    narrow_tool_access_for_mode,
    normalize_session_mode,
    permission_mode_for_session_mode,
    session_mode_of,
    storage_session_mode,
)
from app.core.tool_access import DENY_ALL, ToolAccess, resolve_tool_access, tool_permitted


# =============================================================================
# Normalization
# =============================================================================

@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "code"),
        ("", "code"),
        ("   ", "code"),
        ("code", "code"),
        ("chat", "chat"),
        ("voice", "voice"),
        (" chat ", "chat"),
        ("CHAT", "chat"),
        ("Voice", "voice"),
        ("bogus", "code"),
        # Non-strings can arrive from a hand-built row dict or a WS frame.
        (0, "code"),
        (["chat"], "code"),
    ],
)
def test_normalize_session_mode(value, expected):
    assert normalize_session_mode(value) == expected


def test_mode_constants():
    assert SESSION_MODES == ("code", "chat", "voice")
    assert DEFAULT_SESSION_MODE == "code"


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("code", None),
        (None, None),
        ("bogus", None),
        ("chat", "chat"),
        ("voice", "voice"),
    ],
)
def test_storage_session_mode(mode, expected):
    """'code' is the NULL case — a new code session is byte-identical to every
    row written before this column meant anything."""
    assert storage_session_mode(mode) == expected


@pytest.mark.parametrize(
    "row,expected",
    [
        (None, "code"),
        ({}, "code"),
        ({"mode": None}, "code"),
        ({"mode": ""}, "code"),
        ({"mode": "chat"}, "chat"),
        ({"mode": "voice"}, "voice"),
        ({"mode": "bogus"}, "code"),
    ],
)
def test_session_mode_of(row, expected):
    assert session_mode_of(row) == expected


# =============================================================================
# The chat ceiling
# =============================================================================

@pytest.mark.parametrize("mode", ["code", "voice", None, "bogus"])
def test_non_chat_modes_are_untouched(mode):
    """Only chat has a ceiling. A voice session dispatches worker agents, so
    clipping its surface would break the thing a voice call is for."""
    access = ToolAccess(allowed=["Read", "Bash"], disallowed=["WebFetch"])
    narrowed = narrow_tool_access_for_mode(access, mode)
    assert narrowed is access
    assert narrowed.allowed == ["Read", "Bash"]
    assert narrowed.disallowed == ["WebFetch"]


@pytest.mark.parametrize(
    "config,expected_allowed,web_search_ok,web_fetch_ok,read_ok",
    [
        # "all" — unrestricted agent, so the mode surface IS the surface.
        (
            {"tool_selection_mode": "all"},
            CHAT_MODE_TOOL_SURFACE,
            True,
            True,
            True,
        ),
        # "none" — the agent denies everything; the deny survives the narrowing
        # and still beats the allow surface.
        (
            {"tool_selection_mode": "none"},
            CHAT_MODE_TOOL_SURFACE,
            False,
            False,
            False,
        ),
        # "allow" naming one web tool — the intersection is that tool plus the
        # reader the agent also named.
        (
            {"tool_selection_mode": "allow",
             "allowed_tools": ["Read", "Bash", "WebSearch"]},
            ["Read", "WebSearch"],
            True,
            False,
            True,
        ),
        # "disallow" listing a web tool — the other one survives.
        (
            {"tool_selection_mode": "disallow", "disallowed_tools": ["WebFetch"]},
            CHAT_MODE_TOOL_SURFACE,
            True,
            False,
            True,
        ),
        # An agent that bars the reader keeps only the web half — the mode
        # WIDENS nothing, it only ever narrows (ruling T3).
        (
            {"tool_selection_mode": "disallow", "disallowed_tools": ["Read"]},
            CHAT_MODE_TOOL_SURFACE,
            True,
            True,
            False,
        ),
    ],
)
def test_chat_mode_surface(
    config, expected_allowed, web_search_ok, web_fetch_ok, read_ok
):
    access = narrow_tool_access_for_mode(resolve_tool_access(config), "chat")
    assert access.allowed == expected_allowed
    # Whatever the agent said, nothing that WRITES or RUNS survives. Read does,
    # fenced to the uploads directory by chat_mode_read_permitted rather than by
    # the name list.
    assert tool_permitted("Bash", access.allowed, access.disallowed) is False
    assert tool_permitted("Edit", access.allowed, access.disallowed) is False
    assert tool_permitted("Task", access.allowed, access.disallowed) is False
    assert tool_permitted("Read", access.allowed, access.disallowed) is read_ok
    assert (
        tool_permitted("WebSearch", access.allowed, access.disallowed) is web_search_ok
    )
    assert (
        tool_permitted("WebFetch", access.allowed, access.disallowed) is web_fetch_ok
    )


def test_chat_mode_allow_list_without_the_surface_denies_all():
    """Regression (ruling T2). An agent whose allow list excludes every tool on
    the chat surface intersects to nothing — which must be spelled as a ``["*"]``
    deny, because an empty allow list reads as *unrestricted* on the runtime
    legs."""
    access = narrow_tool_access_for_mode(
        resolve_tool_access(
            {"tool_selection_mode": "allow", "allowed_tools": ["Edit", "Bash"]}
        ),
        "chat",
    )
    assert access.allowed is None
    assert access.disallowed is not None and DENY_ALL in access.disallowed
    for name in ("Bash", "Read", "WebSearch", "WebFetch", "mcp__builtins__Monitor"):
        assert tool_permitted(name, access.allowed, access.disallowed) is False


def test_chat_mode_preserves_the_agents_own_denies():
    """The ceiling composes with the agent's blocklist rather than replacing it —
    deny still beats allow after the narrowing."""
    access = narrow_tool_access_for_mode(
        ToolAccess(allowed=None, disallowed=["WebFetch", "mcp__browser__*"]), "chat"
    )
    assert access.allowed == list(CHAT_MODE_TOOL_SURFACE)
    assert access.disallowed[:2] == ["WebFetch", "mcp__browser__*"]
    assert tool_permitted("WebSearch", access.allowed, access.disallowed) is True
    assert tool_permitted("WebFetch", access.allowed, access.disallowed) is False
    assert (
        tool_permitted("mcp__browser__navigate", access.allowed, access.disallowed)
        is False
    )


def test_chat_mode_keeps_the_host_tools_off_the_surface():
    """The names that matter most: whatever the agent hands the ceiling, the
    tools that write, run or dispatch never reach a chat session — while Read
    does."""
    access = narrow_tool_access_for_mode(ToolAccess(allowed=None, disallowed=None), "chat")
    assert "Read" in (access.allowed or [])
    for name in ("Bash", "Task", "Edit", "Write", "Glob", "Grep", "mcp__builtins__Monitor"):
        assert name not in (access.allowed or [])
        assert tool_permitted(name, access.allowed, access.disallowed) is False


def test_chat_mode_denies_the_code_mode_kernel_by_name():
    """The one family an allow list cannot remove.

    fluid exempts its code-mode surface from the CE12 allowlist
    (``NativeLoop._tool_permitted`` short-circuits on ``source ==
    CODE_MODE_SOURCE``), so naming a narrower surface leaves ``run_python``
    callable — a chat session would be handed a Python kernel. Only a DENY
    survives that exemption, so the ceiling has to emit one.
    """
    access = narrow_tool_access_for_mode(ToolAccess(allowed=None, disallowed=None), "chat")
    for name in CHAT_MODE_DENIED_TOOLS:
        assert name in (access.disallowed or [])
        assert tool_permitted(name, access.allowed, access.disallowed) is False
    assert "run_python" in CHAT_MODE_DENIED_TOOLS


@pytest.mark.parametrize("mode", ["code", "voice", None])
def test_the_code_mode_deny_is_chat_only(mode):
    """A code session keeps its kernel — this deny is a lane ceiling, not policy."""
    access = narrow_tool_access_for_mode(ToolAccess(allowed=None, disallowed=None), mode)
    assert "run_python" not in (access.disallowed or [])


def test_chat_mode_drops_mcp_group_wildcards():
    """An agent allowed a whole MCP group keeps none of it in chat mode."""
    access = narrow_tool_access_for_mode(
        ToolAccess(allowed=["mcp__browser__*", "WebFetch"], disallowed=None), "chat"
    )
    assert access.allowed == ["WebFetch"]
    assert (
        tool_permitted("mcp__browser__navigate", access.allowed, access.disallowed)
        is False
    )


# =============================================================================
# The ceiling as the options builder actually applies it
# =============================================================================

def _build_for_mode(mode, config, **row_extra):
    """``build_options_from_profile`` for a session row in ``mode``.

    The session_id branch is the only one that reads a row, so the cwd seams it
    walks on the way there are stubbed to the project-less answer (the workspace
    dir) — none of them is what this test is about. ``row_extra`` puts further
    columns (the session's own permission-mode binding) on that row.
    """
    from unittest.mock import patch

    import app.core.query_options as qo

    row = {"id": "s-mode", "mode": mode, **row_extra}
    with patch.object(qo, "get_security_instructions", return_value=""), patch.object(
        qo.database, "get_session", return_value=row
    ), patch.object(qo, "_rescue_pending_checkout", return_value=False), patch(
        "app.core.project_resolver.resolve_cwd", return_value=None
    ), patch(
        "app.core.project_resolver.resolve_project_v2", return_value=None
    ):
        options, _ = qo.build_options_from_profile(
            profile={"id": "t", "config": config}, session_id="s-mode"
        )
    return options


def test_build_options_chat_mode_keeps_only_the_chat_surface():
    """The end-to-end surface, not just the ``ToolAccess`` maths: everything the
    builder appends unconditionally (builtin bridge tools, ``mcp__<server>``
    wildcards) has to be gone too."""
    options = _build_for_mode("chat", {"model": "opus"})
    assert set(options.allowed_tools) == set(CHAT_MODE_TOOL_SURFACE)
    assert options.tools is None


def test_build_options_chat_mode_ignores_a_tools_preset():
    """A ``claude_code`` preset hands the leg its whole built-in toolset without
    consulting ``allowed_tools``, so chat mode has to drop the preset outright —
    and still keep its own narrow surface rather than ending up with nothing."""
    options = _build_for_mode("chat", {"model": "opus", "tools_preset": "claude_code"})
    assert options.tools is None
    assert set(options.allowed_tools) == set(CHAT_MODE_TOOL_SURFACE)


def test_build_options_chat_mode_denies_all_when_the_agent_bars_the_surface():
    options = _build_for_mode(
        "chat",
        {"model": "opus", "tool_selection_mode": "allow", "allowed_tools": ["Bash"]},
    )
    assert options.allowed_tools == []
    assert DENY_ALL in (options.disallowed_tools or [])


def test_build_options_chat_mode_runs_inside_the_uploads_dir(tmp_path, monkeypatch):
    """The cwd is half of the read fence, not a tidiness choice.

    The delegated CLI auto-approves any read that resolves inside the session's
    working directory, before the permission-prompt tool and so before anything
    the app could say. Left on the workspace root — the parent of every project
    checkout on this machine — that shortcut hands a projectless chat every
    ``.env`` under it. ``add_dirs`` is emptied for the same reason: each entry
    is another folder that shortcut would cover.
    """
    import app.core.config as config

    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    options = _build_for_mode("chat", {"model": "opus"})
    assert options.cwd == str(chat_uploads_dir())
    assert Path(options.cwd).is_dir()
    assert not (options.add_dirs or [])
    assert options.aih_session_mode == "chat"


def test_build_options_code_mode_keeps_its_own_cwd_and_add_dirs(tmp_path, monkeypatch):
    """The lane guard on both of the above: a code session is untouched."""
    import app.core.config as config

    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    options = _build_for_mode(None, {"model": "opus"})
    assert options.cwd != str(chat_uploads_dir())
    assert options.add_dirs, "the notes dir registration is code-lane behaviour"
    assert options.aih_session_mode == "code"


def test_build_options_code_mode_is_untouched():
    """The regression guard for every session that exists today: a NULL-mode row
    still gets the full surface."""
    options = _build_for_mode(None, {"model": "opus", "tools_preset": "claude_code"})
    assert options.tools == {"type": "preset", "preset": "claude_code"}
    assert len(options.allowed_tools) > 2
    assert "mcp__builtins__Monitor" in options.allowed_tools


# =============================================================================
# The permission mode a chat session is pinned to
# =============================================================================

@pytest.mark.parametrize("mode", ["code", "voice", None, "", "nonsense"])
@pytest.mark.parametrize("permission_mode", ["default", "plan", "bypassPermissions", None])
def test_permission_mode_untouched_outside_chat(mode, permission_mode):
    expected = "default" if permission_mode == "plan" else permission_mode
    assert permission_mode_for_session_mode(permission_mode, mode) == expected


@pytest.mark.parametrize("permission_mode", ["default", "plan", "bypassPermissions", "acceptEdits", None])
def test_chat_mode_pins_the_permission_mode(permission_mode):
    """Whatever the row/config/override chain produced, a chat turn runs on
    ``default``: its composer has no control to undo an inherited ``plan``
    (which the ceiling would deadlock, the plan-exit tool not being on it) and
    ``bypassPermissions`` would switch off the callback the ceiling leans on."""
    assert permission_mode_for_session_mode(permission_mode, "chat") == "default"


def test_build_options_chat_mode_overrules_an_inherited_plan_mode():
    """End-to-end: a chat row carrying ``plan`` (inherited from the code lane
    before the composer stopped passing one) still spawns on ``default``."""
    options = _build_for_mode("chat", {"model": "opus"}, permission_mode="plan")
    assert options.permission_mode == "default"


def test_build_options_chat_mode_overrules_an_inherited_bypass():
    options = _build_for_mode(
        "chat", {"model": "opus"}, permission_mode="bypassPermissions"
    )
    assert options.permission_mode == "default"


def test_build_options_code_mode_normalizes_retired_permission_binding():
    options = _build_for_mode(None, {"model": "opus"}, permission_mode="plan")
    assert options.permission_mode == "default"


# =============================================================================
# The PATH half of the ceiling: where a chat session's Read may point
# =============================================================================
# ``Read`` is on the chat surface so the model can open the files the user
# attached. These tests are written from the attacker's side: everything below
# is a way of spelling "somewhere else on this disk", and each one has to come
# back False.

@pytest.fixture
def chat_uploads(tmp_path, monkeypatch):
    """Point the workspace root at ``tmp_path`` and hand back the uploads dir.

    The runtime override is the same seam the local-mode setup writes, so this
    exercises ``chat_uploads_dir`` exactly as production resolves it. Resolved
    on the way out because macOS hands pytest a ``/private`` symlink for
    ``tmp_path`` and the boundary check compares resolved paths.
    """
    import app.core.config as config

    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    uploads = chat_uploads_dir()
    uploads.mkdir(parents=True, exist_ok=True)
    return uploads


def _read(path):
    """A ``Read`` call on ``path``, in the tool's own argument spelling."""
    return chat_mode_read_permitted("Read", {"file_path": str(path)})


def test_chat_uploads_dir_is_under_the_workspace(chat_uploads, tmp_path):
    assert chat_uploads.is_absolute()
    assert chat_uploads.name == CHAT_UPLOADS_DIRNAME
    assert chat_uploads.parent == tmp_path.resolve()


def test_read_inside_the_uploads_dir_is_allowed(chat_uploads):
    """Both spellings the composer can produce: the workspace-relative path the
    upload endpoint returns, and the absolute one it puts in the ``@ref``."""
    (chat_uploads / "note.png").write_bytes(b"x")
    assert _read(f"{CHAT_UPLOADS_DIRNAME}/note.png") is True
    assert _read(chat_uploads / "note.png") is True
    # as_posix() form, which is what the workspace upload response hands back.
    assert _read((chat_uploads / "note.png").as_posix()) is True


def test_read_in_a_subdirectory_of_uploads_is_allowed(chat_uploads):
    nested = chat_uploads / "2026-09" / "shot.png"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"x")
    assert _read(nested) is True


def test_sibling_directory_sharing_the_prefix_is_denied(chat_uploads, tmp_path):
    """The reason the check is ``is_relative_to`` and not ``startswith``:
    ``chat-uploads-evil`` passes a string prefix test."""
    evil = tmp_path / f"{CHAT_UPLOADS_DIRNAME}-evil"
    evil.mkdir()
    (evil / "loot.txt").write_bytes(b"x")
    assert _read(evil / "loot.txt") is False
    assert _read(f"{CHAT_UPLOADS_DIRNAME}-evil/loot.txt") is False


def test_dot_dot_traversal_is_denied(chat_uploads, tmp_path):
    (tmp_path / "secret.txt").write_bytes(b"x")
    assert _read(f"{CHAT_UPLOADS_DIRNAME}/../secret.txt") is False
    assert _read("../../etc/passwd") is False
    assert _read(chat_uploads / ".." / "secret.txt") is False


def test_absolute_path_elsewhere_is_denied(chat_uploads, tmp_path):
    outside = tmp_path.parent / "outside.txt"
    assert _read(outside) is False
    # The workspace root itself is not the uploads dir.
    assert _read(tmp_path / "anything.txt") is False


def test_symlink_out_of_the_uploads_dir_is_denied(chat_uploads, tmp_path):
    """A file the user could plant INSIDE the folder that points outside it.
    ``resolve()`` follows it before the containment test, so the answer is the
    target's, not the link's."""
    target = tmp_path / "outside-target.txt"
    target.write_bytes(b"x")
    link = chat_uploads / "innocent.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("this platform will not create a symlink unprivileged")
    assert _read(link) is False


def test_symlink_within_the_uploads_dir_is_allowed(chat_uploads):
    target = chat_uploads / "real.png"
    target.write_bytes(b"x")
    link = chat_uploads / "alias.png"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("this platform will not create a symlink unprivileged")
    assert _read(link) is True


@pytest.mark.parametrize(
    "tool_input",
    [
        {},                       # no path key at all
        {"file_path": ""},        # blank
        {"file_path": "   "},     # whitespace only
        {"file_path": None},      # explicit null
        {"file_path": 42},        # non-string
        {"file_path": ["/x"]},    # non-string, list
        {"offset": 1},            # a read call with the wrong keys
        None,                     # not a mapping at all
        "file_path=/etc/passwd",  # not a mapping at all
        [],                       # not a mapping at all
    ],
)
def test_unreadable_read_arguments_are_denied(chat_uploads, tool_input):
    """A read the fence cannot evaluate is denied, never waved through — the
    failure mode of the opposite choice is 'malformed input reads any file'."""
    assert chat_mode_read_permitted("Read", tool_input) is False


def test_alias_path_keys_are_fenced_too(chat_uploads, tmp_path):
    """``file_path`` is what the tool's schema says, but a relayed call spelling
    it differently must not get a free pass."""
    (chat_uploads / "ok.png").write_bytes(b"x")
    assert chat_mode_read_permitted("Read", {"path": f"{CHAT_UPLOADS_DIRNAME}/ok.png"}) is True
    assert chat_mode_read_permitted("Read", {"path": str(tmp_path / "no.txt")}) is False
    assert chat_mode_read_permitted("Read", {"filePath": str(tmp_path / "no.txt")}) is False


@pytest.mark.parametrize(
    "tool_name,tool_input",
    [
        ("WebSearch", {"query": "anything"}),
        ("WebFetch", {"url": "https://example.com"}),
        # Names the NAME half already refuses. This function is not their gate
        # and must not pretend to be one — it answers only about reads.
        ("Bash", {"command": "cat /etc/passwd"}),
        ("Edit", {"file_path": "/etc/passwd"}),
        ("Task", {"prompt": "read /etc/passwd"}),
        ("mcp__browser__navigate", {"file_path": "/etc/passwd"}),
        ("", {}),
    ],
)
def test_non_read_tools_pass_through_untouched(chat_uploads, tool_name, tool_input):
    assert chat_mode_read_permitted(tool_name, tool_input) is True


def test_read_is_on_the_chat_surface_and_the_host_tools_are_not():
    """The NAME half, asserted against the constant itself so a future edit that
    quietly adds Bash to the chat lane fails here."""
    assert "Read" in CHAT_MODE_TOOL_SURFACE
    assert "Bash" not in CHAT_MODE_TOOL_SURFACE
    assert "Task" not in CHAT_MODE_TOOL_SURFACE


def test_a_relative_path_is_read_against_the_workspace_root_only(chat_uploads):
    """One base, deliberately, even though a chat session's cwd is the uploads
    folder. Resolving from there too would let ``chat-uploads/../secret.env``
    land back inside the fence and answer True to a caller that plainly meant an
    escape. A bare cwd-relative filename is therefore False here — and that
    costs nothing, because both runtimes resolve and admit such a call
    themselves before this predicate is ever consulted."""
    assert _read(f"{CHAT_UPLOADS_DIRNAME}/notes.txt") is True
    assert _read("notes.txt") is False


def test_ensure_chat_uploads_dir_creates_it(tmp_path, monkeypatch):
    """It becomes the session cwd, and a cwd that does not exist fails the spawn
    with an OS error that explains nothing."""
    import app.core.config as config

    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path / "ws")
    created = ensure_chat_uploads_dir()
    assert created.is_dir()
    assert created == chat_uploads_dir()


def test_read_glob_covers_the_uploads_subtree_and_nothing_beside_it(chat_uploads):
    """The glob handed to the runtime permission rules has to agree with the
    predicate — they are two matchers over one intent, and a disagreement is
    either a hole or a lane that cannot read its own attachments."""
    from fnmatch import fnmatchcase

    glob = chat_mode_read_glob()
    assert fnmatchcase((chat_uploads / "notes.txt").as_posix(), glob)
    assert fnmatchcase((chat_uploads / "sub" / "notes.txt").as_posix(), glob)
    assert not fnmatchcase(
        (chat_uploads.parent / f"{CHAT_UPLOADS_DIRNAME}-evil" / "x").as_posix(), glob
    )
    assert not fnmatchcase((chat_uploads.parent / "secret.env").as_posix(), glob)


def test_uploads_dir_follows_a_workspace_move(tmp_path, monkeypatch):
    """The directory is resolved per call, not captured at import: an install
    that repoints its workspace must not keep fencing reads to the old root."""
    import app.core.config as config

    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path / "a")
    first = chat_uploads_dir()
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path / "b")
    assert chat_uploads_dir() != first


def test_code_sessions_are_not_narrowed_at_all():
    """The lane-scoping guard: this whole fence is chat-only. A code session's
    surface has to stay the agent's own, Read included."""
    access = ToolAccess(allowed=["Read", "Bash", "Task"], disallowed=None)
    for mode in ("code", None, "voice"):
        assert narrow_tool_access_for_mode(access, mode).allowed == ["Read", "Bash", "Task"]


@pytest.mark.parametrize("name", ["Remember", "Recall", "Forget", "mcp__builtins__Remember", "mcp__builtins__Recall", "mcp__builtins__Forget"])
def test_chat_allows_account_memory_but_preserves_agent_denials(name):
    access = narrow_tool_access_for_mode(ToolAccess(), "chat")
    assert tool_permitted(name, access.allowed, access.disallowed)
    denied = narrow_tool_access_for_mode(ToolAccess(disallowed=[name]), "chat")
    assert not tool_permitted(name, denied.allowed, denied.disallowed)
    assert not tool_permitted("Bash", access.allowed, access.disallowed)
    assert not tool_permitted("Edit", access.allowed, access.disallowed)


def test_chat_delegated_surface_exposes_memory_bridge_without_widening_reads():
    from app.core.provider.types import SessionOptions
    from app.core.provider.harness_adapter import _delegated_allowed
    options = SessionOptions(model="sonnet", tool_allowlist=list(CHAT_MODE_TOOL_SURFACE), extra={"session_mode":"chat"})
    names = _delegated_allowed(options)
    assert "mcp__builtins__Remember" in names
    assert "mcp__builtins__Recall" in names
    assert "mcp__builtins__Forget" in names
    assert "Read" not in names
