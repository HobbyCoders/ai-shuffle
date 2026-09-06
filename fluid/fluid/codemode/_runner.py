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
``vfs.ResultRef`` so only a truncated ``repr`` reaches the model, and every ref
is registered in the namespace's ``_refs`` registry under a short id its repr
prints — a dropped handle is recovered with ``_refs[7]``, never by re-running
the tool. A call that would deliver a payload already sitting behind a live ref
(the host says so with ``content_id``: same file, same bytes) gets the id back
instead of the bytes; a call that would deliver a *slice* of such a payload
(``window_of``) gets the id and the slicing expression, whether the ref holds
the file whole or an earlier window wide enough to contain the new one. The
previous cell's displayed value stays bound to ``_``.
"""

import ast
import io
import json
import os
import re
import struct
import sys
import traceback
import types

# ``-I`` drops the script dir from sys.path; re-add it for the sibling imports.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _audit_guard
import truncate
import vfs

_IN = sys.stdin.buffer  # binary framing channels, captured before any redirect
_OUT = sys.stdout.buffer

# Boot-configured globals (populated by the boot frame).
_REF_THRESHOLD = truncate.DEFAULT_LIMIT
_STDOUT_CAP = truncate.DEFAULT_STDOUT_CAP
_CKPT_MAX_BYTES = 0  # 0: no budget (the host keeps its own cap)
_SOURCES: dict = {}
_SKILLS: dict = {}
_NS: dict = {}
# The ResultRef registry backing the namespace's ``_refs``. Held here too so a
# cell that rebinds the name ``_refs`` cannot detach the registry the RPC
# boundary writes into.
_REFS = vfs.RefRegistry()
# ``content_id`` (host-supplied payload identity) -> ``(ResultRef, covered)``:
# the ref holding that payload and how much of it the ref actually has.
# ``covered is None`` means the whole thing; ``(start, stop)`` means the ref
# holds only those lines of it. Populated only for results big enough to become
# a ref; a hit means a re-read is pointless and gets a pointer instead.
#
# A *windowed* read files itself twice: under its own exact-window identity
# (covering it whole) and under the file's window-independent identity with the
# span it covers. That second entry is what makes a model that only ever reads
# with offset/limit — the common case in transcripts, where the same file was
# read four times and the dedupe never once fired — hit the ref it already has
# whenever the new window sits inside the old one. Deliberately NOT
# checkpointed: a restarted kernel has no refs to point at, so it starts empty
# and every read is a real read again.
_READ_REFS = {}
# One entry per host result this cell produced, in call order:
# ``[registry id | None, size, tool leaf, held text | None]``. A result big
# enough to ride behind a ref has its id already; a smaller one is carried here
# as text and only becomes a ref if the cell turns out to have overrun its
# stdout budget (see ``_cell_refs``) — the case where a sub-threshold result
# was cut with no way back. The stdout-cap notice and the per-cell footer name
# these so a lost print is recovered with a slice instead of a re-run.
_CELL_REFS = []
_CELL_SPILL_MAX = vfs.REF_REGISTRY_MAX  # unprinted results held per cell
# Leaf names of the host calls that COMPLETED in this cell, in order. A cell
# that dies mid-way reports what never got attempted, and that report has to
# know whether anything already landed on disk.
_CELL_DONE = []


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


def _leaf(tool_name: str) -> str:
    """``mcp__server__Edit`` → ``Edit`` — the name the cell actually wrote."""
    return str(tool_name).split("__")[-1]


_MUTATING_RISKS = ("write", "destructive")


def _mutating(leaf: str) -> bool:
    """Whether a projected tool can change the filesystem.

    The stub's docstring carries ``[risk: …]`` (see ``projection.stub_source``),
    so the kernel can tell an Edit from a Grep without the host's spec.
    """
    return any(f"[risk: {r}]" in (_SOURCES.get(leaf) or "") for r in _MUTATING_RISKS)


def _host_call(tool_name: str, kwargs: dict):
    # Deliberately one frame, not a wrapper around an inner function: a tool
    # error's traceback reaches the model with every frame below the cell in
    # it, and an extra kernel frame is an extra line of noise on every failure.
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
    # The call returned, so it counts as completed: the "never attempted"
    # report picks up its numbering from here.
    leaf = _leaf(tool_name)
    _CELL_DONE.append(leaf)
    content_id = reply.get("content_id")
    window = reply.get("window_of")
    held = _live_entry(content_id)
    if held is not None and held[1] is None:
        # This exact payload is already in the kernel and still reachable, so
        # the model gets the id rather than a second copy of the bytes. Any
        # break in that chain — the file changed, a different window, the ref
        # aged out of the registry — moves or clears the map entry, and the
        # read falls through below as an ordinary one. An entry that covers
        # only PART of this identity cannot answer for the whole thing, so it
        # is not a hit here.
        ref = held[0]
        _record_ref(ref.key, len(ref), leaf)
        return truncate.repeat_read_pointer(ref.key, len(ref))
    if isinstance(window, dict):
        # A *narrower* claim: this result is a slice of a payload the host has
        # identified. If the kernel holds a payload that contains this window —
        # the file whole, or an earlier window wide enough — the model gets the
        # slicing expression rather than a second, overlapping copy. This is
        # what makes re-reading the same file at a new offset/limit free.
        start = window.get("start") or 0
        stop = window.get("stop")
        whole = _live_entry(window.get("content_id"))
        if whole is not None:
            ref, covered = whole
            base = _slice_base(covered, start, stop)
            if base is not None:
                _record_ref(ref.key, len(ref), leaf)
                return truncate.windowed_read_pointer(
                    ref.key, len(ref), start, stop, offset=base
                )
    wrapped = truncate.to_result_ref(result, vfs.ResultRef, limit=_REF_THRESHOLD)
    if not isinstance(wrapped, vfs.ResultRef):
        # Under the ref threshold, so it is delivered whole — but a cell that
        # blows its stdout budget can still lose it, and until this was held it
        # was lost with no way back (three whole-file Reads in one cell, one
        # ref). Held as text; promoted to a real ref only if the cell overruns.
        _hold_unwrapped(leaf, result)
    else:
        # Every ref gets an id in ``_refs`` before the cell sees it, so a
        # handle the cell forgets to bind is still recoverable from the id its
        # repr printed (``_refs[7]``) instead of costing a re-run of the tool.
        _REFS.register(wrapped)
        _record_ref(wrapped.key, len(wrapped), leaf)
        if content_id:
            _remember_ref(content_id, wrapped, None)
        if isinstance(window, dict) and window.get("content_id"):
            # Also file it under the window-independent identity of the whole
            # payload, with the span this ref actually covers.
            _remember_ref(
                window["content_id"],
                wrapped,
                (window.get("start") or 0, window.get("stop")),
            )
    return wrapped


def _record_ref(key, size, leaf) -> None:
    """Note that this cell produced (or was pointed at) ref ``key``."""
    _CELL_REFS.append([key, size, leaf, None])


def _hold_unwrapped(leaf, text) -> None:
    """Keep a sub-threshold result in reach in case the cell overruns stdout."""
    if not isinstance(text, str) or len(text) < truncate.SPILL_MIN:
        return
    if sum(1 for e in _CELL_REFS if e[0] is None) >= _CELL_SPILL_MAX:
        return  # the registry holds no more than this anyway
    _CELL_REFS.append([None, len(text), leaf, text])


def _cell_refs(spill: bool):
    """This cell's ``(id, size, tool)`` handles, promoting held text if asked.

    ``spill`` is "the cell printed more than its budget": every result of such
    a cell becomes recoverable, because the one that got cut is exactly the one
    the model cannot see well enough to know it needs.
    """
    out = []
    for entry in _CELL_REFS:
        if entry[0] is None:
            if not spill:
                continue
            ref = vfs.ResultRef(entry[3])
            _REFS.register(ref)
            entry[0], entry[3] = ref.key, None
        out.append((entry[0], entry[1], entry[2]))
    return out


def _live_entry(content_id):
    """``(ref, covered)`` for ``content_id`` while the ref is still held."""
    if not content_id:
        return None
    entry = _READ_REFS.get(content_id)
    if not isinstance(entry, tuple):
        return None
    ref, covered = entry
    key = getattr(ref, "key", None)
    if key is None:
        return None
    # ``_refs`` is bounded and FIFO: an evicted id is gone, and asking the
    # registry (rather than trusting the map) is what makes eviction fall
    # through to a real read instead of pointing at a dead handle.
    live = _REFS.get(key)
    return None if live is None else (live, covered)


def _contains(covered, start, stop) -> bool:
    """True when a ref covering ``covered`` can serve lines ``start:stop``."""
    if covered is None:  # holds the payload whole
        return True
    low, high = covered
    if start < low:
        return False
    if high is None:  # covered through the end
        return True
    return stop is not None and stop <= high


def _slice_base(covered, start, stop):
    """Line index the held ref starts at, or None when it cannot serve."""
    if not _contains(covered, start, stop):
        return None
    return 0 if covered is None else covered[0]


def _wider(a, b) -> bool:
    """True when coverage ``a`` is at least as wide as coverage ``b``."""
    if a is None:
        return True
    if b is None:
        return False
    return _contains(a, b[0], b[1])


def _remember_ref(content_id, ref, covered) -> None:
    """File ``ref`` as a holder of ``content_id``, keeping the widest entry."""
    existing = _live_entry(content_id)
    if existing is not None and not _wider(covered, existing[1]):
        return
    _READ_REFS[content_id] = (ref, covered)
    if len(_READ_REFS) <= vfs.REF_REGISTRY_MAX:
        return
    for cid, entry in list(_READ_REFS.items()):
        held = entry[0] if isinstance(entry, tuple) else None
        if getattr(held, "key", None) not in _REFS:
            del _READ_REFS[cid]


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

# The tail of the tool call's own JSON envelope, left on the last line of the
# cell: ``print(r)"}``. The model closed the `code` string and the argument
# object in the source it was writing. It is the cheapest SyntaxError to
# diagnose and the one an audited session hit on its very first tool call, so
# it is checked before the heredoc story and answered in one line.
_ENVELOPE_TAIL_RE = re.compile(r"""["']\s*\}\s*,?\s*$""")

