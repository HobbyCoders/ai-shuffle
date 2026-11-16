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
  limit).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

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
