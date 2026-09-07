"""Session modes — the sidepanel surface a session belongs to.

Three modes, persisted on the pre-existing ``sessions.mode`` column:

============  ==============  ===========================================
mode          stored value    what it is
============  ==============  ===========================================
``code``      ``NULL``        the full agentic surface (projects, agents,
                              branches, worktrees, every tool)
``chat``      ``'chat'``      a lightweight chat: model, effort, attachments,
                              transcription, usage — the web, plus the files
                              you attach, and no checkout
``voice``     ``'voice'``     an OpenAI Realtime call (ruling VM4)
============  ==============  ===========================================

**Why NULL means 'code'.** The column landed in v74 for voice mode and has only
ever held NULL or ``'voice'``; nothing has ever written ``'chat'``. So the
mode taxonomy is grafted onto that column by renaming the NULL case rather than
by migrating rows: every session that exists today is a code session, and a new
code session keeps storing NULL for exact parity with them. ``'chat'`` is the
only genuinely new value. That is also why ``storage_session_mode`` exists —
the storage form and the API form differ for exactly one mode.

**Why chat mode is enforced server-side.** The user still picks whatever agent
they like in chat mode; the mode is a CEILING over that agent's own tool
configuration, not a substitute for it. Binding the restriction to a special
"chat agent" would leak the moment someone switched agents, edited that agent,
or the session was resumed from another device. So the narrowing happens where
the tool surface is actually built (``query_options``) and where the permission
callback independently rebuilds it (``query_engine``), off the session row —
the one fact that travels with the session everywhere.

**Where the READ fence is actually enforced.** Not, on its own, in the app's
``can_use_tool`` callback: neither runtime consults that callback for a call it
has already decided to allow. fluid's ``PermissionEngine`` auto-allows a
``risk="safe"`` tool under the ``on_write`` policy without emitting a permission
event at all, and the delegated CLI both auto-approves a *bare* name in
``--allowed-tools`` and auto-approves any read whose path sits inside the
session's working directory. A fence that lived only in the callback would
therefore never run. So the path half is enforced at the two points the runtimes
DO consult, with the callback kept as a third line of defence:

* native leg — a ``Read``-scoped fluid rule pair (deny everything, then allow
  the uploads glob), built in ``provider/harness_adapter._build_permissions``.
  A rule beats the policy axis, so it fires before the safe-tool auto-allow.
* delegated leg — the chat session's cwd IS ``chat_uploads_dir()``, so the CLI's
  in-working-directory read auto-approval covers exactly the uploads folder and
  nothing else, and ``Read`` is withheld from the CLI's bare ``--allowed-tools``
  grant so every read outside that folder falls through to the permission-prompt
  tool, i.e. to the app callback, which denies it.

Kept free of DB/SDK imports, exactly like ``app.core.tool_access`` (its only
import), because ``app.core.models`` and the query builders all need it and any
heavier dependency would close an import cycle. ``chat_uploads_dir`` needs
``settings`` and so imports it *inside the function* — that keeps the module's
import graph exactly as light as it was.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Mapping, Optional, Tuple

from app.core.tool_access import DENY_ALL, ToolAccess, intersect_tool_surfaces

# The three sidepanel surfaces. Order is the switcher's order.
SESSION_MODES: Tuple[str, ...] = ("code", "chat", "voice")

DEFAULT_SESSION_MODE = "code"

# The whole tool surface a chat session may reach: general web lookup, plus a
# reader for the files the user attached to the chat itself. ``Read`` is on the
# list because the composer keeps its attach button and an attachment reaches
# the model as an ``@<fullPath>`` text reference — without a reader the model
# can see the filename and nothing else. Nothing here can write or run anything
# on the host, and ``Read`` is fenced to the uploads directory by
# ``chat_mode_read_permitted`` (the name half of the ceiling is this list; the
# path half is that function).
CHAT_MODE_TOOL_SURFACE: List[str] = ["WebSearch", "WebFetch", "Read"]

# Where the chat lane's attachments land, under the workspace root. ONE shared
# directory rather than one per session: a brand-new chat has no session id at
# the moment the file is uploaded, so there is nothing to key a per-session
# folder on. Files therefore stay readable across chats, which is the same
# guarantee the project lane's ``uploads/`` folder already gives.
CHAT_UPLOADS_DIRNAME = "chat-uploads"

# Tools whose reads the path half of the ceiling has to gate. Only ``Read`` is
# on ``CHAT_MODE_TOOL_SURFACE``, so in practice nothing else can reach the
# callback anyway; the set exists so the gate is keyed on a name it recognises
# rather than on "everything that is not a web tool". Public because the two
# real enforcement points (the fluid rule pair and the CLI's withheld grant)
# have to name the same tools this predicate gates.
CHAT_MODE_READ_TOOLS = frozenset({"Read"})

# Names a chat session must be denied OUTRIGHT rather than merely left off the
# allow list. fluid's code mode exempts its own four surface tools from the CE12
# allowlist (``NativeLoop._tool_permitted`` short-circuits on
# ``source == CODE_MODE_SOURCE``), so an allow list of web tools does not remove
# ``run_python`` — only a deny does. Without this a chat session on a native leg
# would be handed a Python kernel, which is a strictly larger surface than the
# lane it was sold as. The primary fix is that the adapter does not BUILD a code
# mode session for this lane at all; this list is the belt for that braces, and
# it deliberately leaves the three read-only surface tools alone so a
# mis-wired build degrades visibly instead of granting a kernel silently.
CHAT_MODE_DENIED_TOOLS: List[str] = ["run_python", "save_skill", "delete_skill"]

# ``Read``'s own parameter is ``file_path`` (fluid's read tool schema). The
# aliases are read defensively — a relayed or re-serialized tool call that spelt
# it differently must not slip past the fence by presenting a key this function
# does not know to look at.
_CHAT_MODE_READ_PATH_KEYS: Tuple[str, ...] = ("file_path", "filePath", "path")


def normalize_session_mode(value: Any) -> str:
    """Coerce anything into one of ``SESSION_MODES``.

    NULL, blank and unrecognized values all resolve to ``'code'`` — an unknown
    string must never reach a client or a tool-surface decision, and the safe
    reading of "I don't know what this is" is the mode every pre-existing row
    is in.
    """
    if not isinstance(value, str):
        return DEFAULT_SESSION_MODE
    text = value.strip().lower()
    if text in SESSION_MODES:
        return text
    return DEFAULT_SESSION_MODE


def session_mode_of(session_row: Optional[Mapping[str, Any]]) -> str:
    """The mode of a raw ``sessions`` row dict; ``'code'`` when there is no row.

    A missing row is treated as a code session because that is what every
    non-session spawn (override/project-only builds, tests) has always been.
    """
    if not session_row:
        return DEFAULT_SESSION_MODE
    return normalize_session_mode(session_row.get("mode"))


def storage_session_mode(mode: Any) -> Optional[str]:
    """The value to write to ``sessions.mode`` for ``mode``.

    ``'code'`` stores NULL so a new code session is byte-identical to every row
    that predates this feature — nothing downstream has to learn a second
    spelling for "ordinary session".
    """
    normalized = normalize_session_mode(mode)
    if normalized == DEFAULT_SESSION_MODE:
        return None
    return normalized


def permission_mode_for_session_mode(permission_mode: Any, mode: Any) -> Any:
    """Normalize retired settings and keep chat sessions on default permissions."""
    # Old saved sessions and clients may still send the retired mode.
    if permission_mode == "plan":
        return "default"
    if normalize_session_mode(mode) != "chat":
        return permission_mode
    return "default"


def narrow_tool_access_for_mode(access: ToolAccess, mode: Any) -> ToolAccess:
    """Compose an agent's own ``ToolAccess`` with the ceiling its mode imposes.

    Only chat mode has a ceiling. Code mode is the full surface by definition,
    and voice sessions DISPATCH worker agents (ruling VM10) — clipping their
    tools would break the very thing a voice call is for — so both are returned
    untouched.

    For chat, the agent's surface is intersected with ``CHAT_MODE_TOOL_SURFACE``
    (ruling T3: the narrower of two surfaces wins, and ``intersect_tool_surfaces``
    already handles None-means-unrestricted and pattern/exact cross-matching).
    An EMPTY intersection means the agent's own config already denies the web
    tools, and it must not be returned as ``allowed=[]`` — an empty allow list
    reads as *unrestricted* on the runtime legs, which is precisely the class of
    bug ruling T2 exists to kill. It becomes a ``["*"]`` deny instead.
    """
    if normalize_session_mode(mode) != "chat":
        return access

    surface = intersect_tool_surfaces(access.allowed, CHAT_MODE_TOOL_SURFACE)
    if not surface:
        return ToolAccess(
            allowed=None, disallowed=[DENY_ALL, *(access.disallowed or [])]
        )
    # The deny list is not merely the allow list's complement here — see
    # ``CHAT_MODE_DENIED_TOOLS`` for the one family the allow list cannot remove.
    blocked = list(access.disallowed or [])
    blocked.extend(name for name in CHAT_MODE_DENIED_TOOLS if name not in blocked)
    return ToolAccess(allowed=surface, disallowed=blocked)


def chat_uploads_dir() -> Path:
    """Absolute, resolved path of the chat lane's upload directory.

    Resolved (not merely joined) because it is one side of a containment
    comparison: if the workspace root reaches the agent through a symlink, an
    unresolved base would never be a prefix of the resolved target and every
    legitimate read would be denied.

    ``settings`` is imported here rather than at module scope to keep this
    module's import graph as light as its docstring promises.
    """
    from app.core.config import settings

    return (settings.effective_workspace_dir / CHAT_UPLOADS_DIRNAME).resolve()


def ensure_chat_uploads_dir() -> Path:
    """``chat_uploads_dir()``, created on disk if it is not there yet.

    A chat session RUNS in this directory (it is the session cwd — see the
    module docstring on where the read fence is enforced), and a cwd that does
    not exist fails the spawn with an opaque OS error long before anything
    explains why. Creating it here is also what makes the delegated CLI's
    in-working-directory read auto-approval line up with the fence instead of
    silently falling back to some parent.
    """
    target = chat_uploads_dir()
    target.mkdir(parents=True, exist_ok=True)
    return target


def chat_mode_read_glob() -> str:
    """The uploads directory as a recursive path glob, for permission rules.

    fluid's rule matcher normalizes both pattern and value to forward slashes,
    and its ``*`` crosses separators — so one ``.../**`` covers the folder's
    whole subtree. Built from the same resolved path as the fence so the rule
    and ``chat_mode_read_permitted`` can never disagree about where the fence is.
    """
    return f"{chat_uploads_dir().as_posix()}/**"


def chat_mode_read_permitted(tool_name: str, tool_input: Any) -> bool:
    """False when a chat session's file read would escape the uploads directory.

    True for every tool that is not a file read, and for a read whose resolved
    target is inside ``chat_uploads_dir()``. This is the PATH half of the chat
    ceiling; the NAME half is ``CHAT_MODE_TOOL_SURFACE``. Both halves are needed:
    the name half puts ``Read`` on the surface at all, and without this the lane
    would have handed a projectless chat a reader for the whole disk.

    The containment test is deliberately paranoid, because this is the only
    thing standing between "read my attachment" and "read anything on this
    machine":

    * ``resolve()`` first, so ``..`` segments and symlinks are gone before the
      comparison — a symlink planted inside the uploads folder points at its
      target here, not at itself.
    * ``is_relative_to`` on the resolved paths, never a string prefix: a sibling
      directory named ``chat-uploads-evil`` shares the prefix of the uploads
      path and would sail through ``startswith``.
    * A relative path resolves against the WORKSPACE ROOT — the base the
      composer's ``chat-uploads/foo.png`` spelling is written against — and only
      that one, even though a chat session's cwd is now the uploads folder
      itself. Admitting a second base would make this predicate answer "yes" to
      a spelling whose caller meant an escape (``chat-uploads/../secret.env``
      lands back inside the fence when resolved from the uploads folder), and a
      security predicate that has to be read twice to see which base rescued a
      path is one nobody can audit. Nothing is lost by the strictness: a bare
      cwd-relative filename is resolved and admitted by the runtimes themselves
      (fluid's rule matcher normalizes against cwd; the CLI auto-approves reads
      inside its working directory), so such a call never reaches this function.
    * Anything unreadable as a path — missing key, blank, non-string, non-dict
      input, an OS that refuses to resolve it — is DENIED. A file read the fence
      cannot evaluate is not a file read it may wave through.
    """
    if tool_name not in CHAT_MODE_READ_TOOLS:
        return True

    raw: Optional[str] = None
    if isinstance(tool_input, Mapping):
        for key in _CHAT_MODE_READ_PATH_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                raw = value.strip()
                break
    if raw is None:
        return False

    try:
        from app.core.config import settings

        uploads = chat_uploads_dir()
        target = Path(raw)
        if not target.is_absolute():
            # Taken from settings rather than from ``uploads.parent`` so a
            # symlinked uploads folder cannot quietly move the base out from
            # under the comparison.
            target = settings.effective_workspace_dir / target
        target = target.resolve()
    except (OSError, ValueError, RuntimeError):
        return False

    # is_relative_to is true for the directory itself as well as its contents,
    # which is the answer we want for both.
    return target.is_relative_to(uploads)
