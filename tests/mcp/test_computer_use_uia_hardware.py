"""Hardware smoke test for UI Automation element targeting (Windows only, opt-in).

Set COMPUTER_USE_UIA_SMOKE=1 on a Windows desktop session with comtypes
installed. It opens its own small WinForms window (shown without taking focus)
with two fields that share a name, a checkbox, a password field and a button,
then drives the real comtypes backend and the real tool server against it.
Pointer and keyboard are fakes: nothing moves the mouse or types, and only
pattern actions (Value, Toggle, Invoke) reach the window. Other windows are
only read, to time tree reads against the budget. Everything it starts is
closed again.
"""

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("COMPUTER_USE_UIA_SMOKE") != "1",
    reason="hardware smoke test: Windows with COMPUTER_USE_UIA_SMOKE=1 only")

FORM = r"""
param([string]$Title, [int]$Rows = 0)
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type -ReferencedAssemblies System.Windows.Forms, System.Drawing -TypeDefinition @"
using System.Windows.Forms;
public class QuietForm : Form {
    protected override bool ShowWithoutActivation { get { return true; } }
}
"@
$f = New-Object QuietForm
$f.Text = $Title; $f.Width = 380; $f.Height = 240
$f.StartPosition = 'Manual'; $f.Location = New-Object Drawing.Point(60, 60)
function Add-Box($y) {
  $t = New-Object Windows.Forms.TextBox
  $t.AccessibleName = 'Amount'; $t.Location = New-Object Drawing.Point(20, $y); $t.Width = 200
  $f.Controls.Add($t); $t
}
$t1 = Add-Box 15
$t2 = Add-Box 45
$c = New-Object Windows.Forms.CheckBox; $c.Text = 'Agree'; $c.Location = New-Object Drawing.Point(20, 80)
$s = New-Object Windows.Forms.TextBox; $s.AccessibleName = 'PIN'; $s.UseSystemPasswordChar = $true
$s.Text = 'hunter2'; $s.Location = New-Object Drawing.Point(20, 110); $s.Width = 100
$l = New-Object Windows.Forms.Label; $l.Text = 'idle'; $l.Name = 'status'; $l.Location = New-Object Drawing.Point(140, 145)
$b = New-Object Windows.Forms.Button; $b.Text = 'Submit'; $b.Location = New-Object Drawing.Point(20, 140)
$b.Add_Click({ $l.Text = 'clicked' })
$z = New-Object Windows.Forms.Button; $z.Text = 'Freeze'; $z.Location = New-Object Drawing.Point(20, 170)
$z.Add_Click({ Start-Sleep -Seconds 6 })
$f.Controls.AddRange(@($c, $s, $l, $b, $z))
if ($Rows -gt 0) {
  $tv = New-Object Windows.Forms.TreeView; $tv.Location = New-Object Drawing.Point(240, 10)
  $tv.Width = 120; $tv.Height = 180
  for ($i = 0; $i -lt $Rows; $i++) {
    $n = $tv.Nodes.Add("group $i")
    for ($j = 0; $j -lt 20; $j++) { [void]$n.Nodes.Add("item $i.$j") }
  }
  $tv.ExpandAll(); $f.Controls.Add($tv)
}
[Windows.Forms.Application]::Run($f)
"""


def payload(result):
    return json.loads(result["content"][0]["text"])


def _open_form(tmp_path, rows=0):
    from app.mcp.computer_use import scaling, windows
    script = tmp_path / "form.ps1"
    script.write_text(FORM, encoding="utf-8")
    title = f"UIA smoke {os.getpid()} {time.time_ns()}"
    proc = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                             "-File", str(script), title, str(rows)],
                            creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        win = None
        for _ in range(150):
            time.sleep(0.2)
            found = [w for w in windows.list_windows(scaling.enumerate_displays())
                     if w.title == title]
            if found:
                win = found[0]
                break
        assert win is not None, "the test window did not appear"
        time.sleep(0.3)
        yield SimpleNamespace(win=win, proc=proc)
    finally:
        proc.kill()
        proc.wait(10)


@pytest.fixture
def form(tmp_path):
    yield from _open_form(tmp_path)


@pytest.fixture
def big_form(tmp_path):
    yield from _open_form(tmp_path, rows=60)  # 60 groups of 20 items: 1,260 tree nodes


