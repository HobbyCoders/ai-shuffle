"""
Pseudo-terminal helper for stock CLI commands that want a terminal.

:class:`PtyProcess` runs one command on a pseudo-terminal (the CLI's own
``claude auth login``, see ``app/core/claude_cli_login.py``). The session
bridge that drove ``/resume`` over a PTY went with the CLI bridge socket.

NOTE: PTY functionality is only available on Unix-like systems.
On Windows, :func:`is_pty_available` is False and :class:`PtyProcess` refuses.
"""

import os
import sys
import threading
from typing import Optional

# PTY-related imports are Unix-only
PTY_AVAILABLE = False
if sys.platform != 'win32':
    try:
        import pty
        import select
        import struct
        import fcntl
        import termios
        import signal
        PTY_AVAILABLE = True
    except ImportError:
        pass


def is_pty_available() -> bool:
    """Check if PTY functionality is available on this platform."""
    return PTY_AVAILABLE


class PtyProcess:
    """One command on a pseudo-terminal, read by a background thread.

    Used for commands that want a terminal but no session, such as the
    stock CLI's own ``claude auth login``. The reader thread keeps working
    whichever event loop (or none) the caller runs on. Unix only.
    """

    def __init__(self, argv: list, env: dict, cwd: Optional[str] = None,
                 cols: int = 200, rows: int = 24, echo: bool = True):
        if not PTY_AVAILABLE:
            raise RuntimeError("A terminal is not available on this platform.")
        self.argv = list(argv)
        self._chunks: list = []
        self._chunks_lock = threading.Lock()
        self._exit_code: Optional[int] = None
        self._done = threading.Event()
        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                if cwd:
                    os.chdir(cwd)
                if not echo:
                    attrs = termios.tcgetattr(0)
                    attrs[3] &= ~termios.ECHO
                    termios.tcsetattr(0, termios.TCSANOW, attrs)
                os.execvpe(self.argv[0], self.argv, env)
            finally:
                os._exit(127)
        self.pid, self.fd = pid, fd
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self) -> None:
        try:
            while True:
                try:
                    readable, _, _ = select.select([self.fd], [], [], 0.25)
                except (OSError, ValueError):
                    break
                if not readable:
                    if self._reap(block=False):
                        break
                    continue
                try:
                    data = os.read(self.fd, 4096)
                except OSError:  # EIO once the child is gone
                    break
                if not data:
                    break
                with self._chunks_lock:
                    self._chunks.append(data)
        finally:
            self._reap(block=True)
            try:
                os.close(self.fd)
            except OSError:
                pass
            self._done.set()

    def _reap(self, block: bool) -> bool:
        if self._exit_code is not None:
            return True
        try:
            pid, status = os.waitpid(self.pid, 0 if block else os.WNOHANG)
        except ChildProcessError:
            self._exit_code = self._exit_code if self._exit_code is not None else -1
            return True
        if pid == 0:
            return False
        self._exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
        return True

    def take_output(self) -> bytes:
        """Output read since the last call."""
        with self._chunks_lock:
            chunks, self._chunks = self._chunks, []
        return b"".join(chunks)

    def write(self, data: str) -> None:
        if self.running:
            os.write(self.fd, data.encode("utf-8"))

    @property
    def running(self) -> bool:
        return not self._done.is_set()

    @property
    def exit_code(self) -> Optional[int]:
        return self._exit_code if self._done.is_set() else None

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._done.wait(timeout)

    def terminate(self, grace_seconds: float = 2.0) -> None:
        if not self.running:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(self.pid, sig)
            except ProcessLookupError:
                break
            if self._done.wait(grace_seconds):
                break
