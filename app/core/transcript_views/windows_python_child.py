"""Site-free Windows startup gate; the parent releases it only after Job admission."""
from __future__ import annotations

import os
import sys

# -I -S: no installed/user site hook runs before Job Object attachment. Use a
# raw one-byte read so no protocol bytes get stranded in a prefetched buffer.
# A Windows venv redirector may be our transport's PID, not this interpreter.
os.write(1, os.getpid().to_bytes(4, "big"))
if os.read(0, 1) != b"G":
    os._exit(70)

sys.argv = sys.argv[1:]
import site  # noqa: E402

site.main()  # Restore normal isolated-interpreter site behavior, now owned.
import runpy  # noqa: E402

runpy.run_path(sys.argv[0], run_name="__main__")
