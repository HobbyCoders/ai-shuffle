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


# --- budgets shorter than a stage: shards, deadlines and interruptions -------

MINUTE = 60.0


class Simulated:
    """A fake machine: suites cost simulated seconds and stop at the stage deadline."""

    def __init__(self, monkeypatch, tmp_path, calls, fluid_costs, desktop=12 * MINUTE, batch=10 * MINUTE):
        self.calls, self.now, self.tmp_path = calls, [0.0], tmp_path
        self.fluid_costs = fluid_costs
        self.fluid_ran: list[str] = []
        monkeypatch.setattr(stages, "_clock", lambda: self.now[0])
        monkeypatch.setattr(stages, "_wall", lambda: 1_000_000 + self.now[0])
        unit = tmp_path / "fluid/tests/unit"
        unit.mkdir(parents=True)
        for name in fluid_costs:
            (tmp_path / name).write_text("def test_ok(): pass")
        monkeypatch.setattr(ci, "security_check", lambda *a, **kw: self.spend("security", 1 * MINUTE))
        monkeypatch.setattr(ci, "backend", lambda ctx: self.spend(("backend", ctx.backend_selected), batch))
        monkeypatch.setattr(ci, "run_suites", lambda mode, only, python: (
            self.spend(only[0], desktop) or (only, [], [])))
        monkeypatch.setattr(ci, "SUITES", [ci.Suite("backend", [], ci.backend, ""),
                                          ci.Suite("fluid", [], self.fluid, ""),
                                          ci.Suite("desktop", [], lambda ctx: None, "")])

    def spend(self, what, seconds):
        left = stages.remaining()
        if left is not None and seconds > left:
            self.now[0] += max(left, 0)
            raise ci.OutOfTime("simulated deadline")
        self.now[0] += seconds
        self.calls.append(what)

    def fluid(self, ctx):
        files = ctx.backend_selected or sorted(self.fluid_costs)
        self.spend(("fluid", tuple(files)), sum(self.fluid_costs[f] for f in files))
        self.fluid_ran.extend(files)

    def drive(self, args, capsys, limit=12):
        """Repeat the documented command until COMPLETE, as a Monitor-driven session would."""
        outputs = []
        for _ in range(limit):
            began = self.now[0]
            assert step(args) == 0
            assert self.now[0] - began <= args.budget * MINUTE  # never past one budget
            outputs.append(capsys.readouterr().out)
            if "COMPLETE" in outputs[-1]:
                return outputs
        raise AssertionError("the staged gate did not finish: " + "\n".join(outputs))


def _fluid(costs_in_minutes):
    return {f"fluid/tests/unit/test_f{i:02}.py": minutes * MINUTE for i, minutes in enumerate(costs_in_minutes)}


def _state():
    return json.loads(ci.records_path().with_name("full-stages.json").read_text())


def _times():
    return json.loads((ci.MAP_PATH.parent / "full-stage-times.json").read_text())


def test_windows_like_timings_shard_an_over_budget_stage_up_front(gate, monkeypatch, tmp_path, capsys):
    """About 75 minutes of work, one stage (fluid, 60 min) longer than the 40-minute budget."""
    calls, args = gate
    args.budget = 40
    sim = Simulated(monkeypatch, tmp_path, calls, _fluid([5] * 12))
    (ci.MAP_PATH.parent / "full-stage-times.json").write_text(json.dumps(
        {"security": 60, "backend-1": 600, "backend-2": 600, "fluid": 3600, "desktop": 720, "finish": 60}))
    outputs = sim.drive(args, capsys)
    assert len(outputs) == 4
    assert not any("Stopped at the end" in out for out in outputs)  # planned, nothing wasted
    shards = _state()["shards"]["fluid"]
    assert [len(s["files"]) for s in shards] == [6, 6] and all(s["done"] for s in shards)
    # Deterministic: the sorted files dealt round robin, kept in the state file.
    files = sorted(sim.fluid_costs)
    assert [s["files"] for s in shards] == [files[0::2], files[1::2]]
    assert sorted(sim.fluid_ran) == files  # every fluid file ran exactly once
    assert calls.count("success") == calls.count("record") == 1
    assert calls.count("pending") == 4  # one per invocation
    assert "fluid" in _state()["ran"]
    assert _times()["fluid"] == 3600  # the two shards together


def test_cold_machine_halves_a_stage_that_overran_a_whole_budget(gate, monkeypatch, tmp_path, capsys):
    calls, args = gate
    args.budget = 40
    sim = Simulated(monkeypatch, tmp_path, calls, _fluid([5] * 12))
    outputs = sim.drive(args, capsys)
    text = "\n".join(outputs)
    assert "before it had a whole budget; it runs first" in text  # started late, stopped, retried
    assert "split it into shards of 6 and 6 test files" in text  # then overran a whole budget
    state = _state()
    assert [len(s["files"]) for s in state["shards"]["fluid"]] == [6, 6]
    # Stopped attempts do not count; the finished shards cover every file once.
    finished = [f for s in state["shards"]["fluid"] for f in s["files"]]
    assert sorted(finished) == sorted(sim.fluid_costs)
    assert calls.count("success") == 1 and "attempt" not in state
    assert _times()["fluid"] == 3600