def test_backend_reads_and_acts_on_its_own_window(form):
    from app.mcp.computer_use import uia
    backend = uia.Backend()
    check = lambda: None  # noqa: E731
    try:
        start = time.perf_counter()
        els, why = uia.read_tree(backend, form.win.id, check)
        print(f"\ntree: {len(els)} elements in {time.perf_counter() - start:.3f} s (cold)")
        assert why is None
        left, top, width, height = form.win.bounds
        for el in els:
            if el.rect:
                x0, y0, x1, y1 = el.rect
                assert left - 2 <= x0 < x1 <= left + width + 2, el
                assert top - 2 <= y0 < y1 <= top + height + 2, el
        fields = [e for e in els if e.control_type == "Edit" and e.name == "Amount"]
        assert len(fields) == 2 and fields[0].rect[1] < fields[1].rect[1]
        pin = next(e for e in els if e.name == "PIN")
        assert pin.password and pin.value is None
        result = uia.act(backend, form.win.id, fields[1], route_for=lambda e: "set_value",
                         value="42.50", check=check, on_dispatch=lambda: None)
        assert result.after.value == "42.50"
        now = [e.value for e in uia.read_tree(backend, form.win.id, check)[0] if e.name == "Amount"]
        assert now == ["", "42.50"]
        box = next(e for e in els if e.control_type == "CheckBox")
        result = uia.act(backend, form.win.id, box, route_for=uia.choose_route, value=None,
                         check=check, on_dispatch=lambda: None)
        assert result.route == "toggle" and result.after.toggle_state == "on"
        button = next(e for e in els if e.name == "Submit")
        result = uia.act(backend, form.win.id, button, route_for=uia.choose_route, value=None,
                         check=check, on_dispatch=lambda: None)
        assert result.route == "invoke"
        time.sleep(0.2)
        labels = [e.name for e in uia.read_tree(backend, form.win.id, check)[0]
                  if e.control_type == "Text"]
        assert "clicked" in labels
        # The window was shown without focus, so another app may cover it.
        hit = uia.window_at(backend, *button.center(), check)
        print(f"window under the button: {hit} (test window {form.win.id})")
        assert isinstance(hit, int)
        form.proc.kill()
        form.proc.wait(10)
        time.sleep(0.5)
        with pytest.raises(uia.ActionError) as err:
            uia.act(backend, form.win.id, button, route_for=uia.choose_route, value=None,
                    check=check, on_dispatch=lambda: None)
        assert err.value.code == "TARGET_CHANGED"
    finally:
        backend.shutdown()


def test_tool_server_end_to_end_without_input(form, monkeypatch):
    """The real server, windows, capture and UIA; fake pointer and keyboard."""
    from app.mcp import computer_use_sdk as sdk
    from app.mcp.computer_use import abort, access, safety, uia
    mouse, keyboard = MagicMock(), MagicMock()
    mouse.position = (5, 5)
    monkeypatch.setitem(sys.modules, "pynput.mouse", SimpleNamespace(
        Controller=lambda: mouse, Button=SimpleNamespace(left="left", right="right", middle="middle")))
    monkeypatch.setitem(sys.modules, "pynput.keyboard", SimpleNamespace(
        Controller=lambda: keyboard, Key=SimpleNamespace(esc="esc", ctrl="ctrl", alt="alt")))
    monkeypatch.setattr("app.api.computer_use.get_computer_use_enabled", lambda: True)
    monkeypatch.setattr("app.mcp.computer_use.overlay.ensure_started", lambda: None)
    monkeypatch.setattr(safety, "ensure_started", lambda: True)
    monkeypatch.setattr("app.mcp.sdk_compat.patch_sdk_server",
                        lambda cfg, tools: {t.name: t.handler for t in tools})
    backend = uia.Backend()
    monkeypatch.setattr(sdk, "_uia_backend", lambda: backend)
    session = "uia-smoke"
    abort.resume(session)
    tools = sdk.create_computer_use_server(enabled=True, permission_mode="bypassPermissions",
                                           session_id=session)
    import asyncio

    async def run():
        access.grant(session, ["Windows PowerShell"])
        await tools["switch_display"]({"display": str(form.win.display)})
        shot = payload(await tools["screenshot"]({}))["screenshot_id"]
        body = payload(await tools["ui_tree"]({"window_id": form.win.id, "interactive_only": True}))
        assert body["success"], body
        amounts = [e for e in body["elements"] if e["name"] == "Amount"]
        assert all(e["screenshot_bounds"] for e in amounts)
        assert "value" not in next(e for e in body["elements"] if e["name"] == "PIN")
        result = payload(await tools["set_value"]({"element_id": amounts[1]["element_id"],
                                                   "value": "7"}))
        assert result["value_matches"] is True, result
        box = next(e for e in body["elements"] if e["type"] == "CheckBox")
        batch = await tools["computer_batch"]({"actions": [
            {"action": "click_element", "element_id": box["element_id"]},
            {"action": "click_element", "element_id": next(
                e["element_id"] for e in body["elements"] if e["name"] == "Submit")}]})
        assert not batch.get("isError"), batch
        assert [json.loads(c["text"])["route"] for c in batch["content"]] == ["toggle", "invoke"]
        await tools["screenshot"]({})
        stale = payload(await tools["click_element"]({"element_id": box["element_id"]}))
        assert stale["code"] == "STALE_OBSERVATION"
        mouse.press.assert_not_called()
        keyboard.type.assert_not_called()
        return shot

    try:
        asyncio.run(run())
    finally:
        abort.request_stop(session, reason="completed")
        access.revoke(session)
        backend.shutdown()


