"""Pure benchmark checks: no services, database or credentials needed."""
import io
import json
import math
import subprocess
from types import SimpleNamespace

import pytest

from scripts.perf import sync_bench
from scripts.perf.sync_bench import (
    attempt, capture_log, committed_at, completed_scans, delta_lags, op_key, recall_visible,
    sample_summary, sanitize_log, stages, stop_lab, summarize, wait_for,
)


def test_project_creation_starts_at_first_domain_commit_not_response_or_read():
    result = {"trace": "create", "end": 7}
    metrics = {"events": [{"kind": "commit", "trace": "other", "t": 1},
                          {"kind": "commit", "trace": "create", "t": 4},
                          {"kind": "commit", "trace": "create", "t": 3}]}
    assert committed_at(result, metrics) == 3
    assert committed_at(result, {"events": []}) == 7


def test_stream_lags_match_common_seq_endpoints_not_frame_positions():
    owner = [{"seq": 1, "t": 1}, {"seq": 2, "t": 2}, {"seq": 4, "t": 4}]
    peer = [{"seq": 2, "t": 2.5}, {"seq": 3, "t": 3.5}, {"seq": 4, "t": 4.25}]
    assert delta_lags(owner, peer) == [500, 250]
    with pytest.raises(RuntimeError, match="not a steady streaming sample"):
        delta_lags(owner, peer[:1])


def test_nearest_rank_percentiles_match_js_rig():
    result = summarize([9, 1, 3, 2, math.nan, math.inf, "ignored"])
    assert result == {"n": 4, "p50": 2, "p75": 3, "max": 9}
    assert summarize([]) == {"n": 0, "p50": None, "p75": None, "max": None}


def test_stage_correlation_uses_signed_origin_and_sequence():
    source = {"events": [
        {"kind": "published", "t": 2, "op": ["A", 1, 7],
         "collection": "sessions", "record_id": "chat", "scope": "scan_targeted", "bytes": 80},
        {"kind": "push_send", "t": 3, "op": ["A", 1, 7]},
    ]}
    peer = {"events": [
        {"kind": "applied", "t": 2.5, "arrival": 2.4, "op": ["B", 1, 7]},
        {"kind": "applied", "t": 4, "arrival": 3.5, "op": ["A", 1, 7]},
    ]}
    result = stages(1, 5, source, peer, "sessions", "chat")
    assert result["commit_to_publish_ms"] == 1000
    assert result["publish_to_exchange_ms"] == 1000
    assert result["exchange_to_apply_ms"] == 500
    assert result["apply_ms"] == 500
    assert result["apply_to_visible_ms"] == 1000
    assert result["exchange_boundary"] == "push_send"
    assert stages(1, 5, source, peer, "sessions", "other") == {}
    assert stages(3, 5, source, peer, "sessions", "chat") == {}


def test_recall_visibility_ignores_replica_aliases():
    from types import SimpleNamespace
    class Store:
        def search_memories(self, text, *, limit):
            assert limit == 100
            return [(SimpleNamespace(id="peer-origin:m1", text="uniquefruit"), False)]
    assert recall_visible(Store(), "uniquefruit")
    assert not recall_visible(Store(), "unrelatedfruit")


def test_log_sanitization_covers_preview_and_enrollment_links():
    text = "http://lab/?token=fake-preview&next=/ http://lab/join?secret=fake-join\" 200"
    assert sanitize_log(text) == "http://lab/?token=[redacted]&next=/ http://lab/join?secret=[redacted]\" 200"


def test_access_announcements_and_ci_readiness_are_filtered_before_persistence():
    # scripts.devloop.Lifecycle._announce and _ci_verdict message shapes.
    lines = [
        "INFO open once in the agent browser: http://127.0.0.1:18080/api/v1/auth/preview-access?token=fake\n",
        'DEVLOOP_READY {"status": "/tmp/status.json", "ui": "http://127.0.0.1:18080/", "pid": 42, "seconds": 1.2}\n',
        "INFO fake AI: http://127.0.0.1:18081 (request log /tmp/fake.log)\n",
        "INFO peer: WSS://127.0.0.1:18082/?secret=fake\n",
        "INFO app ready in 1.2s\n",
        "ERROR request failed token=fake secret=fake\n",
    ]
    class CheckedSink(io.StringIO):
        def write(self, text):
            assert "://" not in text
            assert "fake" not in text
            return super().write(text)
    destination = CheckedSink()
    capture_log(io.StringIO("".join(lines)), destination)
    assert destination.getvalue() == (
        "INFO app ready in 1.2s\nERROR request failed token=[redacted] secret=[redacted]\n"
    )


def test_op_key_does_not_conflate_incarnations():
    assert op_key(dict(origin_device_id="A", origin_incarnation=2, origin_seq=7)) == ["A", 2, 7]