def test_one_test_file_longer_than_the_budget_stops_instead_of_looping(gate, monkeypatch, tmp_path, capsys):
    calls, args = gate
    args.budget = 40
    Simulated(monkeypatch, tmp_path, calls, _fluid([50, 1]))
    (ci.MAP_PATH.parent / "full-stage-times.json").write_text(json.dumps({"fluid": 51 * 60}))
    with pytest.raises(ci.OutOfTime, match=r"cannot be split further \(it is one test file, "
                                           r"fluid/tests/unit/test_f00.py\)"):
        for _ in range(8):
            step(args)
    # A time limit, not a failed test: no failure status, and the way on is named.
    assert "failure" not in calls and "success" not in calls and "record" not in calls


def test_non_test_stage_longer_than_the_budget_says_how_to_continue(gate, monkeypatch, tmp_path, capsys):
    calls, args = gate
    args.budget = 40
    sim = Simulated(monkeypatch, tmp_path, calls, _fluid([1]), desktop=42 * MINUTE)
    with pytest.raises(ci.OutOfTime, match="desktop needs more than a whole 40-minute budget .* "
                                           r"\(it is not a test stage\)") as stopped:
        for _ in range(8):
            step(args)
    assert "--budget 44" in str(stopped.value) and "--budget 0" in str(stopped.value)
    assert "failure" not in calls and "success" not in calls
    assert issubclass(ci.OutOfTime, ci.Failure)  # so the CLI still exits 1
    args.budget = 44  # the advice: one larger budget for this stage
    sim.drive(args, capsys)
    assert calls.count("success") == 1


def test_late_unit_that_only_had_part_of_the_budget_is_deferred_not_failed(gate, monkeypatch, tmp_path, capsys):
    """Started 2 minutes in, a 39-minute desktop stage gets 38 minutes; it must run first next time."""
    calls, args = gate
    args.budget = 40
    sim = Simulated(monkeypatch, tmp_path, calls, _fluid([0.5]), desktop=39 * MINUTE, batch=0.25 * MINUTE)
    (ci.MAP_PATH.parent / "full-stage-times.json").write_text(json.dumps({"desktop": 25 * 60}))
    outputs = sim.drive(args, capsys)
    assert "desktop was stopped after 38.0 min, before it had a whole budget" in outputs[0]
    assert "it runs first in the next invocation" in outputs[0]
    assert outputs[1].splitlines()[1] == "Full gate stage 5/6: desktop"  # first, with the whole budget
    assert "failure" not in calls and calls.count("success") == 1


class Killed(BaseException):
    """An uncatchable stop (SIGKILL, TerminateProcess): no handler or finally runs."""


def test_kill_mid_stage_resumes_from_the_heartbeat(gate, monkeypatch, tmp_path, capsys):
    calls, args = gate
    args.budget = 40
    sim = Simulated(monkeypatch, tmp_path, calls, _fluid([5] * 12))
    (ci.MAP_PATH.parent / "full-stage-times.json").write_text(json.dumps(
        {"security": 60, "backend-1": 600, "backend-2": 600, "fluid": 1800, "finish": 60}))
    assert step(args) == 0  # security, two batches, then fluid would not fit
    beat = ci.records_path().with_name("full-stages.beat")

    def killed(ctx):
        sim.now[0] += 44 * MINUTE  # the Monitor gives up first
        last = json.loads(beat.read_text())
        beat.write_text(json.dumps({**last, "beat": stages._wall()}))  # the heartbeat thread's last write
        raise Killed()

    real_fluid, real_stop = ci.SUITES[1].run, stages._Heartbeat.stop
    monkeypatch.setattr(ci.SUITES[1], "run", killed)
    monkeypatch.setattr(stages._Heartbeat, "stop", lambda self: None)  # a killed process cleans nothing
    monkeypatch.setattr(stages, "_restore_signals", lambda previous: None)
    with pytest.raises(Killed):
        step(args)
    left = _state()
    assert left["attempt"]["stage"] == "fluid" and left["completed"][-1] == "backend-2"
    monkeypatch.setattr(ci.SUITES[1], "run", real_fluid)
    monkeypatch.setattr(stages._Heartbeat, "stop", real_stop)
    outputs = sim.drive(args, capsys)
    assert "was stopped during fluid after about 44.0 min" in outputs[0]
    assert "split it into shards of 6 and 6" in outputs[0]
    assert sorted(sim.fluid_ran) == sorted(sim.fluid_costs)
    assert calls.count("success") == 1



