"""Regression tests for run_local.py's child-exit supervisor logging (#587).

Issue #587: a debug instance's uvicorn process died silently overnight with
no traceback anywhere — run_local.py just did a single blocking
``subprocess.run()`` and never recorded the child's exit code or when it
happened. These tests exercise the pure logging helpers directly (formatting
+ log-path resolution + the append-to-disk side effect) rather than spawning
a real server, per the task's "don't spawn real servers in CI" constraint.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_local  # noqa: E402  (path insert above must run first)


class TestFormatChildExitLine:
    def test_clean_exit_is_not_flagged_as_unexpected(self):
        line = run_local.format_child_exit_line(0, "2026-07-12T22:00:00+00:00",
                                                  "2026-07-12T22:05:00+00:00", 300.0)
        assert "died unexpectedly" not in line
        assert "exited cleanly" in line
        assert "exit=0" in line

    def test_nonzero_exit_is_flagged_unexpected(self):
        line = run_local.format_child_exit_line(1, "2026-07-12T22:00:00+00:00",
                                                  "2026-07-12T22:05:00+00:00", 300.0)
        assert "died unexpectedly" in line
        assert "exit=1" in line

    def test_negative_exit_code_signal_kill_is_flagged(self):
        # POSIX: subprocess.returncode is negative when killed by signal N.
        line = run_local.format_child_exit_line(-9, "2026-07-12T22:00:00+00:00",
                                                  "2026-07-12T22:05:00+00:00", 300.0)
        assert "died unexpectedly" in line
        assert "exit=-9" in line

    def test_windows_access_violation_code_is_flagged(self):
        # Windows STATUS_ACCESS_VIOLATION as an unsigned 32-bit value.
        line = run_local.format_child_exit_line(3221225477, "2026-07-12T22:00:00+00:00",
                                                  "2026-07-12T22:05:00+00:00", 300.0)
        assert "died unexpectedly" in line
        assert "exit=3221225477" in line

    def test_uptime_and_timestamps_are_included(self):
        line = run_local.format_child_exit_line(0, "2026-07-12T22:00:00+00:00",
                                                  "2026-07-12T22:05:00+00:00", 300.0)
        assert "2026-07-12T22:00:00+00:00" in line
        assert "2026-07-12T22:05:00+00:00" in line
        assert "uptime=300.0s" in line


class TestSupervisorLogPath:
    def test_explicit_instance_id_env_var_wins(self, tmp_path):
        env = {"DATA_DIR": str(tmp_path), "INSTANCE_ID": "debug-8765"}
        path = run_local.get_supervisor_log_path(env, 8765)
        assert path == tmp_path / "logs" / "debug-8765" / "supervisor.log"

    def test_falls_back_to_persisted_instance_id_file(self, tmp_path):
        (tmp_path / ".instance_id").write_text("persisted-abc123\n", encoding="utf-8")
        env = {"DATA_DIR": str(tmp_path)}
        path = run_local.get_supervisor_log_path(env, 8000)
        assert path == tmp_path / "logs" / "persisted-abc123" / "supervisor.log"

    def test_falls_back_to_port_when_nothing_else_available(self, tmp_path):
        env = {"DATA_DIR": str(tmp_path)}
        path = run_local.get_supervisor_log_path(env, 9999)
        assert path == tmp_path / "logs" / "local-9999" / "supervisor.log"

    def test_data_dir_env_var_override_respected(self, tmp_path):
        custom = tmp_path / "somewhere-else"
        env = {"DATA_DIR": str(custom), "INSTANCE_ID": "x"}
        path = run_local.get_supervisor_log_path(env, 8000)
        assert path.parent.parent.parent == custom


class TestLogChildExit:
    def test_appends_exit_line_to_disk(self, tmp_path):
        env = {"DATA_DIR": str(tmp_path), "INSTANCE_ID": "debug-8765"}
        run_local.log_child_exit(
            env, 8765, 1, "2026-07-12T22:00:00+00:00", "2026-07-12T22:05:00+00:00", 300.0,
        )
        log_path = tmp_path / "logs" / "debug-8765" / "supervisor.log"
        assert log_path.exists()
        content = log_path.read_text(encoding="utf-8")
        assert "died unexpectedly (exit=1)" in content

    def test_multiple_exits_append_rather_than_overwrite(self, tmp_path):
        env = {"DATA_DIR": str(tmp_path), "INSTANCE_ID": "debug-8765"}
        run_local.log_child_exit(env, 8765, 1, "t0", "t1", 1.0)
        run_local.log_child_exit(env, 8765, 0, "t2", "t3", 2.0)
        log_path = tmp_path / "logs" / "debug-8765" / "supervisor.log"
        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert "exit=1" in lines[0]
        assert "exit=0" in lines[1]

    def test_unwritable_log_dir_does_not_raise(self, tmp_path, monkeypatch):
        # Point DATA_DIR at a path that can't be created (a file where a dir
        # is expected) — log_child_exit must degrade to console-only, never
        # propagate the OSError and mask the real exit status.
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        env = {"DATA_DIR": str(blocker), "INSTANCE_ID": "debug-8765"}
        line = run_local.log_child_exit(
            env, 8765, 1, "2026-07-12T22:00:00+00:00", "2026-07-12T22:05:00+00:00", 300.0,
        )
        assert "died unexpectedly (exit=1)" in line
