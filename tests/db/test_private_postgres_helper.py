"""The private PostgreSQL 16 cluster that backend tests start and stop.

Its teardown is the container supervisor's ``Database.close()``, which allows
30 s for a fast shutdown. The shutdown checkpoint used to fsync every relation
file the app schema had created (about 5,200 files, 25 s on a loaded disk), so
module teardown sometimes failed with "fast shutdown deadline exceeded". The
helper's cluster runs without fsync; this checks the checkpoint itself rather
than wall time, which would be flaky.
"""
import re

import pytest

from tests.db import schema_lifecycle_helpers as lifecycle


def test_private_cluster_shutdown_checkpoint_syncs_nothing(tmp_path):
    bindir = lifecycle.pg16_bindir()
    if bindir is None:
        pytest.skip("PostgreSQL 16 server binaries not found (PATH, SMOKE_PG_BIN or "
                    "/usr/lib/postgresql/16/bin)")
    data = tmp_path / "data"
    data.mkdir()
    with lifecycle.private_postgres(bindir, data) as descriptor:
        conn = lifecycle.pg_connect(descriptor)
        try:
            cur = conn.cursor()
            cur.execute("SHOW fsync")
            assert cur.fetchone()["fsync"] == "off"
            # Dirty many relation files, as creating the app schema does.
            for i in range(200):
                cur.execute(f"CREATE TABLE fsync_probe_{i} (id int PRIMARY KEY, body text)")
                cur.execute(f"INSERT INTO fsync_probe_{i} VALUES (1, 'x')")
        finally:
            conn.close()

    log = (data / "postgres" / "server.log").read_text()
    assert "database system is shut down" in log
    shutdown = [line for line in log.splitlines()
                if "checkpoint complete" in line and "sync files=" in line]
    assert shutdown, log
    assert re.search(r"sync files=0\b", shutdown[-1]), shutdown[-1]