@pytest.mark.parametrize("stop, minutes, splits", [(SystemExit(143), 44, True), (KeyboardInterrupt(), 2, False)])
def test_signal_mid_stage_records_how_long_it_ran(gate, monkeypatch, tmp_path, capsys, stop, minutes, splits):
    calls, args = gate
    args.budget = 40
    sim = Simulated(monkeypatch, tmp_path, calls, _fluid([5] * 12))
    (ci.MAP_PATH.parent / "full-stage-times.json").write_text(json.dumps(
        {"security": 60, "backend-1": 600, "backend-2": 600, "fluid": 1800, "finish": 60}))
    step(args)
    real_fluid = ci.SUITES[1].run

    def stopped(ctx):
        sim.now[0] += minutes * MINUTE
        raise stop

    monkeypatch.setattr(ci.SUITES[1], "run", stopped)
    with pytest.raises(type(stop)):
        step(args)
    assert _state()["attempt"]["stopped_after"] == minutes * MINUTE
    monkeypatch.setattr(ci.SUITES[1], "run", real_fluid)
    capsys.readouterr()
    outputs = sim.drive(args, capsys)
    recovered = outputs[0].splitlines()[:2]  # what the next invocation made of the stop
    assert recovered[0].startswith(f"The previous invocation was stopped during fluid after about {minutes}.0 min")
    assert ("split it into shards" in recovered[1]) is splits
    assert ("it runs first in the next invocation" in recovered[1]) is not splits
    assert sorted(sim.fluid_ran) == sorted(sim.fluid_costs) and calls.count("success") == 1


def test_repeated_kills_split_even_when_the_budget_exceeds_the_monitor(gate, monkeypatch, tmp_path, capsys):
    """--budget 60 under a 45-minute Monitor: each kill looks short, but two in a row split."""
    calls, args = gate
    args.budget = 60
    sim = Simulated(monkeypatch, tmp_path, calls, _fluid([5] * 12))
    (ci.MAP_PATH.parent / "full-stage-times.json").write_text(json.dumps(
        {"security": 60, "backend-1": 600, "backend-2": 1800, "fluid": 2400, "finish": 60}))
    step(args)  # security, backend-1 and backend-2; fluid (48 min) would not fit 60 after 41
    real_fluid = ci.SUITES[1].run

    def monitor_timeout(ctx):
        sim.now[0] += 45 * MINUTE
        raise SystemExit(143)

    monkeypatch.setattr(ci.SUITES[1], "run", monitor_timeout)
    outputs = []
    for _ in range(2):
        with pytest.raises(SystemExit):
            step(args)
        outputs.append(capsys.readouterr().out)
    assert "it runs first in the next invocation" in outputs[1]  # the first kill: retried
    monkeypatch.setattr(ci.SUITES[1], "run", real_fluid)
    done = sim.drive(args, capsys)
    assert "split it into shards of 6 and 6" in done[0]
    assert calls.count("success") == 1


