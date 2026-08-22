"""Textual TUI — optional client over the same EventBus (``fluid chat --tui``).

The textual import is guarded: the core CLI (REPL) must work with no extra deps,
so textual is only required for this file. If it is not installed, :func:`run_tui`
prints a clear install hint and returns non-zero rather than raising.

The app is deliberately modest: a scrolling transcript pane fed by a background
task that subscribes to ``engine.bus``, plus an input box that submits
``UserTurn`` ops. Permission requests surface in the transcript and are answered
with ``y``/``n`` typed into the same input box.
"""

from __future__ import annotations

import sys

try:  # pragma: no cover - trivial import guard
    import textual  # noqa: F401

    _HAVE_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAVE_TEXTUAL = False

_INSTALL_HINT = (
    "The TUI needs the optional 'textual' dependency.\n"
    "Install it with:  pip install 'fluid[tui]'\n"
    "Falling back to the plain REPL is available with 'fluid chat' (no --tui)."
)


def textual_available() -> bool:
    return _HAVE_TEXTUAL


async def run_tui(engine, session_id: str) -> int:
    """Run the Textual app. Returns a process exit code."""
    if not _HAVE_TEXTUAL:
        print(_INSTALL_HINT, file=sys.stderr)
        return 1

    from textual.app import App, ComposeResult
    from textual.containers import VerticalScroll
    from textual.widgets import Input, RichLog

    from fluid.cli.repl import render_event
    from fluid.events.model import PermissionRequest
    from fluid.events.ops import RespondPermission, UserTurn
    from fluid.permissions.engine import PROMPT_DENIAL

    class _LogWriter:
        """Adapt a RichLog to the ``write``/``flush`` surface render_event uses."""

        def __init__(self, log: "RichLog") -> None:
            self._log = log
            self._buf = ""

        def write(self, text: str) -> None:
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._log.write(line)

        def flush(self) -> None:
            if self._buf:
                self._log.write(self._buf)
                self._buf = ""

    class FluidTUI(App):
        CSS = "RichLog { height: 1fr; } Input { dock: bottom; }"

        def __init__(self) -> None:
            super().__init__()
            self.engine = engine
            self.session_id = session_id
            self._pending_perm: str | None = None

        def compose(self) -> ComposeResult:
            with VerticalScroll():
                yield RichLog(highlight=False, markup=False, wrap=True, id="transcript")
            yield Input(placeholder="message (or y/n for a permission prompt)…", id="prompt")

        def on_mount(self) -> None:
            self._log = self.query_one("#transcript", RichLog)
            self._writer = _LogWriter(self._log)
            self.run_worker(self._pump_events(), exclusive=False)

        async def _pump_events(self) -> None:
            async for event in self.engine.bus.subscribe(self.session_id):
                render_event(event, self._writer)
                self._writer.flush()
                if isinstance(event, PermissionRequest):
                    self._pending_perm = event.request_id

        async def on_input_submitted(self, message: Input.Submitted) -> None:
            text = message.value.strip()
            self.query_one("#prompt", Input).value = ""
            if not text:
                return
            if self._pending_perm is not None:
                decision = "allow" if text.lower() in ("y", "yes") else "deny"
                await self.engine.submit(
                    RespondPermission(
                        request_id=self._pending_perm, decision=decision,
                        message=PROMPT_DENIAL if decision == "deny" else None,
                    )
                )
                self._pending_perm = None
                return
            if text in ("/exit", "/quit"):
                self.exit()
                return
            await self.engine.submit(UserTurn(content=text))

    await FluidTUI().run_async()
    return 0
