"""The dev loop's SIGKILL guardian and its CI journey driver (scripts/devloop/)."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from scripts.devloop import ci, guardian


def _wait_for(predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def _sleeper(**kwargs) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], **kwargs)


def _kill(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(10)


@pytest.fixture
def instance(tmp_path):
    """A launcher stand-in with an app process group, a status file and scratch."""
    launcher = _sleeper()
    app = _sleeper(start_new_session=True)
    _wait_for(lambda: guardian.identity(launcher.pid) and guardian.identity(app.pid))
    status_dir = tmp_path / "status"
    status_dir.mkdir(mode=0o700)
    status = status_dir / "dev.json"
    scratch = tmp_path / "ai-shuffle-preview-test"
    (scratch / "data").mkdir(parents=True)
    (scratch / guardian.SCRATCH_MARKER).write_text(json.dumps(
        {"version": 1, "pid": launcher.pid, "start": guardian.identity(launcher.pid),
         "uid": os.getuid()}))
    status.write_text(json.dumps({
        "pid": launcher.pid, "pid_identity": guardian.identity(launcher.pid), "state": "ready",
        "app": {"pid": app.pid, "pid_identity": guardian.identity(app.pid)}}))
    yield launcher, app, status, scratch
    _kill(launcher)
    _kill(app)


def test_a_sigkilled_launcher_is_cleaned_up_after(instance):
    launcher, app, status, scratch = instance
    start = guardian.identity(launcher.pid)
    launcher.send_signal(signal.SIGKILL)
    launcher.wait(10)
    done = guardian.watch(status, scratch, launcher.pid, start, poll=0.05)
    _wait_for(lambda: app.poll() is not None)  # the app's group was SIGKILLed
    assert app.returncode == -signal.SIGKILL
    assert not status.exists() and not scratch.exists()
    assert any("killed app" in line for line in done)
    log = status.with_name("dev.json.guardian.log").read_text()
    assert f"launcher {launcher.pid} died before its teardown" in log


def test_a_newer_status_and_someone_elses_scratch_are_left_alone(instance):
    launcher, app, status, scratch = instance
    start = guardian.identity(launcher.pid)
    newer = {"pid": os.getpid(), "pid_identity": guardian.identity(os.getpid())}
    status.write_text(json.dumps(newer))
    receipt = json.loads((scratch / guardian.SCRATCH_MARKER).read_text())
    (scratch / guardian.SCRATCH_MARKER).write_text(json.dumps({**receipt, "start": "1"}))
    _kill(launcher)
    assert guardian.watch(status, scratch, launcher.pid, start, poll=0.05) == []
    assert json.loads(status.read_text()) == newer and scratch.exists()
    assert app.poll() is None  # recorded only in a status it does not own


def test_a_reused_app_pid_is_never_killed(instance):
    launcher, app, status, scratch = instance
    start = guardian.identity(launcher.pid)
    data = json.loads(status.read_text())
    data["app"]["pid_identity"] = "1"  # another process now has that pid
    status.write_text(json.dumps(data))
    _kill(launcher)
    guardian.watch(status, scratch, launcher.pid, start, poll=0.05)
    assert app.poll() is None and not status.exists()


def test_the_guardian_waits_while_the_launcher_lives(instance):
    launcher, app, status, scratch = instance
    assert guardian.watch(status, scratch, launcher.pid, guardian.identity(launcher.pid),
                          poll=0.05, lifetime=0.3) == []
    assert status.exists() and scratch.exists() and app.poll() is None


def test_zombies_count_as_gone():
    child = _sleeper()
    _wait_for(lambda: guardian.identity(child.pid))
    start = guardian.identity(child.pid)
    assert guardian.alive(child.pid, start)
    os.kill(child.pid, signal.SIGKILL)
    _wait_for(lambda: not guardian.alive(child.pid, start))  # unreaped: a zombie
    child.wait(10)


def test_spawned_guardian_is_detached_and_survives_the_task_kill(instance):
    """TaskStop SIGTERMs the task's group and SIGKILLs its descendants: the
    guardian must be in neither, then clean up after the launcher."""
    launcher, app, status, scratch = instance
    record = guardian.spawn_guardian(status, scratch, launcher_pid=launcher.pid)
    assert record and record["pid_identity"]
    pid = record["pid"]
    try:
        fields = guardian._stat_fields(pid)
        assert int(fields[1]) != os.getpid()  # parent: init or a subreaper, not us
        assert os.getsid(pid) != os.getsid(0) and os.getpgid(pid) != os.getpgid(0)
        launcher.send_signal(signal.SIGKILL)
        launcher.wait(10)
        _wait_for(lambda: not scratch.exists() and not status.exists())
        _wait_for(lambda: app.poll() is not None)
        _wait_for(lambda: not guardian.alive(pid, record["pid_identity"]))
    finally:
        if guardian.alive(pid, record["pid_identity"]):
            os.kill(pid, signal.SIGKILL)


# ─── CI journey driver ─────────────────────────────────────────────────────


def test_shards_are_an_exact_balanced_partition():
    weights = {"a.journey.mjs": 16, "b.journey.mjs": 8, "c.journey.mjs": 8, "d.journey.mjs": 4,
               "e.journey.mjs": 4, "f.journey.mjs": 1}
    shards = ci.partition(weights, 3)
    flat = [name for shard in shards for name in shard]
    assert sorted(flat) == sorted(weights) and len(flat) == len(set(flat))
    loads = [sum(weights[name] for name in shard) for shard in shards]
    assert max(loads) == 16 and min(loads) >= 12
    assert ci.partition(weights, 3) == shards  # deterministic
    assert ci.partition({"a.journey.mjs": 1}, 4)[1:] == [[], [], []]
    with pytest.raises(ValueError):
        ci.partition(weights, 0)


def _cases(monkeypatch, counts):
    listed = []

    def case_ids(path):
        listed.append(path.name)
        return [f"{path.name}-{i}@desktop-fast" for i in range(counts[path.name])]

    monkeypatch.setattr(ci, "case_ids", case_ids)
    return listed


def test_a_selection_splits_heavy_files_into_parts_of_the_target_size(monkeypatch):
    listed = _cases(monkeypatch, {"big.journey.mjs": 10})
    entries = ci.split_heavy({"big.journey.mjs": 600, "small.journey.mjs": 100}, 18)
    # 600s in 2.5-minute parts: four parts of 3, 3, 2 and 2 interleaved cases.
    assert entries == {"big.journey.mjs#1/4": 180, "big.journey.mjs#2/4": 180,
                       "big.journey.mjs#3/4": 120, "big.journey.mjs#4/4": 120, "small.journey.mjs": 100}
    assert listed == ["big.journey.mjs"]  # only a file being split is listed again


def test_splitting_never_puts_two_parts_of_a_file_on_one_shard(monkeypatch):
    _cases(monkeypatch, {"big.journey.mjs": 10, "few.journey.mjs": 2, "a.journey.mjs": 8, "b.journey.mjs": 8})
    weights = {"big.journey.mjs": 600, "small.journey.mjs": 100}
    big = [f"big.journey.mjs#{i}/{n}" for n in (2, 3) for i in range(1, n + 1)]
    assert sorted(ci.split_heavy(weights, 3)) == big[2:] + ["small.journey.mjs"]
    assert sorted(ci.split_heavy(weights, 2)) == big[:2] + ["small.journey.mjs"]
    assert ci.split_heavy(weights, 1) == weights
    for buckets in (ci.partition(ci.split_heavy(weights, n), n) for n in (1, 2, 3, 18)):
        assert all(len({ci.entry_file(e) for e in bucket}) == len(bucket) for bucket in buckets)
    # Never more parts than cases; two equally heavy files are both split.
    assert sorted(ci.split_heavy({"few.journey.mjs": 900}, 18)) == ["few.journey.mjs#1/2", "few.journey.mjs#2/2"]
    assert sorted(ci.split_heavy({"a.journey.mjs": 400, "b.journey.mjs": 400}, 18)) == [
        f"{f}.journey.mjs#{i}/3" for f in "ab" for i in (1, 2, 3)]


def test_a_split_selection_gives_every_part_its_own_shard_and_every_case_once(tmp_path, monkeypatch):
    for name in ("big.journey.mjs", "mid.journey.mjs", "small.journey.mjs"):
        (tmp_path / name).touch()
    counts = {"big.journey.mjs": 23, "mid.journey.mjs": 9, "small.journey.mjs": 4}
    _cases(monkeypatch, counts)
    weights = tmp_path / "weights.json"
    weights.write_text(json.dumps({"seconds": {"big.journey.mjs": 560, "mid.journey.mjs": 400,
                                               "small.journey.mjs": 60},
                                   "seconds_per_case": 10, "case_counts": counts}))
    buckets = ci.plan(18, tmp_path, weights, only=list(counts))
    filled = [bucket for bucket in buckets if bucket]
    assert all(len(bucket) == 1 for bucket in filled) and len(filled) == 4 + 3 + 1
    ran = [id for (entry,) in filled
           for id in (ci.part_cases(entry, tmp_path) or ci.case_ids(tmp_path / entry))]
    assert sorted(ran) == sorted(id for name in counts for id in ci.case_ids(tmp_path / name))
    assert ci.plan(18, tmp_path, weights, only=list(counts)) == buckets  # every shard plans the same
    # The full run (no selection) is planned by whole files, as before.
    assert sorted(n for b in ci.plan(18, tmp_path, weights) for n in b) == sorted(counts)


def test_a_part_runs_its_exact_cases_and_a_bad_part_fails_closed(tmp_path, monkeypatch):
    _cases(monkeypatch, {"big.journey.mjs": 5})
    (tmp_path / "big.journey.mjs").touch()
    commands = ci.commands_for(["big.journey.mjs#2/2", "small.journey.mjs"], 18, tmp_path, per_file=True)
    assert commands == [["node", str(tmp_path / "big.journey.mjs"),
                         "--case=big.journey.mjs-1@desktop-fast", "--case=big.journey.mjs-3@desktop-fast"],
                        ["node", str(tmp_path / "small.journey.mjs")]]
    assert ci.selected_cases(commands[0]) == ["big.journey.mjs-1@desktop-fast", "big.journey.mjs-3@desktop-fast"]
    assert ci.selected_cases(commands[1]) is None
    assert ci.entry_file("big.journey.mjs#2/2") == "big.journey.mjs"
    for bad in ("big.journey.mjs#3/2", "big.journey.mjs#1/6"):
        with pytest.raises(ValueError, match="no such part"):
            ci.part_cases(bad, tmp_path)


def test_one_shard_uses_run_all_when_it_exists(tmp_path):
    for name in ("a.journey.mjs", "b.journey.mjs"):
        (tmp_path / name).write_text("")
    files = ["a.journey.mjs", "b.journey.mjs"]
    assert ci.commands_for(files, 1, tmp_path) == [["node", str(tmp_path / n)] for n in files]
    (tmp_path / ci.RUN_ALL).write_text("")
    assert ci.commands_for(files, 1, tmp_path) == [["node", str(tmp_path / ci.RUN_ALL)]]
    # Sharded runs name each file: run-all would run every journey in every shard.
    assert ci.commands_for(["b.journey.mjs"], 2, tmp_path) == [["node", str(tmp_path / files[1])]]


@pytest.mark.parametrize("only,expected", [
    (None, ci.RUN_ALL), ("touch-targets", "touch-targets.journey.mjs"),
    ("antigravity-usage", "antigravity-usage.journey.mjs"),
])
def test_single_shard_launch_matches_its_fixture_selection(tmp_path, monkeypatch, only, expected):
    files = ["antigravity-usage.journey.mjs", "touch-targets.journey.mjs"]
    monkeypatch.setattr(ci, "plan", lambda shards, only=None: [files if only is None else only])
    launched, fixtures = [], []

    class Instance:
        status = tmp_path / "status.json"

        def __init__(self, out, ttl, *, fake_antigravity):
            fixtures.append(fake_antigravity)

        def wait_ready(self, timeout):
            return None

        def alive(self):
            return True

        def down(self):
            pass

    def run(command, *_):
        launched.append(command)
        return 0, 0.0

    monkeypatch.setattr(ci, "DevInstance", Instance)
    monkeypatch.setattr(ci, "run_command", run)
    argv = ["journeys", "--out", str(tmp_path)]
    if only is not None:
        argv += ["--only", only]
    assert ci.main(argv) == 0
    assert launched == [["node", str(ci.JOURNEYS / expected)]]
    assert fixtures == [only != "touch-targets"]


def test_a_plan_with_no_journeys_fails_closed(tmp_path):
    with pytest.raises(SystemExit):
        ci.plan(2, tmp_path)


def test_the_repository_journeys_are_all_planned():
    files = [path.name for path in ci.journey_files()]
    assert "new-worktree-session.journey.mjs" in files
    assert sorted(name for shard in ci.partition(dict.fromkeys(files, 1), 4)
                  for name in shard) == sorted(files)


def test_summary_lists_journeys_and_prints_failing_timelines(tmp_path):
    out = tmp_path / "journeys" / "flow"
    out.mkdir(parents=True)
    (out / "a@desktop-fast.txt").write_text("# a@desktop-fast  PASS  (10ms)\nok\n")
    (out / "b@phone-slow.txt").write_text("# b@phone-slow  FAIL  (12ms)\n 5ms  ! reversal ```x```\n")
    (out / "notes.txt").write_text("not a report")
    reports = ci.case_reports(tmp_path / "journeys")
    assert [(r["id"], r["ok"]) for r in reports] == [("a@desktop-fast", True),
                                                     ("b@phone-slow", False)]
    text = ci.summarize("shard 1/2", [
        {"name": "flow.journey.mjs", "code": 1, "seconds": 12.0},
        {"name": "other.journey.mjs", "code": None, "seconds": 0.0}], reports, None)
    assert "| `flow.journey.mjs` | FAIL (exit 1) | 12s |" in text
    assert "| `other.journey.mjs` | not run |" in text
    assert "Cases: 1/2 passed." in text
    assert "FAIL b@phone-slow" in text and "! reversal '''x'''" in text
    assert "a@desktop-fast" not in text.split("Cases:")[1]
    assert "could not be used" in ci.summarize("s", [], [], "seed failed")


def test_shard_argument():
    assert ci._shard("2/3") == (2, 3)
    for bad in ("0/3", "4/3", "x", "1"):
        with pytest.raises(argparse.ArgumentTypeError):
            ci._shard(bad)


def test_main_plan_prints_json(capsys, monkeypatch):
    monkeypatch.setattr(ci, "plan", lambda shards: [["a.journey.mjs"], []][:shards])
    assert ci.main(["plan", "--shards", "2"]) == 0
    assert json.loads(capsys.readouterr().out) == [["a.journey.mjs"], []]
