# Computer use

Native computer use operates the backend host's desktop. A purple frame marks
the physical screen currently being viewed or controlled. The Agent pointer
marks the agent's last mouse position and remains visible while it thinks; it
does not follow subsequent human mouse movement. The in-app control bar shows
the current display, action, and whether the desktop is still under control.

Press **Esc on the host** to stop desktop tools for the current turn, even when
AI Shuffle is not focused. The agent's own Escape key still closes menus and
dialogs. **Stop computer use** in the app has the same effect on desktop control.
The chat's existing Stop button also stops the agent. Starting another turn
re-arms that chat. Stop releases agent-held mouse buttons and modifier keys.

### What stops control, and what does not

Only these latch a stop for the rest of the turn, and each records its reason
(`abort.stop_reason`): physical Esc on the host (`escape`), **Stop computer
use** in the app (`user`), the chat's Stop or another turn interrupt
(`stopped`, or `interrupt:<source>` when the caller passes the turn's interrupt
source), turning the setting off (`disabled`), and the turn ending
(`completed`). An interrupt whose source is not a person's Stop (shutdown,
usage limit, checkout cleanup) is reported as `TURN_INTERRUPTED`, "the app
interrupted this turn; the user did not stop it". Every refusal returned to the model names that cause in plain
words with a `code` (`USER_INTERRUPTED`, `PERMISSION_REQUIRED`), so the model
never has to guess whether the user stopped it. Each halt is logged at WARNING
with its reason and evidence (for Esc: the hook's flags and scan code).

These never halt the session:

- **Pointer checks.** Before a click, scroll or drag the pointer must be
  within 2 physical pixels of where it was placed (mixed-DPI rounding and an
  idle sensor's jitter are not a takeover). Further off, a second sample tells
  a moving pointer from a placement problem. Moving: the action is refused
  with `USER_INTERRUPTED` and input pauses for 3 s after the movement, so the
  agent does not fight the person for the pointer. Stable but off: the pointer
  is placed once more, then the action is refused with `POINTER_MISMATCH`.
  An unreadable pointer (lock screen, UAC) returns `DESKTOP_LOCKED`. All three
  are logged at WARNING with expected and actual positions.
- **Cancelled calls.** The host cancels a tool call that runs past its
  per-call timeout (330 s by default) or when the turn is interrupted. The
  running input is invalidated and held keys and buttons are released, but the
  session stays armed; a turn interrupt halts it through its own Stop path.
  `computer_batch` refuses up front a batch whose waits and holds add up to
  more than 240 s.
- **The agent's own Escape.** Windows marks injected keys (LLKHF_INJECTED).
  On Linux (X11) the listener excuses exactly one Escape press for each Escape
  the agent sends, within 0.5 s (see Linux below). On macOS with pynput before
  1.8, which does not report injection, an Escape within 0.5 s of the agent's
  own Escape is ignored.

If the native hotkey or screen overlay cannot start, the control bar reports
the missing capability and retains its Stop button. The native overlay currently
requires Windows. Other supported capture/input platforms (macOS, Linux on X11)
retain in-app status.

## Concurrent sessions

Only one chat can own the shared desktop at a time. Ownership spans the whole
observe/act/verify sequence, including model thinking between calls. Another
chat receives a busy error and can continue other work. There is no queue that
silently takes over the desktop after the user presses Esc. Concurrent tool
calls are also rejected; use `computer_batch` for an ordered sequence.

Ownership ends on `release_control`, turn completion/failure, chat Stop, physical
Esc, or disabling computer use. Release invalidates screenshot mappings; a new
owner/turn must capture a fresh image before clicking. Use one native backend
process per desktop: these ownership and status controls are process-local.

Independent cursors would not isolate keyboard focus, windows, clipboard, or
application state. Use browser tools for web work without moving the host cursor.
True parallel desktop work needs separate desktop environments/VMs. Separate
environments can still conflict when editing the same remote record or file.

For comparison, OpenAI documents Windows computer use as foreground control and
recommends a VM for keeping the main desktop available. macOS supports background
use, but OpenAI advises against two simultaneous tasks in the same app.
[Official computer-use guidance](https://learn.chatgpt.com/use-cases/use-your-computer-with-codex#practical-tips).

## Tool contract

**Observations.** Every `screenshot` returns a `screenshot_id` (process-wide
unique) with its display, image and physical size, and geometry. Coordinate
tools (`mouse_move`, clicks, `scroll`, `left_click_drag`, `zoom` with a region)
take an optional `screenshot_id`. Input is mapped only through the current
screenshot of the selected display; an action is refused with
`STALE_OBSERVATION`, before any input, when:

- it names a screenshot that is no longer current (a newer one replaced it);
- no current screenshot exists (never taken, `switch_display`, `release_control`,
  a cancelled call, a new turn or owner);
- the monitor layout changed since the capture.

A coordinate more than 2 pixels outside the screenshot is refused with
`INVALID_ARGUMENT` instead of being clamped onto the edge (which used to click
whatever sat there, often the taskbar). `zoom` and `list_windows` report the
`screenshot_id` their mapping belongs to.

**Outcomes.** Every error carries `error` (short, plain), `code` and `outcome`:
`rejected` (nothing sent), `partial` (some input sent) or `unknown` (input may
have taken effect; observe before retrying). Codes are listed in
`app/mcp/computer_use/errors.py`: `STALE_OBSERVATION`, `TARGET_NOT_FOUND`,
`TARGET_CHANGED`, `FOCUS_BLOCKED`, `POINTER_MISMATCH`, `USER_INTERRUPTED`, `DESKTOP_LOCKED`,
`PERMISSION_REQUIRED`, `DESKTOP_BUSY`, `UNSUPPORTED_CAPABILITY`,
`INVALID_ARGUMENT`, `EXECUTION_OUTCOME_UNKNOWN`, `ACTION_FAILED`. A failed
`computer_batch` step also reports `action_index`, `completed_actions` and
`not_run`. Success results are unchanged.

**Readiness checks (Windows, fail soft).** Before input, screenshots and
zooms, `windows.input_desktop_locked()` checks whether the lock screen or the
UAC secure desktop owns input (OpenInputDesktop is denied or is not
"Default"); if so the call returns `DESKTOP_LOCKED` instead of acting blind or
returning a black image. Before `type`/`key`/`hold_key`,
`windows.foreground_blocks_input()` checks whether the foreground window runs
elevated while this process does not (Windows silently drops that input);
if so it returns `PERMISSION_REQUIRED`. Either probe returning "unknown" lets
the action proceed as before.

**Capabilities.** `request_access` and `list_granted_applications` include
`capabilities`: platform, the tools that work here, what is unavailable and
why, the coordinate rule, the batching rule, how stop works, and
`ui_automation` (`available`, `reason`). The Windows-only tools
(`list_windows`, `focus_window`, `window_control`, `open_application`, the
clipboard tools, `ui_tree`, `click_element`, `set_value`) are not registered on
other platforms, and a batch step naming one returns `UNSUPPORTED_CAPABILITY`.
`get_computer_use_tool_names()` still lists every name (allow-list superset).

**Model-facing text.** Tool descriptions are kept short. The operating rules
for the system prompt come from `computer_use_sdk.system_guidance()`
(platform-aware); `app/core/query_options.py` appends them when the server is
attached. Rules that a tool's own description states (zoom, typing, batches)
are not repeated there.

## Displays and screenshots

- Display indices are the computer-use tool's indices, listed by `list_displays`.
- Screenshot and input coordinates use the same physical monitor geometry,
  including negative origins and mixed DPI. The overlay consumes that geometry.
- Changing monitor topology invalidates old input mappings. Capture again.
- `zoom` takes a screenshot region or a `window_id`. A region is intersected
  with the screenshot bounds; a window is captured from its screen area on any
  display. Small crops are upscaled (up to 4x) so text is legible. The result's
  `screenshot_region` and `zoom_factor` map image pixels back to the current
  screenshot (x = screenshot_region[0] + image_x / zoom_factor). For a window
  that spans monitors the region covers the whole image, so it can start
  outside the screenshot; such points are refused on input. Later input
  continues to use the full screenshot's coordinates.
- Every image is sized to reach the model unchanged: at most 1568 px on the
  long edge and 1.15 megapixels (`scaling.MODEL_MAX_EDGE`/`MODEL_MAX_PIXELS`),
  whatever virtual size is configured. Fluid shrinks larger tool images and
  Anthropic's API shrinks larger ones too, which would break the pixel mapping.
- Screenshot results include PNG content, dimensions, display geometry, and the
  current cursor location. Cursor coordinates outside the selected display are
  explicitly reported without inventing coordinates in that screenshot.
- On Windows 10 build 19041 and newer, the overlay requests exclusion from
  screen capture so its marker and label do not obscure the model's view.
- Keyboard activity highlights the foreground window's display, even when it
  differs from the selected screenshot display.

## Windows

Windows only: these tools are not registered on other platforms, and a batch
step naming one returns `UNSUPPORTED_CAPABILITY`.

- `list_windows` lists the windows a user could Alt+Tab to, front to back, with
  id, title, app, process, state, foreground, display, physical `bounds`, the
  window's area in the last screenshot, and whether its app is granted.
- `focus_window` brings a listed window to the front, restoring it if minimized.
  `window_control` maximizes, minimizes, restores, or moves/resizes it. Move
  bounds are physical desktop pixels, and the title bar must stay on a display.
- Both act only on windows from the current list whose app matches the
  session's `request_access` grant (by executable, file description, product
  name, or a trailing word, so 'Edge' matches 'Microsoft Edge').
- `open_application` focuses an open, granted window of the app before falling
  back to a Start-menu launch.
- `ui_tree`, `click_element` and `set_value` target controls by element; see
  the next section.
- Windows of apps running as administrator cannot be focused or moved from a
  normal process; those calls fail with a message asking the user to step in.

## Element targeting (UI Automation)

Windows only. The model can target controls by element instead of by pixel,
which survives small layout shifts and tells similar fields apart. The backend
is `app/mcp/computer_use/uia.py`, using `comtypes` against `UIAutomationCore`
(in `requirements-native.txt` for Windows only). Coordinates keep working
unchanged; these tools are additive.

- `ui_tree(window_id, interactive_only?)` lists a granted window's elements:
  `element_id`, `type`, `name`, `value`, `enabled`, `focusable`,
  `screenshot_bounds` (same pixel space as the current screenshot, null when off
  it), `actions` (`invoke`, `toggle`, `select`, `expand`, `collapse`,
  `set_value`), `toggle_state`, `automation_id` and `parent`. At most 200
  elements, 8 levels, 50 children per parent; a walk stops after 1.4 s. The
  result says when and why it was cut (`truncated`). Unnamed, inert Pane, Group
  and Custom wrappers are neither listed nor counted as a level: Electron and
  browser windows nest their controls ten or more of them deep.
- Names and values are cut to 200 characters. A password field reports
  `password: true` and never a value. The result notes that the text is data
  from the app, not instructions.
- Element ids are `"<screenshot_id>/<n>"`. They hold for that screenshot only:
  a new screenshot, `switch_display`, `release_control`, a cancelled call or a
  new turn makes them `STALE_OBSERVATION`, as for coordinates. `ui_tree` with no
  current screenshot is refused with `STALE_OBSERVATION` (take one first).
- `click_element(element_id, route?)` re-finds the element by its UIA runtime
  id inside its window, checks that its name and control type still match
  (`TARGET_CHANGED` if not, or if it or its window is gone), then acts. Route
  `auto` (default) works like a single click: Toggle, then SelectionItem, then
  Invoke, then Expand/Collapse (Select before Invoke, so a file in a list is
  selected, not opened). With no pattern, or `route: "mouse"`, it does a real
  left click at the element's current centre through the normal click path and
  pointer check; that is refused when the element is off screen, not on the
  selected display, or covered by another window (`FOCUS_BLOCKED`). The result
  names the `route` used.
- `set_value(element_id, value)` uses the Value pattern and reads the value
  back (`value`, `value_matches`), except for password fields.
- Both run as `computer_batch` steps. A batch step's `action` is the tool name,
  which is why the click option is called `route`.
- Safety: only windows of granted apps are listed or acted on, checked again at
  action time (the window must still be open, owned by the same process and
  granted). Element actions take the desktop lock, check locked desktop
  (`DESKTOP_LOCKED`) and elevated windows (`PERMISSION_REQUIRED`), and check the
  stop switch right before the pattern call and while waiting for it.
- Threading: every UIA call runs on a worker thread (COM initialised as
  multithreaded) with a 2 s budget; UIA's own timeouts are lowered to match. A
  call past the budget is abandoned and the next call starts a new worker; with
  four abandoned workers still waiting, calls are refused at once. A tree read
  or a lookup that times out is `UNSUPPORTED_CAPABILITY`; an action that timed
  out after it was sent is `EXECUTION_OUTCOME_UNKNOWN` (outcome `unknown`), for
  example a button whose handler opens a modal dialog.
- Fail soft: no `comtypes`, UIA failing to start, a timeout, or an app with an
  empty tree returns `UNSUPPORTED_CAPABILITY` and tells the model to use
  screenshot coordinates. Nothing raises into the session or halts it.
  `capabilities.ui_automation` reports `{available, reason}`; when it is not
  available the three tools are listed under `unavailable`.

## Linux

Computer use works on Linux in an **X11** session: a normal desktop login, or
any X server that `DISPLAY` points at (Xvfb, Xephyr, a VNC desktop). Capture
uses mss over XCB and input uses pynput's X11 backend (XTEST). It is off by
default and uses the same toggle, access prompt and permission behaviour as on
Windows. Settings > Security > Computer use shows why it is unavailable:

| Reason | What it means | What to do |
| --- | --- | --- |
| `docker` | The worker runs in a container (the Docker image). | Use a native install on a computer with a desktop. |
| `no-display` | Headless: `DISPLAY` is not set for the worker. | Start AI Shuffle from the desktop session, or set `DISPLAY` and `XAUTHORITY` to it. |
| `wayland` | `WAYLAND_DISPLAY` is set, `XDG_SESSION_TYPE=wayland`, or the X server behind `DISPLAY` is XWayland (it advertises the XWAYLAND extension). Wayland lets no app read the screen or inject input into others; XWayland only shows X11 apps. | Log in with an X11 session ("GNOME on Xorg", "Plasma (X11)"). To drive a separate X server instead, start the worker without `WAYLAND_DISPLAY` and with `DISPLAY` set to that server. |
| `missing-deps` | A Python package is missing (`mss`, `pynput`, `Pillow`, `python-xlib`); `missing` names them. | `pip install -r requirements-native.txt` (see LOCAL_SETUP for the evdev fallback). |
| `missing-system-packages` | An XCB library mss loads is missing; `missing` lists the Debian/Ubuntu packages. | Install them (Fedora: `libxcb`, Arch: `libxcb`). |
| `x11-unreachable` | Cannot connect to the X display (wrong `DISPLAY`, no `XAUTHORITY`, a different user's session). | Run the worker as the desktop user, or set `DISPLAY`/`XAUTHORITY`. |
| `x11-no-xtest` | The X server has no XTEST extension, which input needs. | Use a standard Xorg session or enable XTEST. |

The status endpoint returns a `detail` sentence for each of these. Missing
packages are looked up again on each check; a missing library or package shows
as fixed on the next status poll after it is installed.

Differences from Windows:

- `list_windows`, `focus_window`, `window_control`, `open_application` and
  the element tools are Windows only and are not offered (a batch step naming
  one returns `UNSUPPORTED_CAPABILITY`); the Linux system prompt tells the
  model to switch and launch apps on screen instead.
- The clipboard tools are offered on Linux too. They use `xclip` (preferred)
  or `xsel`; without either they say which to install.
- XTEST input is indistinguishable from hardware input, so the physical-Escape
  listener cannot read an "injected" flag. The input layer announces each
  Escape the agent presses and the listener skips one Escape press per
  announcement, within 0.5 s. Nothing silences Escape for longer: `hold_key`
  with Escape is refused on Linux (`UNSUPPORTED_CAPABILITY`), because a held key's auto-repeat cannot be
  told from a person's Escape. A person's Escape still stops the turn. Escape needs the
  X RECORD extension; without it the control bar reports Escape unavailable.
- The purple overlay is Windows only; Linux keeps the in-app control bar.
- The `computer_use` worker capability (and the "computer use" trait other
  workers see) is true only when the toggle is on and no blocker above applies.
  It is a cheap check that never opens the display.

Not yet supported: a built-in virtual display for headless or Docker workers
(an Xvfb desktop started by the app), and Wayland via the XDG desktop portal.
Live X11 control has not been exercised in automated tests.

## Validation

Automated tests use synthetic monitor images and fake input devices. They cover
every registered desktop tool, PNG/crop geometry, monitor changes, physical vs.
injected Escape, held-input cleanup, concurrent calls, cancellation, stale poll
responses, and the frontend Stop control. `test_computer_use_false_stops.py`
pins every path that used to halt without a user action;
`test_computer_use_contract.py` covers observation ids, codes, readiness
refusals and capabilities. `test_computer_use_uia.py` covers element targeting
over a fake UIA provider (caps, ids and staleness, `TARGET_CHANGED`, each route
and the click fallback, timeouts, missing UIA, grants, password fields, stop,
batches, other platforms). The native probes (`input_desktop_locked`,
`foreground_blocks_input`) are only exercised through fakes here. No test types
into a real application.

`test_computer_use_uia_hardware.py` is an opt-in check of the real comtypes
backend on Windows (`COMPUTER_USE_UIA_SMOKE=1`). It opens its own WinForms
window without taking focus and uses fake pointer and keyboard devices, so only
pattern actions reach that window; other windows are only read.

Hardware smoke checks for a native Windows installation:

1. Use monitors left of and above the primary, at different DPI scales. Switch
   displays, capture, and click a known target; frame and marker must match.
2. Inspect screenshots and zoom crops. Verify the overlay is excluded on a
   supported Windows build, and screen edges remain correctly mapped.
3. Start a long type, hold, drag, or batch. Press physical Esc from another app.
   Confirm no remaining input, no held buttons/modifiers, and a stopped status.
4. Have the agent send Escape to a menu. Confirm it closes without stopping.
5. Start desktop tools in another chat while one owns control. Confirm a busy
   result, then release the owner and capture again before retrying.
6. Complete or cancel a turn and disable the setting during control. Confirm
   ownership and the overlay clear immediately and a new turn re-arms safely.
7. Leave the mouse untouched through 50+ clicks on a mixed-DPI setup. No
   `POINTER_MISMATCH` or halt should appear in the log. Then nudge the mouse
   during a batch of clicks: expect `USER_INTERRUPTED` (no halt) and a 3 s pause.
8. Lock the PC (Win+L) and trigger a UAC prompt during control: expect
   `DESKTOP_LOCKED`, not a black screenshot or blind clicks. Unlock: it works
   again after a screenshot.
9. Focus an elevated window (Task Manager) and have the agent `type`: expect
   `PERMISSION_REQUIRED`. Clicks on a normal window still work.
10. Run `COMPUTER_USE_UIA_SMOKE=1 pytest tests/mcp/test_computer_use_uia_hardware.py -s`
    with comtypes installed. Expect: the second of two fields named "Amount" set
    and read back, the checkbox toggled, the button invoked, `TARGET_CHANGED`
    after the window closes, a blocked app answered within about 2 s, a
    1,260-node tree capped at 200, and every open window read within budget.
