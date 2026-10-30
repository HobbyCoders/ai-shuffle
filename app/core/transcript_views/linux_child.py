"""Bind this exec'd projection child to its creating parent without needing the GIL."""
from __future__ import annotations

import ctypes
import os
import signal
import sys


def main() -> None:
    expected_parent = int(sys.argv[1])
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), "projection_parent_ownership_failed")
    # The parent may have died before prctl. Never attach to a replacement
    # parent (including PID 1); the signal remains armed across ordinary exec.
    if os.getppid() != expected_parent:
        os.kill(os.getpid(), signal.SIGKILL)
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)


if __name__ == "__main__":
    main()
