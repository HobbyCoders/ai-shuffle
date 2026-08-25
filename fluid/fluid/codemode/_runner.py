"""In-kernel runner — the child side of ``SubprocessKernel``.

Launched as ``python -I -S _runner.py``. Because ``-I`` keeps the script's own
directory off ``sys.path`` (it implies ``-P``), the runner re-adds its directory
first so it can import its stdlib-only siblings ``vfs`` and ``truncate`` as
top-level modules. Everything here runs *before* the per-cell import guard is
installed, so the runner itself may import freely; the guard applies only to the
namespace the model's code executes in (built by ``vfs.make_namespace``).

Wire protocol (length-prefixed JSON, ``>I`` header) matched to
``SubprocessKernel``:

    host → child : boot{config} · set_vfs{sources} · exec{code} · host_reply{…} · shutdown
    child → host : ready · vfs_ok · host_call{name,kwargs} · done{stdout,value_repr,error}

A host call blocks the cell: the runner emits ``host_call`` and reads the next
frame as the reply (result / error / denied). Large results are wrapped in
``vfs.ResultRef`` so only a truncated ``repr`` reaches the model.
"""

import ast
import io
import json
import os
import re
import struct
import sys
import traceback

# ``-I`` drops the script dir from sys.path; re-add it for the sibling imports.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import truncate
import vfs

_IN = sys.stdin.buffer  # binary framing channels, captured before any redirect
_OUT = sys.stdout.buffer

# Boot-configured globals (populated by the boot frame).
_REF_THRESHOLD = truncate.DEFAULT_LIMIT
_STDOUT_CAP = 16384
_SOURCES: dict = {}
_SKILLS: dict = {}
_NS: dict = {}


# ─── framing ─────────────────────────────────────────────────────────────────