def test_project_and_stream_only_cannot_silently_skip_every_journey(monkeypatch):
    from scripts.perf.sync_bench import main
    monkeypatch.setattr("sys.argv", ["sync_bench", "run", "--output", "unused.json",
                                    "--projects-only", "--stream-only"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_memory_only_cli_keeps_the_real_scanner_idle_interval(monkeypatch, tmp_path):
    import sys
    from scripts.perf import sync_bench
    captured = []
    monkeypatch.setattr(sync_bench, "run", captured.append)
    monkeypatch.setattr(sys, "argv", ["sync_bench", "run", "--memory-only", "--runs", "10",
                                    "--output", str(tmp_path / "result.json")])
    sync_bench.main()
    assert captured[0].memory_only
    assert captured[0].memory_cold_seconds == 65
    assert captured[0].runs == 10
    monkeypatch.setattr(sys, "argv", sys.argv + ["--memory-cold-seconds", "60"])
    with pytest.raises(SystemExit) as error:
        sync_bench.main()
    assert error.value.code == 2
    assert len(captured) == 1


def test_child_cannot_attach_to_unowned_app(monkeypatch):
    from scripts.perf.sync_bench import child
    monkeypatch.delenv("AI_SHUFFLE_PREVIEW", raising=False)
    monkeypatch.delenv("SYNC_BENCH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="private devloop"):
        child()


@pytest.mark.parametrize("journeys", [
    ("setup",), ("preference",), ("stream_first_token", "stream_steady_delta"),
    ("catchup_1000",), ("long_seed_setup",),
])
def test_deadline_failures_are_saved_with_null_latency_before_reraising(journeys):
    samples = [dict(journey="earlier", total_ms=12)]
    saved = []
    def save():
        saved.append(json.loads(json.dumps(samples)))
    with pytest.raises(TimeoutError, match="convergence deadline"):
        with attempt(samples, save, *journeys):
            # Even partially prepared samples from this attempt must not survive
            # as successful finite measurements when a subsequent step fails.
            samples.append(dict(journey=journeys[0], total_ms=0))
            wait_for(lambda: False, timeout=0)
    assert len(saved) == 1
    assert saved[0] == samples
    assert samples[0] == dict(journey="earlier", total_ms=12)
    summary, counts = sample_summary(samples)
    for name in journeys:
        failed = next(row for row in samples if row["journey"] == name)
        assert failed["status"] == "failed"
        assert failed["total_ms"] is None
        assert failed["failure"] == "TimeoutError"
        assert failed["observed_seconds"] >= 0
        assert counts[name] == dict(ok=0, failed=1, censored=0)
        assert summary[name + ".total_ms"] == dict(
            n=0, p50=None, p75=None, max=None, failed=1, censored=0)


def test_censored_and_failed_attempts_are_counted_but_not_zero_latencies():
    summary, counts = sample_summary([
        dict(journey="project_create", total_ms=42),
        dict(journey="project_create", status="censored", total_ms=None),
        dict(journey="project_create", status="failed", total_ms=0),
    ])
    assert summary["project_create.total_ms"] == dict(
        n=1, p50=42, p75=42, max=42, failed=1, censored=1)
    assert counts["project_create"] == dict(ok=1, failed=1, censored=1)


def test_scans_crossing_snapshot_boundaries_are_counted_once_on_completion():
    early = dict(kind="scan_full", start=-.1, t=.1)
    crossing = dict(kind="scan_full", start=59.9, t=60.1)
    boundary = dict(kind="scan_full", start=59.8, t=60)
    first = dict(now=0, events=[])
    middle = dict(now=60, events=[early])
    last = dict(now=120, events=[early, crossing, boundary,
                               dict(kind="scan_targeted", start=61, t=62)])
    assert completed_scans(first, middle) == [early]
    assert completed_scans(middle, last) == [crossing, boundary]


@pytest.mark.parametrize("exit_code", [0, 1, -15])
def test_teardown_saves_diagnostics_before_rejecting_nonzero_or_signal_exit(exit_code):
    class Process:
        returncode = None
        terminated = False
        def poll(self):
            return self.returncode
        def terminate(self):
            self.terminated = True
        def wait(self, *, timeout):
            assert timeout == 90
            self.returncode = exit_code
    process, report, saved = Process(), {}, []
    def save():
        saved.append(dict(report))
    if exit_code:
        with pytest.raises(RuntimeError, match=f"cleanup failed: exit {exit_code}"):
            stop_lab(process, report, save)
    else:
        stop_lab(process, report, save)
    assert process.terminated
    assert saved == [dict(teardown_exit=exit_code)]


def test_teardown_timeout_saves_diagnostics_then_fails():
    class Process:
        def poll(self):
            return None
        def terminate(self):
            pass
        def wait(self, *, timeout):
            raise subprocess.TimeoutExpired("lab", timeout)
    report, saved = {}, []
    with pytest.raises(RuntimeError, match="did not finish cleanup"):
        stop_lab(Process(), report, lambda: saved.append(dict(report)))
    assert saved == [dict(teardown_exit=None, teardown_error="cleanup_timeout")]


@pytest.mark.parametrize("exit_code", [0, 1, -15])
def test_log_output_extension_cannot_overwrite_json_on_a_failed_run(tmp_path, monkeypatch, exit_code):
    class Process:
        returncode = exit_code
        stdout = io.StringIO("INFO app ready\nhttp://lab/?token=fake\n")
        def poll(self):
            return self.returncode
        def wait(self, *, timeout):
            return self.returncode
    def deadline(*args, **kwargs):
        raise TimeoutError("test setup deadline")
    monkeypatch.setattr(sync_bench.signal, "signal", lambda *args: None)
    monkeypatch.setattr(sync_bench.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(sync_bench.subprocess, "check_output", lambda *args, **kwargs: "test-commit")
    monkeypatch.setattr(sync_bench, "wait_for", deadline)
    output = tmp_path / "x.log"
    error = TimeoutError if exit_code == 0 else RuntimeError
    message = "test setup deadline" if exit_code == 0 else f"cleanup failed: exit {exit_code}"
    with pytest.raises(error, match=message):
        sync_bench.run(SimpleNamespace(output=output))
    report = json.loads(output.read_text())
    assert report["counts"]["setup"] == dict(ok=0, failed=1, censored=0)
    assert report["samples"][0]["total_ms"] is None
    assert report["teardown_exit"] == exit_code
    assert report["log_capture_complete"] is True
    assert (tmp_path / "x.log.lab.log").read_text() == "INFO app ready\n"
