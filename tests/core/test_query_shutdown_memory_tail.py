"""Retired saving outlives HTTP cancellation on both unchanged supervisor budgets."""
import sys

import pytest

from tests.core.test_query_shutdown_budget import run_query_shutdown_probe


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX production stop_group")
@pytest.mark.parametrize("container_supervised", [False, True])
def test_two_second_memory_tail_retains_terminal_and_local_memory(tmp_path, container_supervised):
    # Keep the original two-second tail on both budgets. On 10s Uvicorn can
    # cancel its HTTP owner, but must not cancel terminal/memory continuation.
    run_query_shutdown_probe(
        tmp_path, "over-budget", 30 if container_supervised else 10,
        "/api/v1/query", container_supervised=container_supervised,
        memory_delay=2, expect_memory=True,
    )
