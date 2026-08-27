"""Audit fixes: the post-edit syntax gate, whole-line removes, clean monitor output.

Three defects a production audit caught, one module each:

* Edit wrote whatever the model handed it into a ``.py`` file and told the
  model not to read it back, so a broken replacement surfaced at import time
  many turns later;
* Note/Ledger ``remove`` deleted every line *containing* the text, which turned
  one imprecise call into a deleted checklist;
* monitored subprocesses were spawned without the no-colour hints and their
  output reached the model with ANSI escapes still in it.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from fluid.cli.host import CliHost, Worker, _split_trailing_escape
from fluid.tools.builtins.edit import handler as edit_handler
from fluid.tools.builtins.ledger import handler as ledger_handler
from fluid.tools.builtins.note import handler as note_handler
from fluid.tools.registry import ToolCtx

PY = sys.executable

GOOD = "def f(x):\n    if x:\n        return 1\n    return 0\n"


def _ctx(tmp_path) -> ToolCtx:
    return ToolCtx(cwd=str(tmp_path), session_id="s1")


def _py(tmp_path, name: str = "mod.py", body: str = GOOD):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


async def _edit(path, ctx: ToolCtx, old: str, new: str, **extra):
    ctx.read_files.add(str(path))
    return await edit_handler(
        {"file_path": str(path), "old_string": old, "new_string": new, **extra}, ctx
    )


# ─── Edit: the post-edit syntax gate ─────────────────────────────────────────


async def test_edit_rejects_a_result_that_would_not_parse(tmp_path) -> None:
    path = _py(tmp_path)
    ctx = _ctx(tmp_path)
    # The audited failure exactly: a bare condition left dangling.
    out = await _edit(path, ctx, "    if x:", "    if :")
    assert out.is_error
    assert "REJECTED" in out.text
    assert "line 2" in out.text  # the SyntaxError's own line
    assert "if :" in out.text  # ...quoted back
    assert path.read_text(encoding="utf-8") == GOOD  # nothing written


async def test_rejection_names_the_syntax_error(tmp_path) -> None:
    path = _py(tmp_path)
    ctx = _ctx(tmp_path)
    out = await _edit(path, ctx, "    return 0\n", "    return 0\n:w\n")
    assert out.is_error and "SyntaxError" in out.text
    assert path.read_text(encoding="utf-8") == GOOD


async def test_valid_python_edit_still_lands(tmp_path) -> None:
    path = _py(tmp_path)
    ctx = _ctx(tmp_path)
    out = await _edit(path, ctx, "return 1", "return 2")
    assert not out.is_error
    assert "return 2" in path.read_text(encoding="utf-8")


async def test_edit_to_an_already_broken_file_is_allowed(tmp_path) -> None:
    # Repairs must stay possible, and a repair can take more than one Edit —
    # so the gate only refuses the parses-then-breaks transition.
    broken = "def f(:\n    return 1\n"
    path = _py(tmp_path, body=broken)
    ctx = _ctx(tmp_path)
    out = await _edit(path, ctx, "return 1", "return 2")
    assert not out.is_error
    assert path.read_text(encoding="utf-8") == "def f(:\n    return 2\n"


async def test_non_python_files_are_not_parsed(tmp_path) -> None:
    path = _py(tmp_path, name="notes.md", body="# title\n")
    ctx = _ctx(tmp_path)
    out = await _edit(path, ctx, "# title", "if :")
    assert not out.is_error


async def test_pyi_targets_are_gated_too(tmp_path) -> None:
    path = _py(tmp_path, name="mod.pyi", body="def f(x: int) -> int: ...\n")
    ctx = _ctx(tmp_path)
    out = await _edit(path, ctx, "def f", "def :f")
    assert out.is_error and "REJECTED" in out.text


async def test_replace_all_path_is_gated(tmp_path) -> None:
    body = "x = 1\ny = 1\n"
    path = _py(tmp_path, body=body)
    ctx = _ctx(tmp_path)
    out = await _edit(path, ctx, "1", "1 +", replace_all=True)
    assert out.is_error and "REJECTED" in out.text
    assert path.read_text(encoding="utf-8") == body


async def test_line_prefix_fallback_path_is_gated(tmp_path) -> None:
    # old_string carries Read's "lineno<TAB>" display prefix — the edit still
    # matches, and the syntax gate still runs on the result.
    path = _py(tmp_path)
    ctx = _ctx(tmp_path)
    out = await _edit(path, ctx, "1\tdef f(x):", "def f(:")
    assert out.is_error and "REJECTED" in out.text
    assert path.read_text(encoding="utf-8") == GOOD


async def test_indentation_drift_path_is_gated(tmp_path) -> None:
    # old_string carries one extra tab of indentation on every line; the drift
    # is corrected, then the result is parsed before anything is written.
    path = _py(tmp_path)
    ctx = _ctx(tmp_path)
    out = await _edit(path, ctx, "\t    if x:\n\t        return 1", "    if :\n        return 1")
    assert out.is_error and "REJECTED" in out.text
    assert path.read_text(encoding="utf-8") == GOOD


async def test_indentation_drift_path_still_writes_valid_python(tmp_path) -> None:
    path = _py(tmp_path)
    ctx = _ctx(tmp_path)
    out = await _edit(path, ctx, "\t    if x:\n\t        return 1", "    if x:\n        return 7")
    assert not out.is_error
    assert "return 7" in path.read_text(encoding="utf-8")


# ─── Note: remove matches whole notes ────────────────────────────────────────


async def _notes(ctx: ToolCtx, *lines: str) -> None:
    await note_handler({"action": "add", "texts": list(lines)}, ctx)


async def test_note_remove_takes_one_whole_note(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    await _notes(ctx, "step one", "step two", "step three")
    out = await note_handler({"action": "remove", "text": "step two"}, ctx)
    assert not out.is_error
    assert "removed 1 note" in out.text and "2 remain" in out.text
    listing = await note_handler({"action": "list"}, ctx)
    assert "step two" not in listing.text
    assert "step one" in listing.text and "step three" in listing.text


async def test_note_remove_is_case_insensitive_and_strip_equal(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    await _notes(ctx, "Step One")
    out = await note_handler({"action": "remove", "text": "  step one  "}, ctx)
    assert not out.is_error and "removed 1 note" in out.text


async def test_note_remove_drops_every_identical_note(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    await _notes(ctx, "dedupe me", "keep me", "dedupe me")
    out = await note_handler({"action": "remove", "text": "dedupe me"}, ctx)
    assert not out.is_error
    assert "removed 2 notes" in out.text and "1 remain" in out.text
    listing = await note_handler({"action": "list"}, ctx)
    assert "dedupe me" not in listing.text and "keep me" in listing.text


async def test_note_remove_without_an_exact_match_deletes_nothing(tmp_path) -> None:
    # The audited destruction: "step" used to take the whole checklist with it.
    ctx = _ctx(tmp_path)
    await _notes(ctx, "step one", "step two", "step three")
    out = await note_handler({"action": "remove", "text": "step"}, ctx)
    assert out.is_error
    listing = await note_handler({"action": "list"}, ctx)
    for line in ("step one", "step two", "step three"):
        assert line in listing.text
        assert line in out.text  # ...offered back as candidates


async def test_note_remove_candidate_list_is_capped(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    await _notes(ctx, *[f"step {i}" for i in range(9)])
    out = await note_handler({"action": "remove", "text": "step"}, ctx)
    assert out.is_error
    assert out.text.count("\n- ") == 5
    assert "4 more" in out.text


async def test_note_remove_with_no_near_notes_says_so(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    await _notes(ctx, "step one")
    out = await note_handler({"action": "remove", "text": "nothing like it"}, ctx)
    assert out.is_error and "action=list" in out.text


# ─── Ledger: the same shape, per section ─────────────────────────────────────


async def test_ledger_remove_takes_one_whole_entry(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    for text in ("use postgres", "use redis for the queue"):
        await ledger_handler(
            {"action": "add", "section": "decisions", "text": text}, ctx
        )
    out = await ledger_handler(
        {"action": "remove", "section": "decisions", "text": "use postgres"}, ctx
    )
    assert not out.is_error
    assert "removed 1 decisions entry" in out.text and "1 remain" in out.text
    show = await ledger_handler({"action": "show"}, ctx)
    assert "use postgres" not in show.text and "use redis" in show.text


async def test_ledger_remove_without_an_exact_match_deletes_nothing(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    for text in ("use postgres", "use redis for the queue"):
        await ledger_handler(
            {"action": "add", "section": "decisions", "text": text}, ctx
        )
    out = await ledger_handler(
        {"action": "remove", "section": "decisions", "text": "use"}, ctx
    )
    assert out.is_error
    assert "use postgres" in out.text and "use redis for the queue" in out.text
    show = await ledger_handler({"action": "show"}, ctx)
    assert "use postgres" in show.text and "use redis for the queue" in show.text


# ─── Monitor output: no colour in, no escapes through ────────────────────────


class _RecordingSpawn:
    """Captures the kwargs the host passes to create_subprocess_exec."""

    def __init__(self) -> None:
        self.kwargs: dict = {}

    async def __call__(self, *argv, **kwargs):
        self.kwargs = kwargs

        class _Proc:
            pid = 1234
            returncode = 0
            stdout = None

            async def wait(self) -> int:
                return 0

        return _Proc()


async def test_monitor_spawns_with_colour_disabled(tmp_path, monkeypatch) -> None:
    spawn = _RecordingSpawn()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    host = CliHost(config=None, store=None)
    monkeypatch.setattr(host, "_supervise", lambda worker: asyncio.sleep(0))
    await host.monitor_spawn({"command": "pytest -q"}, _ctx(tmp_path))
    env = spawn.kwargs["env"]
    assert env["NO_COLOR"] == "1" and env["FORCE_COLOR"] == "0"
    assert "PATH" in env or "Path" in env  # the ambient environment is preserved


@pytest.mark.parametrize("chunked", [False, True])
async def test_monitor_output_reaches_the_model_ansi_free(chunked: bool) -> None:
    coloured = "\x1b[31mFAILED\x1b[0m tests/test_x.py::test_y\n\x1b]0;title\x07done\n"
    host = CliHost(config=None, store=None)
    worker = Worker(task_id="mon-1", kind="monitor", description="pytest")

    class _Stdout:
        def __init__(self) -> None:
            raw = coloured.encode()
            # `chunked` splits mid-escape, the case a 4096-byte read hits in
            # real output: half a sequence must not survive as literal text.
            self.parts = [raw[:3], raw[3:]] if chunked else [raw]

        async def read(self, _n: int) -> bytes:
            return self.parts.pop(0) if self.parts else b""

    class _Proc:
        stdout = _Stdout()

        async def wait(self) -> int:
            return 1

    worker.process = _Proc()
    reported: list[str] = []
    host._report = lambda w, status, summary: reported.append(summary) or asyncio.sleep(0)  # type: ignore[assignment]
    await host._supervise(worker)
    assert "\x1b" not in worker.output()
    assert "FAILED tests/test_x.py::test_y" in worker.output()
    assert "done" in worker.output()


def test_split_trailing_escape_holds_only_incomplete_sequences() -> None:
    assert _split_trailing_escape("plain text") == ("plain text", "")
    assert _split_trailing_escape("a\x1b[31mb") == ("a\x1b[31mb", "")
    assert _split_trailing_escape("a\x1b[3") == ("a", "\x1b[3")
    assert _split_trailing_escape("a\x1b") == ("a", "\x1b")
    assert _split_trailing_escape("a\x1b]0;ti") == ("a", "\x1b]0;ti")
    # An escape that never terminates is flushed rather than buffered forever.
    long_tail = "a\x1b]" + "x" * 400
    assert _split_trailing_escape(long_tail) == (long_tail, "")


async def test_monitor_summary_of_a_real_colourising_process(tmp_path) -> None:
    """End to end: a child that prints escapes regardless of NO_COLOR."""
    host = CliHost(config=None, store=None)
    receipt = await host.monitor_spawn(
        {"command": f"\"{PY}\" -c \"import sys; sys.stdout.write('\\x1b[32mok\\x1b[0m')\""},
        _ctx(tmp_path),
    )
    worker = host.workers[receipt.task_id]
    assert worker.task is not None
    await asyncio.wait_for(asyncio.shield(worker.task), timeout=30)
    assert "\x1b" not in worker.output()
    assert "ok" in worker.output()
