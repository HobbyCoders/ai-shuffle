"""Child process for the concurrent first-start test (not collected by pytest).

    python schema_init_child.py <database file> <start-flag file>

Waits for the start flag (so both children reach ``init_database`` together),
then runs the real entry point against the shared SQLite file and exits 0.
Any exception propagates, giving a non-zero exit and a traceback on stderr.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def main(db_path: str, start_flag: str) -> None:
    sys.path.insert(0, str(REPO_ROOT))
    os.environ["TESTING"] = "1"
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="shuffle-schema-child-")

    from tests.db import schema_lifecycle_helpers as lifecycle

    deadline = time.monotonic() + 60
    while not os.path.exists(start_flag):
        if time.monotonic() > deadline:
            raise SystemExit("start flag never appeared")
        time.sleep(0.01)
    # A generous busy timeout: the loser waits for the winner's whole init.
    raw = lifecycle.connect(db_path, timeout=90)
    try:
        lifecycle.boot(raw)
    finally:
        raw.close()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
