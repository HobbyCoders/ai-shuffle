"""Bash called from a code-mode cell hands the cell its whole output.

The 12,000-char inline cap exists for what the model reads. In a cell the
value goes to Python first; the cell's stdout cap bounds what reaches the
model, and a big result rides behind a ResultRef. Cutting the value there left
cells parsing a ``gh ... --json`` or ``git diff`` with its middle missing.

Pinned here:

- a cell that parses ~30k chars of JSON from Bash gets all of it, and no
  spill is written or counted;
- above :data:`bash.CELL_TRUNCATE_LIMIT` the cell result still spills, with
  the marker naming the file;
- a direct Bash call keeps the 12,000 cap, also when it runs inside a call a
  cell made (a worker started from a bridged tool does not inherit the cell's
  limit);
- the larger value cannot escape through a cell's error text: a long
  traceback (message, notes, chained causes) is cut tail-heavy and spilled,
  and the final exception line stays visible; a short one is unchanged.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from fluid.codemode import truncate as cm_truncate
from fluid.codemode.bridge import HostBridge
from fluid.codemode.projection import Projection
from fluid.codemode.subprocess_kernel import KernelConfig, SubprocessKernel
from fluid.permissions.engine import PermissionEngine
from fluid.tools import spill
from fluid.tools.builtins import _util, bash
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec


def _ctx(tmp_path: Path) -> ToolCtx:
    return ToolCtx(
        cwd=str(tmp_path), session_id="sess-cell",
        extra={"harness_state_root": tmp_path / "state"},
    )


def _json_command(items: int) -> str:
    """A command printing one JSON object of ``items`` ~100-char entries."""
    script = (
        "import json; print(json.dumps({'items': [{'n': i, 'pad': 'x' * 80} "
        f"for i in range({items})]}}))"
    )
    return f'"{sys.executable}" -c "{script}"'


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_builtin(bash.SPEC, bash.handler)
    return registry


async def _run_cell(registry: ToolRegistry, ctx: ToolCtx, code: str):
    bridge = HostBridge(
        registry, PermissionEngine("never_ask"), ctx=ctx,
        result_budget=KernelConfig().ref_threshold,
    )
    kernel = SubprocessKernel(KernelConfig())
    await kernel.start()
    try:
        await kernel.set_vfs(Projection(registry.list_tools()).sources())
        return await kernel.exec(code, timeout=30, on_host_call=bridge.on_host_call)
    finally:
        await kernel.stop()


def _spilled(ctx: ToolCtx) -> int:
    return spill.session_stats(ctx).get("spilled", {}).get("Bash", 0)


async def test_a_cell_parses_30k_of_json_from_bash_whole(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    items = 300  # ~30,000 chars: past the direct cap, under the cell limit
    code = (
        "import json\n"
        "from tools.Bash import Bash\n"
        f"r = Bash(command={_json_command(items)!r})\n"
        "data = json.loads(r.output)\n"
        "print(len(r.output), len(data['items']), data['items'][150]['n'])\n"
    )
    result = await _run_cell(_registry(), ctx, code)
    assert result.error is None, result.error
    size, count, middle = result.stdout.splitlines()[0].split()
    assert _util._TRUNCATE_LIMIT < int(size) < bash.CELL_TRUNCATE_LIMIT
    assert (count, middle) == (str(items), "150")
    # Nothing was cut, so nothing was spilled or counted as a spill.
    assert _spilled(ctx) == 0
    assert not spill.spill_dir(ctx).exists()


async def test_over_the_cell_limit_the_result_still_spills(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    code = (
        "from tools.Bash import Bash\n"
        f"r = Bash(command={_json_command(800)!r})\n"  # ~80,000 chars
        "print(len(r.output))\n"
        "print(r.output[r.output.index('... [cut'):].splitlines()[0])\n"
    )
    result = await _run_cell(_registry(), ctx, code)
    assert result.error is None, result.error
    size, marker = result.stdout.splitlines()[:2]
    assert int(size) <= bash.CELL_TRUNCATE_LIMIT + 1000  # head + tail + marker
    assert "full output saved to" in marker
    saved = Path(re.search(r"saved to (.+?\.txt)", marker).group(1))
    assert '"n": 799' in saved.read_text(encoding="utf-8")
    assert _spilled(ctx) == 1


async def test_a_direct_bash_call_keeps_the_12k_cap(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    out = await _registry().dispatch("Bash", {"command": _json_command(300)}, ctx)
    text, _code = out.shell_result
    assert len(text) <= _util._TRUNCATE_LIMIT + 1000
    assert "full output saved to" in text
    assert _spilled(ctx) == 1


async def test_a_direct_call_inside_a_bridged_call_keeps_the_12k_cap(tmp_path) -> None:
    """What a cell's call starts (a worker, a nested dispatch) gets its own
    direct-call limit, not the cell's."""
    ctx = _ctx(tmp_path)
    registry = _registry()
    seen: list[str] = []

    async def outer(_input, inner_ctx):
        inner = await registry.dispatch("Bash", {"command": _json_command(300)}, inner_ctx)
        seen.append(inner.shell_result[0])
        return ToolOutcome(text="done")

    registry.register_builtin(
        ToolSpec(name="Outer", description="nests a call",
                 input_schema={"type": "object", "properties": {}}),
        outer,
    )
    await registry.dispatch("Outer", {}, ctx, raw_shell_output=True)
    assert len(seen[0]) <= _util._TRUNCATE_LIMIT + 1000
    assert "full output saved to" in seen[0]


