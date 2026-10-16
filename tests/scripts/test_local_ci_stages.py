"""Full gate checkpoints preserve coverage and never attest a partial pass."""
import argparse
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import local_ci as ci
import local_ci_stages as stages


@pytest.fixture
def gate(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ci, "ROOT", tmp_path)
    monkeypatch.setattr(ci, "records_path", lambda: tmp_path / "git/passes.json")
    monkeypatch.setattr(ci, "clean_checkout", lambda: True)
    monkeypatch.setattr(ci, "git", lambda *a: "revision")
    monkeypatch.setattr(ci, "tree_of", lambda *a: "tree")
    monkeypatch.setattr(ci, "resolve_base", lambda **kw: "public/main")
    monkeypatch.setattr(ci, "fetch_base", lambda *a, **kw: None)
    monkeypatch.setattr(ci, "attest_update_reach", lambda *a: calls.append("reach"))
    monkeypatch.setattr(ci, "attest", lambda state, *a: calls.append(state))
    monkeypatch.setattr(ci, "security_check", lambda *a, **kw: calls.append("security"))
    monkeypatch.setattr(ci, "backend", lambda ctx: calls.append(("backend", ctx.backend_selected)))
    monkeypatch.setattr(ci, "run_suites", lambda mode, only, python: (calls.append(only[0]) or (only, [], [])))
    monkeypatch.setattr(ci, "save_record", lambda *a: calls.append("record"))
    monkeypatch.setattr(ci, "run", lambda argv, **kw: calls.append(("command", argv)))
    monkeypatch.setattr(ci, "MAP_PATH", tmp_path / "map.json.gz")
    monkeypatch.setattr(ci, "SUITES", [ci.Suite("backend", [], ci.backend, ""),
                                      ci.Suite("fluid", [], lambda ctx: None, "")])
    monkeypatch.setattr(stages, "BACKEND_BATCH_SIZE", 1)
    tests = tmp_path / "tests/core"
    tests.mkdir(parents=True)
    for name in ["a", "b"]:
        (tests / f"test_{name}.py").write_text("def test_ok(): pass")
    return calls, argparse.Namespace(attest=True, strict_audit=False, restart=False, budget=0)


def step(args):
    return stages.run_step(ci, args, sys.executable, "revision")


def test_full_gate_only_records_and_attests_after_every_stage(gate):
    calls, args = gate
    for _ in range(4):  # security, two backend batches, fluid
        assert step(args) == 0
        assert "success" not in calls and "record" not in calls
    assert step(args) == 0
    assert calls.count("success") == calls.count("record") == 1
    assert ("backend", ["tests/core/test_b.py"]) in calls
    assert ("backend", ["tests/core/test_a.py"]) in calls
    assert "fluid" in calls
    before = list(calls)
    assert step(args) == 0
    assert calls.count("success") == 1
    # The update-reach recheck runs in local_ci.attest_preflight, before this.
    assert calls[len(before):] == []


def test_failed_stage_retries_without_replaying_successes(gate, monkeypatch):
    calls, args = gate
    step(args)
    def fail(ctx):
        raise ci.Failure("test failed")
    monkeypatch.setattr(ci, "backend", fail)
    with pytest.raises(ci.Failure, match="test failed"):
        step(args)
    state = json.loads(ci.records_path().with_name("full-stages.json").read_text())
    assert state["completed"] == ["security"]
    assert "failure" in calls and "success" not in calls
    monkeypatch.setattr(ci, "backend", lambda ctx: calls.append("retried"))
    step(args)
    assert calls.count("security") == 1
    assert calls.count("retried") == 1


@pytest.mark.parametrize("changed", ["revision", "tree", "workers", "audit", "python", "restart", "plan"])
def test_changed_identity_starts_new_receipts(gate, monkeypatch, changed):
    calls, args = gate
    step(args)
    old = json.loads(ci.records_path().with_name("full-stages.json").read_text())
    python = sys.executable
    revision = "revision"
    if changed == "revision":
        revision = "new-revision"
        monkeypatch.setattr(ci, "git", lambda *a: revision)
    elif changed == "tree":
        monkeypatch.setattr(ci, "tree_of", lambda *a: "new-tree")
    elif changed == "workers":
        monkeypatch.setattr(ci, "WORKERS", "1")
    elif changed == "audit":
        args.strict_audit = True
    elif changed == "python":
        python = "/different/python"
    elif changed == "restart":
        args.restart = True
    elif changed == "plan":
        monkeypatch.setattr(stages, "BACKEND_BATCH_SIZE", 2)
    stages.run_step(ci, args, python, revision)
    state = json.loads(ci.records_path().with_name("full-stages.json").read_text())
    assert state["completed"] == ["security"]
    assert state["hits"] != old["hits"]
    assert calls.count("security") == 2