def test_hung_app_is_bounded_by_the_budget(form):
    """A click handler that blocks the app's UI thread: the action and a tree
    read both come back within the budget, coded, and the app recovers."""
    from app.mcp.computer_use import uia
    backend = uia.Backend()
    check = lambda: None  # noqa: E731
    try:
        els, _ = uia.read_tree(backend, form.win.id, check)
        freeze = next(e for e in els if e.name == "Freeze")
        start = time.perf_counter()
        with pytest.raises(uia.ActionError) as err:
            uia.act(backend, form.win.id, freeze, route_for=uia.choose_route, value=None,
                    check=check, on_dispatch=lambda: None)
        took = time.perf_counter() - start
        print(f"\nhung invoke: {err.value.code}/{err.value.outcome} after {took:.2f} s")
        assert err.value.code == "EXECUTION_OUTCOME_UNKNOWN" and err.value.outcome == "unknown"
        assert took < uia.CALL_BUDGET_S + 0.5
        start = time.perf_counter()
        try:
            uia.read_tree(backend, form.win.id, check)
            outcome = "answered"
        except uia.ActionError as exc:
            outcome = exc.code
        took = time.perf_counter() - start
        print(f"tree of the hung app: {outcome} after {took:.2f} s")
        assert took < uia.CALL_BUDGET_S + 0.5
        time.sleep(6)
        assert uia.read_tree(backend, form.win.id, check)[0]  # it recovers
    finally:
        backend.shutdown()


def test_large_tree_is_capped_within_budget(big_form):
    from app.mcp.computer_use import uia
    backend = uia.Backend()
    try:
        for _ in range(2):  # cold, then warm
            start = time.perf_counter()
            els, why = uia.read_tree(backend, big_form.win.id, lambda: None)
            took = time.perf_counter() - start
            print(f"\nlarge tree: {len(els)} elements, truncated={why}, {took:.3f} s")
            assert len(els) <= uia.MAX_ELEMENTS and why in ("count", "children", "time")
            assert took < uia.CALL_BUDGET_S
            assert max(e.depth for e in els) <= uia.MAX_DEPTH
    finally:
        backend.shutdown()


def test_reading_other_windows_stays_within_budget():
    """Read-only: every listed window's tree comes back capped, in budget, or
    as a coded error. Nothing is acted on."""
    from app.mcp.computer_use import scaling, uia, windows
    backend = uia.Backend()
    try:
        for win in windows.list_windows(scaling.enumerate_displays()):
            if not win.bounds:
                continue
            start = time.perf_counter()
            try:
                els, why = uia.read_tree(backend, win.id, lambda: None)
                outcome = f"{len(els)} elements, truncated={why}"
                assert len(els) <= uia.MAX_ELEMENTS
            except uia.ActionError as err:
                outcome = f"{err.code}"
            took = time.perf_counter() - start
            print(f"\n{win.app!r}: {outcome} in {took:.3f} s")
            assert took < uia.CALL_BUDGET_S + 0.5
    finally:
        backend.shutdown()