# ── the larger value must not escape through a cell's error text ────────────


class _Loop:
    """The minimal ``loop`` surface ``CodeModeSession.run_python`` reads."""

    def __init__(self, registry: ToolRegistry, ctx: ToolCtx) -> None:
        self.registry = registry
        self.ctx = ctx
        self.hooks = None
        self.permissions = PermissionEngine("never_ask")
        self.events: list = []

    async def _emit(self, event) -> None:
        self.events.append(event)


async def _run_python(tmp_path: Path, code: str, *, skills=None):
    from fluid.codemode.session import CodeModeSession

    registry = _registry()
    ctx = _ctx(tmp_path)
    session = CodeModeSession(registry, skills=skills)
    try:
        outcome = await session.run_python(
            sid="s1", turn_id="t1", input={"code": code, "timeout": 30},
            loop=_Loop(registry, ctx),
        )
    finally:
        await session.aclose()
    return outcome, ctx


_BASH_30K = f"from tools.Bash import Bash\nr = Bash(command={_json_command(300)!r})\n"
# Room for the fence, the spill pointer and the committed-calls manifest.
_SLACK = 2_000


async def test_an_exception_carrying_the_bash_value_is_bounded(tmp_path) -> None:
    code = _BASH_30K + "raise ValueError(str(r.output) + 'END-OF-MESSAGE')\n"
    outcome, ctx = await _run_python(tmp_path, code)
    assert outcome.is_error
    assert len(outcome.text) < cm_truncate.ERROR_CAP + _SLACK
    assert "ValueError: {" in outcome.text
    assert "END-OF-MESSAGE" in outcome.text  # the message keeps its tail
    saved = Path(re.search(r"chars saved to (.+?\.txt)", outcome.text).group(1))
    assert '"n": 150' in saved.read_text(encoding="utf-8")
    assert spill.session_stats(ctx)["spilled"] == {"run_python": 1}


async def test_a_long_note_on_a_chained_exception_keeps_the_final_type(tmp_path) -> None:
    code = _BASH_30K + (
        "try:\n"
        "    raise ValueError(str(r.output))\n"
        "except ValueError as e:\n"
        "    err = RuntimeError('parse step failed')\n"
        "    err.add_note(str(r.output))\n"
        "    raise err from e\n"
    )
    outcome, _ = await _run_python(tmp_path, code)
    assert outcome.is_error
    assert len(outcome.text) < cm_truncate.ERROR_CAP + _SLACK
    # Each link is clipped on its own, so both frames and both types survive.
    assert "ValueError: {" in outcome.text
    assert "RuntimeError: parse step failed" in outcome.text
    assert outcome.text.count('File "<cell>"') == 2
    assert "chars saved to" in outcome.text