def test_mid_stage_edit_cannot_get_receipt(gate, monkeypatch):
    calls, args = gate
    monkeypatch.setattr(ci, "security_check", lambda *a: monkeypatch.setattr(ci, "clean_checkout", lambda: False))
    with pytest.raises(ci.Failure, match="Checkout changed"):
        step(args)
    state = json.loads(ci.records_path().with_name("full-stages.json").read_text())
    assert state["completed"] == []
    assert "success" not in calls


def test_backend_batches_cover_exact_original_files():
    batches = stages.backend_batches(ci)
    files = [f for batch in batches for f in batch]
    expected = {p.relative_to(ci.ROOT).as_posix() for d in ci.FAST_SUBSET
                for p in (ci.ROOT / d).rglob("test_*.py")}
    expected.update(p.relative_to(ci.ROOT).as_posix() for p in (ci.ROOT / "tests").glob("test_*.py"))
    expected.difference_update([*ci.FAST_SUBSET_EXCLUDED, "tests/test_scan_blocking_db.py"])
    assert set(files) == expected
    assert len(files) == len(set(files))
    assert all(len(batch) <= stages.BACKEND_BATCH_SIZE for batch in batches)


def test_backend_batches_keep_directory_collection_and_filter_explicit_roots(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(ci, "ROOT", tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_root.py").write_text("")
    monkeypatch.setattr(ci, "run", lambda argv, **kw: commands.append(argv))
    monkeypatch.setattr(ci, "pytest_args", lambda *a: ["-q"])
    ci.backend(ci.Context("full", "revision", None, sys.executable,
                          backend_selected=["tests/core/test_selected.py"],
                          map_hits=tmp_path / "hits"))
    command = commands[-1]
    assert all(d in command for d in ci.FAST_SUBSET)
    assert "tests/test_root.py" not in command
    assert "scripts.ci.local_ci_batch" in command
    assert "--deselect=tests/test_scan_blocking_db.py" in command
    assert all(f"--ignore={p}" in command for p in ci.FAST_SUBSET_EXCLUDED)


@pytest.mark.parametrize("argv", [["check", "--step"], ["full", "--step", "--only", "backend"],
                                  ["full", "--restart"]])
def test_invalid_step_options_refused(argv):
    with pytest.raises(SystemExit):
        ci.main(argv)



def test_real_batch_collection_preserves_existing_ignore_hooks(tmp_path):
    import os
    import subprocess

    (tmp_path / "pytest.ini").write_text("[pytest]\npython_files = test_*.py\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text(
        "def pytest_ignore_collect(collection_path):\n"
        "    if collection_path.name == 'test_platform.py': return True\n")
    for name in ["a", "b", "platform"]:
        (tests / f"test_{name}.py").write_text("def test_example(): pass\n")
    env = {**os.environ, "PYTHONPATH": str(ci.ROOT),
           "LOCAL_CI_BACKEND_BATCH": json.dumps(["tests/test_a.py", "tests/test_platform.py"])}
    result = subprocess.run([sys.executable, "-m", "pytest", "tests", "--collect-only", "-q",
                             "-p", "scripts.ci.local_ci_batch", "-p", "no:cacheprovider"],
                            cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tests/test_a.py::test_example" in result.stdout
    assert "tests/test_b.py::" not in result.stdout
    assert "tests/test_platform.py::" not in result.stdout


def test_batch_map_hits_survive_recycled_worker_ids(gate, monkeypatch):
    _calls, args = gate
    def backend(ctx):
        (ctx.map_hits / "hits-gw0-123.json").write_text(json.dumps({"hits": {ctx.backend_selected[0]: []}}))
    monkeypatch.setattr(ci, "backend", backend)
    step(args)
    step(args)
    step(args)
    state = json.loads(ci.records_path().with_name("full-stages.json").read_text())
    hits = list(Path(state["hits"]).glob("hits-*.json"))
    assert len(hits) == 2
    assert {name for hit in hits for name in json.loads(hit.read_text())["hits"]} == {
        "tests/core/test_a.py", "tests/core/test_b.py"}


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(stages, "_clock", lambda: now[0])
    return now


def _timed(calls, clock, name, seconds):
    def fake(*args, **kwargs):
        clock[0] += seconds
        calls.append(name if name != "backend" else ("backend", args[0].backend_selected))
        return None
    return fake


def test_budget_runs_stages_back_to_back_while_the_next_fits(gate, monkeypatch, clock, capsys):
    calls, args = gate
    args.budget = 10  # minutes
    (ci.MAP_PATH.parent / "full-stage-times.json").write_text(json.dumps(
        {"security": 10, "backend-1": 240, "backend-2": 240, "fluid": 60, "finish": 60}))
    monkeypatch.setattr(ci, "security_check", _timed(calls, clock, "security", 10))
    monkeypatch.setattr(ci, "backend", _timed(calls, clock, "backend", 240))
    monkeypatch.setattr(ci, "run_suites", lambda mode, only, python: (
        _timed(calls, clock, only[0], 60)() or (only, [], [])))
    assert step(args) == 0
    # 10 + 240 + 240 + 60 s elapsed; finish (about 72 s with margin) would pass 600 s.
    assert [c for c in calls if c not in ("pending",)] == [
        "security", ("backend", ["tests/core/test_a.py"]), ("backend", ["tests/core/test_b.py"]), "fluid"]
    assert calls.count("pending") == 1  # one pending status per invocation, not per stage
    assert "success" not in calls and "record" not in calls
    out = capsys.readouterr().out
    assert "4/5 complete" in out and "Next: finish" in out
    assert step(args) == 0
    assert calls.count("success") == calls.count("record") == 1
    assert calls.count("pending") == 2
    assert "COMPLETE: every available full-gate suite passed." in capsys.readouterr().out


def test_unknown_stages_are_estimated_and_timings_are_recorded(gate, monkeypatch, clock):
    calls, args = gate
    args.budget = 5
    monkeypatch.setattr(ci, "security_check", _timed(calls, clock, "security", 30))
    monkeypatch.setattr(ci, "backend", _timed(calls, clock, "backend", 100))
    # security ran; backend-1 is unknown (10 min) and does not fit 5 minutes.
    assert step(args) == 0
    assert calls == ["pending", "security"]
    times = json.loads((ci.MAP_PATH.parent / "full-stage-times.json").read_text())
    assert times == {"security": 30}
    assert step(args) == 0  # the first stage of an invocation always runs
    times = json.loads((ci.MAP_PATH.parent / "full-stage-times.json").read_text())
    assert times["backend-1"] == 100
    # The unseen batch is estimated from the slowest batch timed so far.
    assert stages.estimate(times, "backend-2") == pytest.approx(100 * stages.ESTIMATE_MARGIN)
    assert stages.estimate(times, "backend-linux") == stages.UNKNOWN_STAGE_SECONDS


def test_completed_stage_set_is_attested_later_without_rerunning(gate, capsys):
    calls, args = gate
    args.attest, args.budget = False, 40
    assert step(args) == 0
    assert "pending" not in calls and "success" not in calls and calls.count("record") == 1
    ran = len(calls)
    args.attest = True
    assert step(args) == 0
    assert calls[ran:] == ["success"]
    assert "from the completed stage set" in capsys.readouterr().out
    assert step(args) == 0
    assert calls[ran:] == ["success"]  # attested once


def test_full_attest_accepts_a_completed_stage_set(gate, monkeypatch):
    calls, args = gate
    args.attest, args.budget = False, 40
    step(args)
    ran = len(calls)
    monkeypatch.setattr(ci, "attest_preflight", lambda revision: calls.append("preflight"))
    monkeypatch.setattr(ci, "python_preflight", lambda python: None)
    monkeypatch.setattr(ci, "choose_python", lambda: sys.executable)
    assert ci.main(["full", "--attest"]) == 0
    assert calls[ran:] == ["preflight", "success"]


def test_full_attest_without_completed_stages_runs_the_suites(gate, monkeypatch):
    calls, _ = gate
    monkeypatch.setattr(ci, "attest_preflight", lambda revision: calls.append("preflight"))
    monkeypatch.setattr(ci, "python_preflight", lambda python: None)
    monkeypatch.setattr(ci, "choose_python", lambda: sys.executable)
    monkeypatch.setattr(ci, "run_suites", lambda mode, only, python: (calls.append(("suites", mode)) or ([], [], [])))
    assert ci.main(["full", "--attest"]) == 0
    assert calls[:3] == ["preflight", "pending", "security"]
    assert ("suites", "full") in calls and calls[-1] == "success"
