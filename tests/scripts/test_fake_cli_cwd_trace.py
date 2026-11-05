"""Observe the disposable fake CLI's own cwd, without changing protocol or cwd."""
import json
import os
from pathlib import Path

import pytest
from scripts.devloop import fake_claude
from tests.scripts.test_devloop_fake_claude import Cli


def traces(root: Path):
    return [json.loads(line) for line in fake_claude.log_path(root).read_text().splitlines()
            if json.loads(line).get("kind") == "turn_cwd"]


def test_every_incoming_turn_traces_actual_cwd(tmp_path):
    cli = Cli(tmp_path)
    try:
        cli.turn("first")
        cli.turn("second")
        rows = traces(tmp_path)
        assert [row["query_generation"] for row in rows] == [1, 2]
        assert {row["launch_generation"] for row in rows} == {cli.proc.pid}
        assert all(row["cwd"] == str(tmp_path / "ws") and row["cwd_error"] is None for row in rows)
    finally:
        assert cli.close() == 0


@pytest.mark.skipif(os.name == "nt", reason="Windows refuses deleting a process cwd")
def test_missing_cwd_is_observed_not_repaired(tmp_path):
    cli = Cli(tmp_path)
    try:
        cli.turn("first")
        (tmp_path / "ws").rmdir()
        # Existing transcript writing fails on the removed cwd; telemetry must
        # observe that before the original failure, not repair or hide it.
        with pytest.raises(AssertionError, match="the CLI exited"):
            cli.turn("second")
        row = traces(tmp_path)[-1]
        assert row["query_generation"] == 2 and row["cwd"] is None
        assert row["cwd_error"] == "FileNotFoundError" and row["cwd_errno"] == 2
        assert not (tmp_path / "ws").exists()
    finally:
        assert cli.close() == 1