async def test_a_frame_lookalike_in_a_message_does_not_fool_the_summary(tmp_path) -> None:
    code = (
        "try:\n"
        "    raise KeyError('x' * 30000)\n"
        "except KeyError as e:\n"
        "    raise RuntimeError('actual error\\n  File \"not-a-frame\", line 1\\n'\n"
        "                       'misleading detail\\n' + 'y' * 30000) from e\n"
    )
    result = await _run_cell(_registry(), _ctx(tmp_path), code)
    assert len(result.error) < cm_truncate.ERROR_CAP
    assert "RuntimeError: actual error" in result.error
    assert result.error_summary.startswith("RuntimeError: actual error")
    assert "misleading" not in result.error_summary.split("File")[0]
    outcome, _ = await _run_python(tmp_path, code)
    assert "RuntimeError: actual error" in outcome.text
    assert "[the cell ended with: misleading detail]" not in outcome.text


async def test_an_exception_group_raised_from_a_long_cause_keeps_its_heading(tmp_path) -> None:
    code = (
        "try:\n"
        "    raise KeyError('k' * 30000)\n"
        "except KeyError as e:\n"
        "    raise ExceptionGroup('batch failed', [RuntimeError('child broke')]) from e\n"
    )
    result = await _run_cell(_registry(), _ctx(tmp_path), code)
    assert len(result.error) < cm_truncate.ERROR_CAP
    assert "ExceptionGroup: batch failed (1 sub-exception)" in result.error
    assert "RuntimeError: child broke" in result.error
    assert result.error_summary == "ExceptionGroup: batch failed (1 sub-exception)"


async def test_many_clipped_parts_hit_the_backstop_with_a_summary(tmp_path) -> None:
    code = "raise ExceptionGroup('many', [ValueError('v' * 30000) for _ in range(15)])\n"
    result = await _run_cell(_registry(), _ctx(tmp_path), code)
    assert len(result.error) < cm_truncate.ERROR_CAP + 200
    assert result.error.startswith("[the cell ended with: ExceptionGroup: many (15 sub-exceptions)]")
    assert len(result.error_full) > 15 * 30_000


async def test_the_journal_and_learner_prompt_hold_only_the_summary(tmp_path) -> None:
    from fluid.codemode.continual import Synthesizer
    from fluid.codemode.skills import SkillStore

    store = SkillStore(tmp_path / "skills")
    code = _BASH_30K + "raise ValueError(str(r.output))\n"
    await _run_python(tmp_path, code, skills=store)
    entries = store.journal_entries()
    error = entries[-1]["error"]
    assert error.startswith("ValueError: {") and len(error) <= cm_truncate.ERROR_SUMMARY_CAP
    prompt = Synthesizer(None, "probe", store, context_window=200_000)._prompt(entries)
    assert '"n": 150' not in prompt


async def test_a_broken_skill_verdict_and_listing_are_bounded(tmp_path) -> None:
    from fluid.codemode.session import CodeModeSession, register_code_mode_tools
    from fluid.codemode.skills import SkillStore

    store = SkillStore(tmp_path / "skills")
    store.save("broken", '"""A probe."""\ndef _selftest():\n'
               '    raise ValueError("x" * 30000 + "END_ERROR")\n')
    registry = _registry()
    ctx = _ctx(tmp_path)
    session = CodeModeSession(registry, skills=store)
    register_code_mode_tools(registry, session)
    try:
        await session.run_python(
            sid="s1", turn_id="t1", input={"code": "print(42)", "timeout": 30},
            loop=_Loop(registry, ctx),
        )
        listing = await registry.dispatch("list_tools", {}, ctx)
    finally:
        await session.aclose()
    broken = store.get("broken").broken
    assert broken.startswith("ValueError: xxx") and len(broken) <= cm_truncate.ERROR_SUMMARY_CAP
    assert "BROKEN" in listing.text and len(listing.text) < 3_000


async def test_historical_errors_are_bounded_when_read(tmp_path) -> None:
    from fluid.codemode.continual import Synthesizer
    from fluid.codemode.session import CodeModeSession, register_code_mode_tools
    from fluid.codemode.skills import SkillStore

    store = SkillStore(tmp_path / "skills")
    error = "ValueError: " + "z" * 30_000 + "END_ERROR"
    store.save("broken", '"""An old skill."""\n')
    store.mark_verified("broken", error)
    store.journal_append({"kind": "cell", "code": "f()", "error": error})

    registry = _registry()
    session = CodeModeSession(registry, skills=store)
    register_code_mode_tools(registry, session)
    try:
        listing = await registry.dispatch("list_tools", {}, _ctx(tmp_path))
    finally:
        await session.aclose()
    prompt = Synthesizer(None, "probe", store, context_window=200_000)._prompt(store.journal_entries())
    for rendered in (listing.text, prompt):
        assert "ValueError:" in rendered and "END_ERROR" in rendered
        assert "z" * cm_truncate.ERROR_SUMMARY_CAP not in rendered


