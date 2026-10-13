"""The in-app terminal for the stock CLI's own login: ``claude auth login``.

The user signs in to Claude Code the same way they would in any terminal: the
CLI prints Anthropic's sign-in link, the user opens it, and pastes the code the
page shows back into the CLI. AI Shuffle only runs the command on a
pseudo-terminal, shows what it prints and passes typed lines to it. It never
reads, stores or forwards anything the CLI writes to its config dir.

- One login at a time per worker. Starting again while one runs returns it.
- Only ``claude auth login`` runs here; there is no general shell.
- Echo is off on the terminal, so a pasted code never lands in the output kept
  in memory; typed input is written to the CLI and not kept or logged.
- Output lives in memory only, is cleared when a new login starts, and is
  dropped a few minutes after the command exits. An unattended login is
  stopped after 15 minutes.
- Unix only (Docker and Linux or macOS local runs). The desktop app and
  Windows show the command to run in the user's own terminal instead.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from typing import Optional

from fluid.tools.child_env import scrub_child_env

from app.core import claude_cli_status
from app.core.cli_bridge import PtyProcess, is_pty_available

logger = logging.getLogger(__name__)

LOGIN_ARGS = ("auth", "login")
MAX_INPUT_CHARS = 4096
_MAX_OUTPUT_CHARS = 64 * 1024
_IDLE_LIMIT_SECONDS = 15 * 60
_KEEP_AFTER_EXIT_SECONDS = 5 * 60

_ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC (hyperlinks, titles)
    r"|\x1b\[[0-?]*[ -/]*[@-~]"            # CSI
    r"|\x1b[@-Z\\-_]"                      # other two-byte escapes
)
_URL_RE = re.compile(r"https://[^\s\x1b\x07<>\"']+")


def clean_terminal_text(text: str) -> str:
    """Terminal output as plain text: escapes removed, CRLF normalized."""
    text = _ANSI_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(ch for ch in text if ch in "\n\t" or ch >= " ")


def terminal_supported() -> bool:
    """Whether this worker offers the in-app login terminal.

    Not on Windows (no PTY) and not in the desktop app, whose worker uses the
    user's own ``claude`` login from their own terminal.
    """
    if sys.platform == "win32" or not is_pty_available():
        return False
    return os.environ.get("SHUFFLE_DESKTOP") != "1"


def _login_env() -> dict:
    env = {k: v for k, v in scrub_child_env().items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")}
    env["TERM"] = "dumb"
    env["NO_COLOR"] = "1"
    return env


class LoginTerminal:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[PtyProcess] = None
        self._text = ""
        self._base = 0          # absolute offset of self._text[0]
        self._url: Optional[str] = None
        self._started_at = 0.0
        self._ended_at: Optional[float] = None
        self._exit_code: Optional[int] = None
        self._error: Optional[str] = None

    # -- internal ---------------------------------------------------------
    def _pump(self) -> None:
        proc = self._proc
        if proc is None:
            return
        chunk = proc.take_output()
        if chunk:
            text = clean_terminal_text(chunk.decode("utf-8", errors="replace"))
            self._text += text
            if len(self._text) > _MAX_OUTPUT_CHARS:
                drop = len(self._text) - _MAX_OUTPUT_CHARS
                self._text = self._text[drop:]
                self._base += drop
            if self._url is None:
                # Search the cleaned text so a link split across reads, or
                # wrapped in a terminal hyperlink escape, is still found whole
                # once its line has ended.
                for line in self._text.split("\n")[:-1]:
                    match = _URL_RE.search(line)
                    if match:
                        self._url = match.group(0).rstrip(".,;)")
                        break
        if not proc.running and self._ended_at is None:
            proc.wait(1)
            self._exit_code = proc.exit_code
            self._ended_at = time.monotonic()
            claude_cli_status.invalidate()
        elif proc.running and time.monotonic() - self._started_at > _IDLE_LIMIT_SECONDS:
            proc.terminate()
            self._error = "The sign-in was stopped after 15 minutes. Start it again when you are ready."

    def _expire(self) -> None:
        if (self._ended_at is not None
                and time.monotonic() - self._ended_at > _KEEP_AFTER_EXIT_SECONDS):
            self._reset()

    def _reset(self) -> None:
        self._proc = None
        self._text, self._base, self._url = "", 0, None
        self._ended_at = self._exit_code = self._error = None

    def _state(self, offset: int = 0) -> dict:
        proc = self._proc
        start = max(0, offset - self._base)
        return {
            "supported": terminal_supported(),
            "active": proc is not None,
            "running": bool(proc and proc.running),
            "exit_code": self._exit_code,
            "succeeded": self._exit_code == 0 if self._ended_at is not None else None,
            "output": self._text[start:],
            "offset": self._base + len(self._text),
            "reset": offset < self._base or offset > self._base + len(self._text),
            "url": self._url,
            "error": self._error,
        }

    # -- public -----------------------------------------------------------
    def start(self, binary: Optional[str] = None) -> dict:
        with self._lock:
            self._pump()
            if self._proc is not None and self._proc.running:
                return self._state()
            if not terminal_supported():
                raise RuntimeError("Sign in from your own terminal on this machine: run `claude auth login`.")
            if binary is None:
                binary = claude_cli_status.get_status(force=True).binary_path
                if not binary:
                    from fluid.engine.delegated import ClaudeCliMissingError
                    raise ClaudeCliMissingError()
            self._reset()
            cwd = os.environ.get("HOME") or None
            self._proc = PtyProcess([binary, *LOGIN_ARGS], env=_login_env(), cwd=cwd, echo=False)
            self._started_at = time.monotonic()
            logger.info("Started `claude auth login` in the in-app terminal")
            return self._state()

    def read(self, offset: int = 0) -> dict:
        with self._lock:
            self._pump()
            self._expire()
            return self._state(offset)

    def send(self, text: str, offset: int = 0) -> dict:
        if not isinstance(text, str) or len(text) > MAX_INPUT_CHARS:
            raise ValueError("Input is too long.")
        line = text.replace("\r", "").replace("\n", "")
        with self._lock:
            self._pump()
            if self._proc is None or not self._proc.running:
                raise RuntimeError("The sign-in is not running.")
            self._proc.write(line + "\r")
            self._started_at = time.monotonic()
            return self._state(offset)

    def cancel(self) -> dict:
        with self._lock:
            proc = self._proc
            if proc is not None and proc.running:
                proc.terminate()
            self._pump()
            self._reset()
            claude_cli_status.invalidate()
            return self._state()


login_terminal = LoginTerminal()
