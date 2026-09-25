"""Child process for the concurrent first-start tests (not collected by pytest).

    python schema_init_child.py <database file> <start-flag file>
    python schema_init_child.py --postgres <start-flag file>

Waits for the start flag (so every child reaches start-up together), then runs
the real entry point and exits 0. Any exception propagates, giving a non-zero
exit and a traceback on stderr.

SQLite mode runs ``init_database`` against the shared file through the test
shim. ``--postgres`` runs the worker's whole database start-up,
``app.core.startup.init_core`` (schema, migrations, mobile tables, seeds),
against the cluster named by ``managed-postgres.json`` in ``DATA_DIR``; the
parent sets ``DATA_DIR``/``WORKSPACE_DIR``/``TESTING=0``. It imports the app
first and then touches ``<start flag>.ready.<pid>``, so the parent can release
all children at once after the slow imports.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _wait_for(start_flag: str) -> None:
    deadline = time.monotonic() + 60
    while not os.path.exists(start_flag):
        if time.monotonic() > deadline:
            raise SystemExit("start flag never appeared")
        time.sleep(0.005)


def main(db_path: str, start_flag: str) -> None:
    sys.path.insert(0, str(REPO_ROOT))
    os.environ["TESTING"] = "1"
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="shuffle-schema-child-")

    from tests.db import schema_lifecycle_helpers as lifecycle

    _wait_for(start_flag)
    # A generous busy timeout: the loser waits for the winner's whole init.
    raw = lifecycle.connect(db_path, timeout=90)
    try:
        lifecycle.boot(raw)
    finally:
        raw.close()


def main_postgres(start_flag: str) -> None:
    sys.path.insert(0, str(REPO_ROOT))
    os.chdir(REPO_ROOT)

    from app.core import startup

    Path(f"{start_flag}.ready.{os.getpid()}").touch()
    _wait_for(start_flag)
    startup.init_core()


if __name__ == "__main__":
    if sys.argv[1] == "--postgres":
        main_postgres(sys.argv[2])
    else:
        main(sys.argv[1], sys.argv[2])