_ENVELOPE_HINT = (
    'Hint: the cell ends with the tool call\'s own argument envelope — a stray '
    '`"}` after your last statement. Those characters belong to the request, '
    "not to the code. Delete them and re-issue the cell otherwise unchanged."
)

_UNTERMINATED_HINT = (
    "Hint: the string literal never closes. Check the END of the cell first: a "
    'stray `"}` or `\'}` after the last statement is the tool call\'s own '
    "argument envelope leaking into the source — delete it and re-issue the "
    "cell unchanged. Otherwise the usual causes are a shell "
    "heredoc (<<'EOF') nested in a Python string and a long prose argument — "
    "a commit message, a PR body, a markdown report — whose own quotes, "
    '``` fences or """ run into the literal that holds it. Give the prose its '
    "own triple-quoted variable, with a delimiter the text does not itself "
    "contain (body = '''…''' when the text has no '''; "
    'r"""…""" otherwise), or Write it to a file '
    "and point the command at that file (git commit -F <file>, gh pr create "
    "--body-file <file>) — a file outside the repo working tree, so git add "
    "-A cannot commit it. Then pass the command as one ordinary string."
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


def _missing_name(exc: BaseException, message: str) -> str:
    """The unbound name behind a NameError, from the attribute or the text."""
    name = getattr(exc, "name", None)
    if isinstance(name, str) and name:
        return name
    match = re.search(r"name '([^']+)' is not defined", message)
    return match.group(1) if match else ""


def _name_error_hint(name: str) -> str:
    """A NameError against a mounted tool means the namespace was reset.

    Python answers with ``Did you mean: 'hash'?`` — a suggestion that is never
    right for ``Bash``, ``Note`` or ``Ledger``, and four audited sessions lost
    a cell to it. The kernel knows which names it mounts, so it can say the one
    thing that helps: the tool is here, this namespace just does not have it
    bound any more, and a kernel restart between turns is why.
    """
    if not name:
        return ""
    if name in _SOURCES:
        return (
            f"Hint: `{name}` is a tool mounted in this kernel but not bound in "
            f"this namespace — `from tools import {name}` first (Python's own "
            "'Did you mean' suggestion above is a builtin, not your tool). If "
            "you imported it in an earlier turn, the kernel has restarted "
            "since: every name bound by earlier cells is gone, so re-import "
            "the tools this cell uses."
        )
    if name in _SKILLS:
        return (
            f"Hint: `{name}` is a saved skill — `from skills import {name}`. A "
            "kernel restart between turns drops earlier imports, so re-import "
            "it before use."
        )
    if name in vfs.PRELUDE_MODULES:
        return (
            f"Hint: `{name}` is pre-bound in every fresh cell, so it is missing "
            "only because a cell rebound or deleted it — `import "
            f"{name}` to get the module back."
        )
    return ""


def _shadowed_module_hint(attr: str) -> str:
    """A stdlib module shadowed by a variable holding a tool result.

    ``re = Read(...)`` seven hundred sequence numbers earlier, then ``re.match``
    in a later cell: the persistent namespace makes that failure arbitrarily
    far from its cause, and the generic dict-vs-str hint pointed at the wrong
    problem entirely.
    """
    if not attr:
        return ""
    for name in vfs.PRELUDE_MODULES:
        value = _NS.get(name)
        if value is None or isinstance(value, types.ModuleType):
            continue
        module = sys.modules.get(name)
        if module is None or not hasattr(module, attr):
            continue
        return (
            f"Hint: `{name}` in this kernel is bound to a tool result, not the "
            f"stdlib module — a cell assigned it (`{name} = SomeTool(...)`), "
            f"which shadows the pre-imported `{name}`. Rename that variable, "
            f"or `import {name}` to get the module back."
        )
    return ""


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
        # Deliberately inline and pointer-free: the kernel holds the stub for
        # EVERY projected tool, deferred ones included, so a bad call to a tool
        # whose docs never entered the prompt still self-corrects here without
        # spending a turn on read_tool_docs.
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
    if _ENVELOPE_TAIL_RE.search(text.rstrip()):
        return _ENVELOPE_HINT
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
        if pkg == "tools":
            return (
                "Hint: no such tool in the kernel VFS — `dir(tools)` lists "
                "every name mounted here, and `tools.<name>.__doc__` is its "
                "documentation. (list_tools / search_tools / read_tool_docs "
                "are separate top-level tool calls; they are not importable "
                "from `tools` and cannot be called inside a cell.)"
            )
        if pkg == "skills":
            return (
                "Hint: no such skill — `dir(skills)` lists the skills mounted "
                "in this kernel."
            )
    if isinstance(exc, TypeError) and "got an unexpected keyword argument" in message:
        name = message.split("(", 1)[0].strip().rsplit(".", 1)[-1] or "the tool"
        return _signature_hint(name)
    if isinstance(exc, TypeError) and "required positional argument" in message:
        name = message.split("(", 1)[0].strip().rsplit(".", 1)[-1] or "the tool"
        return _signature_hint(name)
    if isinstance(exc, NameError):
        return _name_error_hint(_missing_name(exc, message))
    if isinstance(exc, AttributeError) and "object has no attribute" in message:
        attr = getattr(exc, "name", None)
        if not isinstance(attr, str) or not attr:
            match = re.search(r"has no attribute '([^']+)'", message)
            attr = match.group(1) if match else ""
        # A shadowed stdlib module is a more specific diagnosis than "tools
        # return text", and it is the one that names the actual mistake.
        shadow = _shadowed_module_hint(attr)
        if shadow:
            return shadow
        if "'str' object has no attribute" in message:
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
        text = "".join(traceback.format_exception_only(type(exc), exc))
    else:
        text = "".join(traceback.format_exception(type(exc), exc, tb))
    return _scrub_host_paths(text)


_KERNEL_DIR = os.path.dirname(os.path.abspath(__file__))
_HOME_DIR = os.path.expanduser("~")


def _scrub_host_paths(text: str) -> str:
    """Replace host filesystem paths a traceback would print with placeholders.

    Frames below the cell (the kernel's own modules, the user's home) name
    directories on the host; the model has no use for them and a transcript
    should not carry them.
    """
    if _KERNEL_DIR and _KERNEL_DIR in text:
        text = text.replace(_KERNEL_DIR, "<kernel>")
    if _HOME_DIR and len(_HOME_DIR) > 1 and _HOME_DIR in text:
        text = text.replace(_HOME_DIR, "~")
    return text


class _SegmentedStdout(io.TextIOBase):
    """Cell stdout capture that remembers where each ``print`` ended.

    ``print(a, b)`` reaches the stream as several ``write`` calls (arg,
    separator, arg, terminator), so a segment is defined as the run of writes
    up to and including one that ends in a newline — one segment per print,
    near enough. Keeping the boundaries is what lets the cap be fair-shared
    across prints instead of tail-dropped (see ``truncate.fair_share_stdout``).
    """

    def __init__(self) -> None:
        self._done: list = []
        self._open: list = []

    # ── stream surface ──
    def write(self, text) -> int:
        if not isinstance(text, str):
            text = str(text)
        if not text:
            return 0
        self._open.append(text)
        if text.endswith("\n"):
            self._done.append("".join(self._open))
            self._open.clear()
        return len(text)

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def flush(self) -> None:
        pass

    # ── capture surface ──
    def segments(self) -> list:
        if self._open:
            return [*self._done, "".join(self._open)]
        return list(self._done)

    def getvalue(self) -> str:
        return "".join(self._done) + "".join(self._open)


# ─── the calls the error cancelled ───────────────────────────────────────────
#
# A host call that raises aborts the whole cell, so everything queued after it
# is dropped without a word: audited sessions lost Note/Ledger bookkeeping,
# commits and Monitor launches this way and re-issued them a turn later — once,
# twelve model calls later. The host names the calls that *completed*; this
# names the ones that never got their turn, read statically off the cell source.

# Statements whose body may not run at all, so a call inside one is only
# "possibly" pending — a helper the cell defines but never reaches is the
# obvious false positive.
_CONDITIONAL = tuple(
    node for node in (
        ast.If, ast.For, ast.While, ast.Try, ast.FunctionDef, ast.AsyncFunctionDef,
        ast.ClassDef, ast.AsyncFor, ast.Lambda, ast.IfExp, ast.ListComp,
        ast.DictComp, ast.SetComp, ast.GeneratorExp,
        getattr(ast, "Match", None), getattr(ast, "TryStar", None),
    ) if node is not None
)

# The kwarg that says what a call would have acted on, most identifying first
# (the host's manifest labels completed calls the same way).
_LABEL_KEYS = (
    "file_path", "path", "notebook_path", "target_file", "command",
    "pattern", "url", "query", "name", "description", "prompt",
)
_LABEL_CAP = 60
_UNATTEMPTED_MAX = 20


def _cell_lineno(exc: BaseException):
    """Line of the model's own code that was executing when it blew up."""
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == "<cell>":
            return tb.tb_lineno
        tb = tb.tb_next
    return None


def _call_label(node) -> str:
    """A short, recognisable label for what a queued call would have done."""
    literals = {
        kw.arg: kw.value.value
        for kw in node.keywords
        if kw.arg and isinstance(kw.value, ast.Constant)
        and isinstance(kw.value.value, str)
    }
    text = next((literals[key] for key in _LABEL_KEYS if key in literals), "")
    if not text:
        first = node.args[0] if node.args else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            text = first.value
    text = " ".join(text.split())
    if len(text) > _LABEL_CAP:
        text = text[:_LABEL_CAP] + "…"
    return text


def _called_tool(node, known) -> str:
    """The projected tool a ``Call`` node invokes — ``Edit``, ``tools.Edit``."""
    func = node.func
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    else:
        return ""
    return name if name in known else ""


def _tool_calls_after(code: str, lineno: int, known=None) -> list:
    """``[(line, name, label, conditional)]`` for tool calls past ``lineno``.

    Best-effort and deliberately static: the cell is dead, so there is nothing
    left to ask. A call reached only through a branch, a loop or a function
    body is flagged ``conditional`` — it may never have been due to run.
    """
    known = _SOURCES if known is None else known
    try:
        tree = ast.parse(code, "<cell>", "exec")
    except SyntaxError:
        return []
    found: list = []

    def walk(node, conditional: bool) -> None:
        for child in ast.iter_child_nodes(node):
            nested = conditional or isinstance(child, _CONDITIONAL)
            if isinstance(child, ast.Call):
                name = _called_tool(child, known)
                if name and getattr(child, "lineno", 0) > lineno:
                    found.append(
                        (child.lineno, name, _call_label(child), nested)
                    )
            walk(child, nested)

    walk(tree, False)
    found.sort(key=lambda item: item[0])
    return found


def _unattempted_note(code: str, exc: BaseException) -> str:
    """Name the host calls the aborted cell never got to, or ``""``."""
    try:
        lineno = _cell_lineno(exc)
        if lineno is None:
            return ""
        sites = _tool_calls_after(code, lineno)
        if not sites:
            return ""
        # The host numbers completed calls 1..N and the failing one last, so
        # picking up from there keeps one sequence across both lines.
        failed = isinstance(exc, (vfs.ToolError, vfs.HostCallDenied))
        index = len(_CELL_DONE) + (2 if failed else 1)
        shown = sites[:_UNATTEMPTED_MAX]
        # Numbered in the order they are listed, not in source order: a loop
        # makes source order a fiction anyway, and a list that counts 5, 6, 4
        # reads as a mistake.
        ordered = [s for s in shown if not s[3]] + [s for s in shown if s[3]]
        sure: list = []
        maybe: list = []
        for _line, name, label, conditional in ordered:
            (maybe if conditional else sure).append(
                f"{index}. {name}({label})" if label else f"{index}. {name}()"
            )
            index += 1
        rest = len(sites) - len(shown)
        note = ""
        if sure:
            note = (
                "never attempted — the cell stopped at the error, so these "
                f"queued calls did not run: {', '.join(sure)}."
            )
        if maybe:
            lead = " Possibly not attempted" if note else (
                "possibly not attempted — the cell stopped at the error, and "
                "these call sites come after it"
            )
            note += (
                f"{lead} (inside a branch, loop or function, so they may not "
                f"have been due to run): {', '.join(maybe)}."
            )
        if rest > 0:
            note += f" (… and {rest} more call sites after those.)"
        note += " Re-issue whichever you still need"
        if any(_mutating(name) for name in _CELL_DONE):
            note += (
                " — but the Edits/Writes that DID complete are already on "
                "disk; do not re-issue those."
            )
        else:
            note += "."
        return note
    except Exception:  # pragma: no cover - a diagnostic must never mask the bug
        return ""


def _run_cell(code: str):
    buf = _SegmentedStdout()
    value_repr = ""
    err = None
    saved = sys.stdout
    sys.stdout = buf
    _CELL_REFS.clear()
    _CELL_DONE.clear()
    try:
        tree = ast.parse(code, "<cell>", "exec")
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last = tree.body.pop()
            if tree.body:
                exec(compile(tree, "<cell>", "exec"), _NS)
            value = eval(compile(ast.Expression(last.value), "<cell>", "eval"), _NS)
            if value is not None:
                value_repr = repr(value)
                # REPL semantics: the displayed value stays reachable as ``_``
                # in the next cell, so an unbound last expression (a tool call
                # written bare) is never lost.
                _NS["_"] = value
        else:
            exec(compile(tree, "<cell>", "exec"), _NS)
    except Exception as exc:
        err = _format_cell_traceback(exc)
        hint = _error_hint(exc)
        if hint:
            err = err.rstrip("\n") + "\n" + hint + "\n"
        queued = _unattempted_note(code, exc)
        if queued:
            err = err.rstrip("\n") + "\n" + queued + "\n"
    except (SystemExit, KeyboardInterrupt) as exc:
        # A cell that calls sys.exit() or raises KeyboardInterrupt ends the
        # cell, not the kernel: letting it out would kill the child and the
        # host would report a death it cannot explain.
        err = _format_cell_traceback(exc).rstrip("\n") + (
            "\nThe kernel keeps running; a cell cannot exit the process.\n"
        )
    finally:
        sys.stdout = saved
    # The registry is the runner's, not the cell's: rebinding ``_refs`` in a
    # cell shadows the view, never the recording.
    _NS["_refs"] = _REFS
    segments = buf.segments()
    truncated = bool(segments) and sum(len(s) for s in segments) > _STDOUT_CAP
    refs = _cell_refs(truncated)
    out = truncate.fair_share_stdout(segments, _STDOUT_CAP, refs=refs)
    if not truncated:
        # A truncated cell's notice already names the handles; an untruncated
        # one said nothing at all, which is how a model ends up guessing which
        # ``_refs`` slot holds its Read.
        footer = truncate.refs_footer(refs)
        if footer:
            out = out.rstrip("\n")
            out = f"{out}\n{footer}" if out else footer
    return out, value_repr, err


# ─── namespace checkpoint / restore (kernel-death resilience) ────────────────
#
# Per-variable pickle so one unpicklable object doesn't void the checkpoint.
# Functions/classes defined in cells pickle by reference to the __cell__
# module, which won't exist in a fresh kernel — they are dropped (documented);
# data (strings, numbers, containers, ResultRefs) survives.

_CKPT_SKIP = (
    "ResultRef", "ToolError", "HostCallDenied", "Path",
    # The registry itself is runner state, not cell data: a fresh kernel builds
    # its own and re-adopts every ResultRef the restore brings back (see
    # ``_restore``), so ``_refs[7]`` keeps resolving for refs a variable still
    # holds. Refs that were only ever reachable through ``_refs`` — never
    # bound in the cell — do not survive a kernel restart.
    "_refs",
    "_",
    *vfs.PRELUDE_MODULES,
)


class _CheckpointTooLarge(Exception):
    pass


class _BoundedSink:
    """A write target for ``pickle.dump`` that gives up past a byte budget.

    Pickling a 50 MB variable and then finding the result oversized cost
    hundreds of milliseconds per cell. Raising as soon as the budget is
    crossed keeps the work bounded by the budget, not by the variable.
    """

    def __init__(self, budget: int) -> None:
        self.budget = budget
        self.chunks: list = []
        self.size = 0

    def write(self, data) -> int:
        self.size += len(data)
        if self.size > self.budget:
            raise _CheckpointTooLarge()
        self.chunks.append(data)
        return len(data)


def _checkpoint() -> dict:
    import base64
    import pickle
    import types

    vars_map: dict = {}
    dropped: list = []
    oversized: list = []
    # Budget on the pickled bytes; base64 adds a third on the wire, which the
    # host's own cap allows for.
    remaining = _CKPT_MAX_BYTES if _CKPT_MAX_BYTES > 0 else None
    for key, value in _NS.items():
        if key.startswith("__") or key in _CKPT_SKIP:
            continue
        # Module objects never pickle and are rebuilt by the fresh kernel's
        # prelude anyway; listing them as "dropped" would be pure noise.
        if isinstance(value, types.ModuleType):
            continue
        if remaining is not None and remaining <= 0:
            oversized.append(key)
            continue
        sink = _BoundedSink(remaining if remaining is not None else 1 << 62)
        try:
            pickle.dump(value, sink)
        except _CheckpointTooLarge:
            oversized.append(key)
            continue
        except Exception:
            dropped.append(key)
            continue
        blob = b"".join(sink.chunks)
        if remaining is not None:
            remaining -= len(blob)
        vars_map[key] = base64.b64encode(blob).decode("ascii")
    return {
        "kind": "checkpoint",
        "vars": vars_map,
        "dropped": sorted(dropped),
        "oversized": sorted(oversized),
        # Tool and skill stubs never pickle as data, and re-importing them is
        # both cheap and exactly what the model would have to do by hand. A
        # restart that drops `Bash` and `Note` is the audited NameError; the
        # names are recorded here so the restore can just put them back.
        "tools": sorted(_bound_stubs(_SOURCES)),
        "skills": sorted(_bound_stubs(_SKILLS)),
    }


def _bound_stubs(mounted: dict) -> list:
    """Names in the namespace that are imports of a mounted stub."""
    return [
        name for name, value in _NS.items()
        if name in mounted and callable(value)
    ]


def _reimport_stubs(names, package: str) -> list:
    """Re-bind ``from <package> import <name>`` for each name. Best-effort."""
    import importlib

    done: list = []
    for name in names or ():
        try:
            module = importlib.import_module(f"{package}.{name}")
            value = getattr(module, name)
        except Exception:
            continue
        _NS[name] = value
        done.append(name)
    return done


def _restore(vars_map: dict, tools=None, skills=None) -> dict:
    import base64
    import pickle

    restored: list = []
    dropped: list = []
    reimported = _reimport_stubs(tools, "tools") + _reimport_stubs(skills, "skills")
    for key, encoded in (vars_map or {}).items():
        try:
            value = pickle.loads(base64.b64decode(encoded))
        except Exception:
            dropped.append(key)
            continue
        _NS[key] = value
        # A ResultRef pickles with its registry id, so re-adopting it here
        # keeps ``_refs[7]`` resolving in the restarted kernel.
        if isinstance(value, vfs.ResultRef):
            _REFS.adopt(value)
        restored.append(key)
    return {
        "kind": "restored",
        "restored": sorted(restored),
        "dropped": sorted(dropped),
        "reimported": sorted(reimported),
    }


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
    global _REF_THRESHOLD, _STDOUT_CAP, _NS, _CKPT_MAX_BYTES
    _REF_THRESHOLD = config.get("ref_threshold", truncate.DEFAULT_LIMIT)
    _STDOUT_CAP = config.get("stdout_cap", truncate.DEFAULT_STDOUT_CAP)
    _CKPT_MAX_BYTES = int(config.get("checkpoint_max_bytes", 0) or 0)
    _apply_rlimit(config.get("mem_limit_bytes", 0))
    # Importing a stdlib module whose .pyc is missing (a fresh interpreter,
    # or a read-only install) makes importlib open the cache file for
    # writing, which the guard refuses. The kernel never needs bytecode
    # caches, so turn them off rather than widen the import carve-out.
    sys.dont_write_bytecode = True
    # The hard boundary: an audit hook the interpreter consults at every
    # spawn/socket/open no matter which object the cell reached it through.
    # Installed before the namespace exists, so no cell ever runs without it.
    _audit_guard.install(
        allow_fs=bool(config.get("allow_fs", False)),
        allow_network=bool(config.get("allow_network", False)),
    )
    if not config.get("allow_network", False):
        _guard_network()
    if not config.get("allow_fs", False):
        _guard_filesystem()  # must precede make_namespace (it copies builtins)
    allowlist = set(config.get("allowlist") or vfs.DEFAULT_ALLOWLIST)
    _NS = vfs.make_namespace(
        allowlist=allowlist, host_call=_host_call, refs=_REFS
    )
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
            _send(_restore(
                frame.get("vars") or {},
                frame.get("tools") or [],
                frame.get("skills") or [],
            ))
        elif kind == "shutdown":
            break


if __name__ == "__main__":
    _main()