async def test_a_short_traceback_is_unchanged(tmp_path) -> None:
    result = await _run_cell(_registry(), _ctx(tmp_path), "x = {}\nx['missing']\n")
    # Byte-identical to the rendering before errors were bounded.
    assert result.error == (
        "Traceback (most recent call last):\n"
        '  File "<cell>", line 2, in <module>\n'
        "KeyError: 'missing'\n"
    )
    assert result.error_full == ""
    assert result.error_summary == "KeyError: 'missing'"
    outcome, ctx = await _run_python(tmp_path, "x = {}\nx['missing']\n")
    assert "KeyError: 'missing'" in outcome.text and "saved to" not in outcome.text
    assert spill.session_stats(ctx) == {}


async def test_a_thousand_link_chain_is_bounded_not_lost(tmp_path) -> None:
    code = (
        "e = None\n"
        "for i in range(1200):\n"
        "    try:\n"
        "        raise ValueError(f'link {i}') from e\n"
        "    except ValueError as x:\n"
        "        e = x\n"
        "raise RuntimeError('final ' + 'z' * 3000) from e\n"
    )
    result = await _run_cell(_registry(), _ctx(tmp_path), code)
    assert len(result.error) < cm_truncate.ERROR_CAP + 400
    assert result.error_summary.startswith("RuntimeError: final zzz")
    assert result.error.startswith("[the cell ended with: RuntimeError: final")
    assert "unrepresentable" not in result.error
    assert "ValueError: link 1199" in result.error_full
    assert len(result.error_full) > 100_000


def test_the_summary_scrubs_home_paths_like_the_error(monkeypatch) -> None:
    # The cell cannot import os, so drive the formatter in-process with a
    # known home; the kernel runs this same function.
    from fluid.codemode import _runner

    monkeypatch.setattr(_runner, "_HOME_DIR", "/home/someone")
    try:
        raise ValueError("/home/someone/proj/app.py is broken")
    except ValueError as exc:
        err, _full, summary = _runner._format_cell_traceback(exc)
    assert err.endswith("ValueError: ~/proj/app.py is broken\n")
    assert summary == "ValueError: ~/proj/app.py is broken"


async def test_a_capped_error_spill_says_only_its_head_and_tail_were_kept(tmp_path) -> None:
    code = "raise ValueError('h' * 10 + 'x' * 4_400_000 + 't' * 10)\n"
    outcome, _ = await _run_python(tmp_path, code)
    half = cm_truncate.FULL_STDOUT_MAX // 2
    assert f"its first and last {half:,} chars saved to" in outcome.text
    assert "all " not in outcome.text.split("cell error clipped;")[1][:5]
    saved = Path(re.search(r"chars saved to (.+?\.txt)", outcome.text).group(1))
    body = saved.read_text(encoding="utf-8")
    assert "chars of the error not kept]" in body and "cell stdout" not in body
    # A normal clipped error keeps the exact wording.
    outcome, _ = await _run_python(tmp_path, "raise ValueError('y' * 30000)\n")
    assert re.search(r"\[cell error clipped; all [\d,]+ chars saved to ", outcome.text)


def test_a_failed_clip_falls_back_to_the_whole_rendering(monkeypatch) -> None:
    from fluid.codemode import _runner

    def boom(_te) -> None:
        raise RecursionError("too deep")

    monkeypatch.setattr(_runner, "_clip_parts", boom)
    try:
        raise ValueError("v" * 30000)
    except ValueError as exc:
        err, full, summary = _runner._format_cell_traceback(exc)
    assert err == full and "v" * 30000 in full
    assert summary.startswith("ValueError: vvv")
    assert len(cm_truncate.bound_error(err, summary)) < cm_truncate.ERROR_CAP + 400
