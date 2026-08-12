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
import struct
import sys
import traceback

# ``-I`` drops the script dir from sys.path; re-add it for the sibling imports.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import truncate  # noqa: E402  (top-level sibling, stdlib-only)
import vfs  # noqa: E402

_IN = sys.stdin.buffer  # binary framing channels, captured before any redirect
_OUT = sys.stdout.buffer

# Boot-configured globals (populated by the boot frame).
_REF_THRESHOLD = truncate.DEFAULT_LIMIT
_STDOUT_CAP = 16384
_SOURCES: dict = {}
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
    except Exception:
        err = traceback.format_exc()
    finally:
        sys.stdout = saved
    out = buf.getvalue()
    if len(out) > _STDOUT_CAP:
        out = out[:_STDOUT_CAP] + f"\n…[stdout truncated at {_STDOUT_CAP} chars]"
    return out, value_repr, err


def _set_vfs(sources: dict) -> None:
    _SOURCES.clear()
    _SOURCES.update(sources or {})
    import importlib

    for name in [n for n in list(sys.modules) if n == "tools" or n.startswith("tools.")]:
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


def _boot(config: dict) -> None:
    global _REF_THRESHOLD, _STDOUT_CAP, _NS
    _REF_THRESHOLD = config.get("ref_threshold", truncate.DEFAULT_LIMIT)
    _STDOUT_CAP = config.get("stdout_cap", 16384)
    _apply_rlimit(config.get("mem_limit_bytes", 0))
    if not config.get("allow_network", False):
        _guard_network()
    allowlist = set(config.get("allowlist") or vfs.DEFAULT_ALLOWLIST)
    _NS = vfs.make_namespace(allowlist=allowlist, host_call=_host_call)
    vfs.install_finder(_SOURCES, _host_call)


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
            _set_vfs(frame.get("sources") or {})
            _send({"kind": "vfs_ok"})
        elif kind == "shutdown":
            break


if __name__ == "__main__":
    _main()