def test_second_invocation_refuses_while_another_process_holds_the_lock(gate, tmp_path):
    import subprocess
    _calls, args = gate
    lock = ci.records_path().with_name("full-stages.lock")
    holder = subprocess.Popen(
        [sys.executable, "-c",
         ("import sys, time; from pathlib import Path; sys.path.insert(0, sys.argv[1]);"
          "import local_ci as ci, local_ci_stages as s; held = s._claim(ci, Path(sys.argv[2]));"
          "print('held', flush=True); time.sleep(60)"),
         str(Path(ci.__file__).parent), str(lock)],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(ci.Failure, match="Another full --step invocation"):
            step(args)
    finally:
        holder.kill()  # an outright kill: the system drops its lock
        holder.wait()
    assert step(args) == 0
    held = stages._claim(ci, lock)  # released at the end of the invocation
    stages._release(held)


def test_attest_refuses_receipts_that_do_not_cover_the_tree(gate, monkeypatch, tmp_path, capsys):
    calls, args = gate
    args.attest, args.budget = False, 40
    sim = Simulated(monkeypatch, tmp_path, calls, _fluid([5] * 4))
    (ci.MAP_PATH.parent / "full-stage-times.json").write_text(json.dumps({"fluid": 3600}))
    sim.drive(args, capsys)
    path = ci.records_path().with_name("full-stages.json")
    state = _state()
    state["shards"]["fluid"][0]["files"].pop()  # a test file no receipt covers
    path.write_text(json.dumps(state))
    args.attest = True
    with pytest.raises(ci.Failure, match="do not cover its 4 test files exactly once"):
        step(args)
    assert "success" not in calls


def test_success_names_the_tree_the_stages_covered(gate, monkeypatch, tmp_path, capsys):
    calls, args = gate
    described = []
    monkeypatch.setattr(ci, "attest", lambda state, description, revision: described.append((state, description)))
    args.budget = 40
    sim = Simulated(monkeypatch, tmp_path, calls, _fluid([1] * 3))
    sim.drive(args, capsys)
    successes = [d for s, d in described if s == "success"]
    assert len(successes) == 1 and "tree tree" in successes[0] and "stages" in successes[0]


def test_deadline_stops_the_whole_process_tree(tmp_path, monkeypatch):
    import os
    import subprocess
    import time
    child_pid = tmp_path / "child.pid"
    script = ("import subprocess, sys, time, pathlib\n"
              "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
              f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid))\n"
              "time.sleep(60)\n")
    monkeypatch.setattr(ci, "DEADLINE", time.monotonic() + 3)
    began = time.monotonic()
    with pytest.raises(ci.OutOfTime, match="end of this invocation's budget"):
        ci.run([sys.executable, "-c", script])
    assert time.monotonic() - began < 30
    pid = int(child_pid.read_text())

    def running(pid):
        if os.name == "nt":
            listed = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, check=False)
            return str(pid) in listed.stdout
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            return state != "Z"  # an unreaped zombie is not running
        except OSError:
            pass
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    for _ in range(50):
        if not running(pid):
            break
        time.sleep(0.1)
    else:
        if os.name != "nt":
            os.kill(pid, 9)
        raise AssertionError("the command's child outlived the deadline")
    with pytest.raises(ci.OutOfTime, match="no time left"):
        ci.run([sys.executable, "-c", "pass"])
    monkeypatch.setattr(ci, "DEADLINE", None)
    ci.run([sys.executable, "-c", "pass"])  # no budget, no limit


def test_real_shard_collection_under_its_own_rootdir(tmp_path):
    """fluid/tests/unit has its own rootdir (fluid/pyproject.toml); shard paths stay checkout-relative."""
    import os
    import subprocess

    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    fluid = tmp_path / "fluid"
    (fluid / "tests/unit").mkdir(parents=True)
    (fluid / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths = ['tests']\n")
    for name in ["a", "b"]:
        (fluid / f"tests/unit/test_{name}.py").write_text("def test_example(): pass\n")
    env = {**os.environ, "PYTHONPATH": str(ci.ROOT),
           "LOCAL_CI_BACKEND_BATCH": json.dumps(["fluid/tests/unit/test_b.py"])}
    result = subprocess.run([sys.executable, "-m", "pytest", "fluid/tests/unit", "--collect-only", "-q",
                             "-p", "scripts.ci.local_ci_batch", "-p", "no:cacheprovider"],
                            cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "test_b.py::test_example" in result.stdout
    assert "test_a.py::" not in result.stdout


def test_fluid_shard_command_keeps_the_directory_and_adds_the_batch_plugin(monkeypatch):
    commands = []
    monkeypatch.setattr(ci, "run", lambda argv, **kw: commands.append((argv, kw["env"])))
    monkeypatch.setattr(ci, "pytest_args", lambda *a: ["-q"])
    ci.fluid(ci.Context("full", "revision", None, sys.executable))
    ci.fluid(ci.Context("full", "revision", None, sys.executable,
                        backend_selected=["fluid/tests/unit/test_x.py"]))
    whole, shard = commands
    assert whole[0] == [sys.executable, "-m", "pytest", "fluid/tests/unit", "-q"]
    assert "LOCAL_CI_BACKEND_BATCH" not in whole[1]
    assert shard[0][:4] == [sys.executable, "-m", "pytest", "fluid/tests/unit"]
    assert "scripts.ci.local_ci_batch" in shard[0]
    assert json.loads(shard[1]["LOCAL_CI_BACKEND_BATCH"]) == ["fluid/tests/unit/test_x.py"]


def test_shards_split_up_front_only_on_measured_timings():
    files = [f"tests/core/test_{i:02}.py" for i in range(20)]
    # Never timed: the 10-minute guess must not split a stage before any evidence.
    assert len(stages._initial_shards({}, "backend-1", files, budget=6 * MINUTE)) == 1
    assert len(stages._initial_shards({"backend-1": 900}, "backend-1", files, budget=6 * MINUTE)) == 3
    state = {"shards": {"backend-1": [{"files": files[:10], "done": True, "seconds": 70.0},
                                      {"files": files[10:], "done": False}]}}
    seconds, measured = stages.unit_estimate({}, state, "backend-1", files[10:], files)
    assert measured and seconds == pytest.approx(70.0 * stages.ESTIMATE_MARGIN)
    assert stages.unit_estimate({}, {"shards": {}}, "backend-1", files, files) == (
        stages.UNKNOWN_STAGE_SECONDS, False)
