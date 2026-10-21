"""Resource sampling tolerates hardened /proc without losing RSS/CPU evidence."""
from pathlib import Path

import pytest

import scripts.perf_resources as resources


def fixture_proc(tmp_path, monkeypatch):
    base = tmp_path / "42"
    base.mkdir()
    # fields 3..22: state, ppid, ... utime/stime (14/15), starttime (22)
    fields = ["S", "1"] + ["0"] * 18
    fields[11:13] = ["123", "45"]
    fields[19] = "999"
    (base / "stat").write_text("42 (a name with ) parens) " + " ".join(fields))
    (base / "status").write_text("VmRSS:\t1024 kB\nRssAnon:\t800 kB\nRssFile:\t224 kB\nThreads:\t3\n")
    (base / "net").mkdir()
    (base / "net/dev").write_text("header\nheader\n eth0: 100 0 0 0 0 0 0 0 200 0 0 0 0 0 0 0\n")
    monkeypatch.setattr(resources, "Path", lambda value: tmp_path if value == "/proc" else Path(value))
    return base


def test_unavailable_fd_and_smaps_preserve_status(tmp_path, monkeypatch):
    fixture_proc(tmp_path, monkeypatch)
    row = resources.process(42)
    assert row["VmRSS"] == 1024 and row["RssAnon"] == 800
    assert row["cpu_ticks"] == 168 and row["start_ticks"] == 999
    assert row["fds"] is None and row["smaps_unavailable"]


def test_sample_network_is_explicitly_namespace_scoped(tmp_path, monkeypatch):
    fixture_proc(tmp_path, monkeypatch)
    row = resources.sample(42)
    assert row["network"]["eth0"] == {"rx_bytes": 100, "tx_bytes": 200}
    assert "not process-attributed" in row["network_scope"]
    assert [p["pid"] for p in row["processes"]] == [42]


def test_sampler_rejects_pid_reuse(monkeypatch):
    import sys
    records = iter([
        dict(monotonic=1, processes=[dict(pid=42, start_ticks=1, cpu_ticks=10)]),
        dict(monotonic=2, processes=[dict(pid=42, start_ticks=2, cpu_ticks=11)]),
    ])
    monkeypatch.setattr(resources, "sample", lambda pid: next(records))
    monkeypatch.setattr(resources.time, "monotonic", lambda: 0)
    monkeypatch.setattr(resources.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(sys, "argv", ["sampler", "--pid", "42", "--seconds", "10"])
    with pytest.raises(SystemExit, match="PID changed"):
        resources.main()
