"""Keep the short-budget negative evidence; a supplied Docker budget has more reserve."""
import sys

import pytest

from tests.core.test_query_shutdown_budget import run_query_shutdown_probe


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX production stop_group")
@pytest.mark.parametrize("container_supervised", [False, True])
def test_two_second_memory_tail_retains_terminal_and_local_memory(tmp_path, container_supervised):
    # On 10s: terminal persistence and lifespan are guaranteed by this scenario,
    # not the pending replication/HTTP success. On 30s the two-second tail fits.
    run_query_shutdown_probe(
        tmp_path, "over-budget", 30 if container_supervised else 10,
        "/api/v1/query", container_supervised=container_supervised,
        memory_delay=2, expect_memory=container_supervised,
    )