def _read_exact(n: int) -> bytes:
    chunks = []
    got = 0
    while got < n:
        chunk = _IN.read(n - got)
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def _read_frame():
    header = _read_exact(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack(">I", header)
    body = _read_exact(length)
    if len(body) < length:
        return None
    return json.loads(body.decode("utf-8"))


def _send(obj: dict) -> None:
    body = json.dumps(obj).encode("utf-8")
    _OUT.write(struct.pack(">I", len(body)) + body)
    _OUT.flush()


# ─── host-call RPC (blocks the cell until the host answers) ──────────────────


def _host_call(tool_name: str, kwargs: dict):
    _send({"kind": "host_call", "name": tool_name, "kwargs": kwargs})
    reply = _read_frame()
    if reply is None:
        raise vfs.ToolError("host pipe closed during a tool call")
    if reply.get("denied"):
        raise vfs.HostCallDenied(reply.get("message") or "permission denied")
    if reply.get("error") is not None:
        raise vfs.ToolError(reply["error"])
    result = reply.get("result")
    if result is None:
        result = ""
    return truncate.to_result_ref(result, vfs.ResultRef, limit=_REF_THRESHOLD)


# ─── cell execution ──────────────────────────────────────────────────────────

_AWAIT_HINT = "Hint: tools are synchronous functions — call them without await."

# A tool argument written as a bare token instead of a string —
# ``Grep(pattern=def foo|bar, glob=*.py)``. The regex only has to recognise the
# shape on the SyntaxError's own source line, so it stays deliberately loose.
_BARE_ARG_RE = re.compile(
    r"\b(pattern|glob|path|file_path|command|query|old_string|new_string|"
    r"content|description|prompt|url)\s*=\s*([^\s'\"),][^,)]*)"
)

_QUOTED_START_RE = re.compile(r"^[rbfu]{0,2}['\"]")

_UNQUOTED_HINT = (
    "Hint: tool arguments are Python values — quote every string. Write "
    'Grep(pattern=r"def foo|bar", glob="*.py"), not pattern=def foo|bar. '
    "Use a raw string (r\"...\") for regexes and Windows paths."
)

_UNTERMINATED_HINT = (
    "Hint: the string literal never closes. Do not nest a shell heredoc "
    "(<<'EOF') or a long multi-line body inside a Python string — assign the "
    "text to a triple-quoted variable, or Write it to a file and point the "
    "command at that file (e.g. gh pr create --body-file), then pass the "
    "command as one ordinary string."
)

_IMPORT_SPELLING_HINT = (
    "Hint: import a tool as `from tools.Grep import Grep`, `from tools import "
    "Grep`, or `import tools` then `tools.Grep(...)`. `from tools.X as Y` is "
    "not valid Python."
)

_STR_RESULT_HINT = (
    "Hint: a tool call returns text (str / ResultRef), never a dict — parse it "
    "with json.loads(...) when the tool emits JSON, or slice it as a string."
)


def _stub_signature(name: str) -> str:
    """The projected stub's ``def`` line, so a bad call self-corrects.

    Sending the real signature back beats telling the model to spend another
    turn on ``read_tool_docs``.
    """
    source = _SOURCES.get(name)
    if not source:
        return ""
    for line in source.splitlines():
        if line.startswith("def "):
            return line[len("def "):].rstrip(":").strip()
    return ""


def _signature_hint(name: str) -> str:
    sig = _stub_signature(name)
    if sig:
        return f"Hint: the signature is {sig} — pass only those parameters."
    return (
        "Hint: pass only the parameters in the tool's signature; run "
        f'read_tool_docs("{name}") to see it.'
    )


def _syntax_hint(exc: SyntaxError) -> str:
    message = str(exc)
    if "'await' outside function" in message:
        return _AWAIT_HINT
    text = (getattr(exc, "text", None) or "")
    match = _BARE_ARG_RE.search(text)
    if match is not None and not _QUOTED_START_RE.match(match.group(2).strip()):
        return _UNQUOTED_HINT
    if "unterminated" in message or "was never closed" in message:
        return _UNTERMINATED_HINT
    if "tools." in text and " as " in text and text.lstrip().startswith("from "):
        return _IMPORT_SPELLING_HINT
    return ""


def _error_hint(exc: BaseException) -> str:
    """A one-line nudge for the mistakes the projected stubs invite.

    Cells run at module level, so ``await tool(...)`` is a SyntaxError with no
    clue that the stubs are plain functions; a wrong kwarg is a bare TypeError
    with no pointer at the signature. Unquoted tool arguments and shell
    heredocs nested inside Python strings are the two authoring mistakes that
    dominate real transcripts, and both surface as an opaque SyntaxError. Every
    hint is appended to the traceback so the correction rides back in the
    model's context instead of costing another turn.
    """
    message = str(exc)
    if isinstance(exc, SyntaxError):
        return _syntax_hint(exc)
    if isinstance(exc, ImportError) and message.startswith("cannot import name "):
        pkg = getattr(exc, "name", None) or ""
        if pkg in ("tools", "skills"):
            return (
                f"Hint: no such {'tool' if pkg == 'tools' else 'skill'} — run "
                "list_tools or search_tools to find the right name."
            )
    if isinstance(exc, TypeError) and "got an unexpected keyword argument" in message:
        name = message.split("(", 1)[0].strip().rsplit(".", 1)[-1] or "the tool"
        return _signature_hint(name)
    if isinstance(exc, TypeError) and "required positional argument" in message:
        name = message.split("(", 1)[0].strip().rsplit(".", 1)[-1] or "the tool"
        return _signature_hint(name)
    if isinstance(exc, AttributeError) and "'str' object has no attribute" in message:
        return _STR_RESULT_HINT
    return ""


def _format_cell_traceback(exc: BaseException) -> str:
    """Traceback text starting at the model's own code.

    The cell runs under ``exec``/``eval`` inside :func:`_run_cell`, so a raw
    ``format_exc`` leads with runner internals (``eval(compile(ast.Expression
    ...``) the model cannot act on. Frames are dropped up to the first one
    whose file is ``<cell>``; a failure with no cell frame at all (a
    SyntaxError from parsing, an error in a stub import before the cell ran)
    renders as just the exception line.
    """
    tb = exc.__traceback__
    while tb is not None and tb.tb_frame.f_code.co_filename != "<cell>":
        tb = tb.tb_next
    if tb is None:
        return "".join(traceback.format_exception_only(type(exc), exc))
    return "".join(traceback.format_exception(type(exc), exc, tb))


def _run_cell(code: str):
    buf = io.StringIO()
    value_repr = ""
    err = None
    saved = sys.stdout
    sys.stdout = buf
    try:
        tree = ast.parse(code, "<cell>", "exec")
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last = tree.body.pop()
            if tree.body:
                exec(compile(tree, "<cell>", "exec"), _NS)
            value = eval(compile(ast.Expression(last.value), "<cell>", "eval"), _NS)
            if value is not None:
                value_repr = repr(value)
        else:
            exec(compile(tree, "<cell>", "exec"), _NS)
    except Exception as exc:
        err = _format_cell_traceback(exc)
        hint = _error_hint(exc)
        if hint:
            err = err.rstrip("\n") + "\n" + hint + "\n"
    finally:
        sys.stdout = saved
    out = buf.getvalue()
    if len(out) > _STDOUT_CAP:
        out = out[:_STDOUT_CAP] + f"\n…[stdout truncated at {_STDOUT_CAP} chars]"
    return out, value_repr, err


# ─── namespace checkpoint / restore (kernel-death resilience) ────────────────
#
# Per-variable pickle so one unpicklable object doesn't void the checkpoint.
# Functions/classes defined in cells pickle by reference to the __cell__
# module, which won't exist in a fresh kernel — they are dropped (documented);
# data (strings, numbers, containers, ResultRefs) survives.

_CKPT_SKIP = ("ResultRef", "ToolError", "HostCallDenied", "Path", *vfs.PRELUDE_MODULES)


def _checkpoint() -> dict:
    import base64
    import pickle
    import types

    vars_map: dict = {}
    dropped: list = []
    for key, value in _NS.items():
        if key.startswith("__") or key in _CKPT_SKIP:
            continue
        # Module objects never pickle and are rebuilt by the fresh kernel's
        # prelude anyway; listing them as "dropped" would be pure noise.
        if isinstance(value, types.ModuleType):
            continue
        try:
            blob = pickle.dumps(value)
        except Exception:
            dropped.append(key)
            continue
        vars_map[key] = base64.b64encode(blob).decode("ascii")
    return {"kind": "checkpoint", "vars": vars_map, "dropped": sorted(dropped)}


def _restore(vars_map: dict) -> dict:
    import base64
    import pickle

    restored: list = []
    dropped: list = []
    for key, encoded in (vars_map or {}).items():
        try:
            _NS[key] = pickle.loads(base64.b64decode(encoded))
            restored.append(key)
        except Exception:
            dropped.append(key)
    return {"kind": "restored", "restored": sorted(restored), "dropped": sorted(dropped)}


def _set_vfs(sources: dict, skills: dict) -> None:
    _SOURCES.clear()
    _SOURCES.update(sources or {})
    _SKILLS.clear()
    _SKILLS.update(skills or {})
    import importlib

    for name in [
        n for n in list(sys.modules)
        if n in ("tools", "skills") or n.startswith(("tools.", "skills."))
    ]:
        del sys.modules[name]
    importlib.invalidate_caches()


# ─── boot + main loop ────────────────────────────────────────────────────────


def _apply_rlimit(mem_limit_bytes: int) -> None:
    if not mem_limit_bytes:
        return
    try:
        import resource

        if hasattr(resource, "RLIMIT_AS"):
            resource.setrlimit(resource.RLIMIT_AS, (mem_limit_bytes, mem_limit_bytes))
    except (ValueError, OSError, ImportError):  # pragma: no cover - platform dependent
        pass


def _guard_network() -> None:
    try:
        import socket

        class _BlockedSocket:
            def __init__(self, *a, **k):
                raise OSError("network access is blocked by the code-mode kernel")

        socket.socket = _BlockedSocket
    except Exception:  # pragma: no cover
        pass


def _guard_filesystem() -> None:
    """Block direct file I/O so the host permission gate cannot be sidestepped.

    Patches ``builtins.open`` (before ``make_namespace`` copies builtins) plus
    the ``io``-module escape routes (``io.open`` — which ``pathlib`` delegates
    to — ``io.FileIO``, ``io.open_code``). Model code must use the projected
    file tools instead; a huge file read that way still stays in-kernel as a
    ResultRef. Like the other guards this is a soft boundary (importlib's
    internal ``_io`` references are untouched so allowlisted imports work);
    the hard gates remain host-side.
    """
    import builtins as _builtins
    import io as _io

    _WRITE_MODES = ("w", "a", "x", "+")

    def _blocked_open(file=None, mode="r", *a, **k):
        """Refuse, and name the tool that does the job the call was reaching for.

        The old message said "use the projected file tools" and left the model
        to guess which one; naming Read/Write (and the path it already passed)
        turns the refusal into a call it can make straight away.
        """
        want = "Write" if any(m in str(mode) for m in _WRITE_MODES) else "Read"
        target = f"({file!r})" if isinstance(file, (str, bytes)) else "(...)"
        raise PermissionError(
            "direct file I/O is blocked in the code-mode kernel; use the "
            f"projected file tools so the host permission gate applies — "
            f"`from tools.{want} import {want}` then {want}{target}. "
            "Glob lists paths, Grep searches contents, Bash runs commands. "
            "A large result stays in-kernel as a ResultRef you can slice."
        )

    _builtins.open = _blocked_open  # type: ignore[assignment]
    _io.open = _blocked_open  # type: ignore[assignment]
    _io.open_code = _blocked_open  # type: ignore[assignment]
    _io.FileIO = _blocked_open  # type: ignore[assignment]


def _boot(config: dict) -> None:
    global _REF_THRESHOLD, _STDOUT_CAP, _NS
    _REF_THRESHOLD = config.get("ref_threshold", truncate.DEFAULT_LIMIT)
    _STDOUT_CAP = config.get("stdout_cap", 16384)
    _apply_rlimit(config.get("mem_limit_bytes", 0))
    if not config.get("allow_network", False):
        _guard_network()
    if not config.get("allow_fs", False):
        _guard_filesystem()  # must precede make_namespace (it copies builtins)
    allowlist = set(config.get("allowlist") or vfs.DEFAULT_ALLOWLIST)
    _NS = vfs.make_namespace(allowlist=allowlist, host_call=_host_call)
    vfs.install_finder(
        _SOURCES, _host_call,
        skills=_SKILLS, guarded_builtins=_NS["__builtins__"],
    )


def _main() -> None:
    boot = _read_frame()
    if boot is None or boot.get("kind") != "boot":
        return
    _boot(boot.get("config") or {})
    _send({"kind": "ready"})
    while True:
        frame = _read_frame()
        if frame is None:
            break
        kind = frame.get("kind")
        if kind == "exec":
            out, value_repr, err = _run_cell(frame.get("code", ""))
            _send({"kind": "done", "stdout": out, "value_repr": value_repr, "error": err})
        elif kind == "set_vfs":
            _set_vfs(frame.get("sources") or {}, frame.get("skills") or {})
            _send({"kind": "vfs_ok"})
        elif kind == "checkpoint":
            _send(_checkpoint())
        elif kind == "restore":
            _send(_restore(frame.get("vars") or {}))
        elif kind == "shutdown":
            break


if __name__ == "__main__":
    _main()
