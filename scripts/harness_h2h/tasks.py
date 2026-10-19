"""Bench tasks: history-free snapshots, hidden-test grading, validation.

A task is a real fix from a repository's history. The agent gets the tree at
the commit before the fix, with no git history (``git archive`` into a fresh
``git init``), so the fix cannot be read out of later commits. The grader is
the fix's own test files: they are copied in from the fix commit only after
the agent finishes, overwriting whatever the agent left there, and run.

``validate`` proves each task is gradable: the hidden tests must FAIL on the
base snapshot and PASS on the fix snapshot.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
TASKS_FILE = HERE / "tasks.json"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "bench", "GIT_AUTHOR_EMAIL": "bench@localhost",
    "GIT_COMMITTER_NAME": "bench", "GIT_COMMITTER_EMAIL": "bench@localhost",
}


@dataclass
class Task:
    id: str
    title: str
    kind: str  # "bugfix" | "question"
    repo: str
    base: str
    prompt: str
    fix: str | None = None
    hidden_tests: list[str] = field(default_factory=list)
    test_cmd: str = ""
    test_timeout_s: int = 900
    timeout_s: int = 2400
    # question tasks: regexes the answer file must match (all of them)
    answer_file: str = "ANSWER.md"
    answer_checks: list[str] = field(default_factory=list)
    answer_forbid: list[str] = field(default_factory=list)
    source: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Task:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def load_tasks(path: Path = TASKS_FILE) -> list[Task]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Task.from_dict(t) for t in data["tasks"]]


def _run(cmd: list[str] | str, *, cwd: Path | None = None, env: dict | None = None,
         timeout: float | None = None, shell: bool = False, input: bytes | None = None
         ) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout, shell=shell, input=input,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def clear_dir(path: Path) -> None:
    """Empty ``path`` in place (the app project keeps pointing at it)."""
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def snapshot(repo: str, sha: str, dest: Path) -> None:
    """``dest`` holds ``sha``'s tree as a one-commit repository, nothing else."""
    clear_dir(dest)
    archive = _run(["git", "-C", repo, "archive", "--format=tar", sha])
    if archive.returncode != 0:
        raise RuntimeError(f"git archive {sha} failed: {archive.stdout[-500:]!r}")
    untar = _run(["tar", "-x", "-C", str(dest)], input=archive.stdout)
    if untar.returncode != 0:
        raise RuntimeError(f"untar failed: {untar.stdout[-500:]!r}")
    env = {**os.environ, **_GIT_ENV}
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"],
                ["git", "commit", "-q", "--no-verify", "-m", "base"]):
        r = _run(cmd, cwd=dest, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} failed: {r.stdout[-500:]!r}")


def install_hidden_tests(task: Task, dest: Path) -> list[str]:
    """Copy the fix commit's version of each hidden test file into ``dest``."""
    installed = []
    for rel in task.hidden_tests:
        show = _run(["git", "-C", task.repo, "show", f"{task.fix}:{rel}"])
        if show.returncode != 0:
            raise RuntimeError(f"hidden test {rel} missing at {task.fix}")
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(show.stdout)
        installed.append(rel)
    return installed


def diff_stats(dest: Path) -> dict[str, Any]:
    """What the agent changed against the base commit (tracked + new files)."""
    _run(["git", "add", "-A"], cwd=dest)
    stat = _run(["git", "diff", "--cached", "--numstat", "HEAD"], cwd=dest)
    files, added, removed = [], 0, 0
    for line in stat.stdout.decode(errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            a, r, name = parts
            files.append(name)
            added += int(a) if a.isdigit() else 0
            removed += int(r) if r.isdigit() else 0
    patch = _run(["git", "diff", "--cached", "HEAD"], cwd=dest).stdout.decode(errors="replace")
    _run(["git", "reset", "-q"], cwd=dest)
    return {"files": files, "added": added, "removed": removed, "patch": patch}


_SUMMARY_RE = re.compile(r"(\d+) (passed|failed|error|errors|skipped)")


def run_tests(task: Task, dest: Path) -> dict[str, Any]:
    cmd = task.test_cmd.format(tests=" ".join(task.hidden_tests))
    t0 = time.time()
    try:
        r = _run(cmd, cwd=dest, shell=True, timeout=task.test_timeout_s,
                 env={**os.environ, "AI_SHUFFLE_PYTEST_GATE": os.environ.get("AI_SHUFFLE_PYTEST_GATE", "1")})
        out = r.stdout.decode(errors="replace")
        code = r.returncode
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode(errors="replace") + "\n[grader timeout]"
        code = -9
    counts: dict[str, int] = {}
    for n, word in _SUMMARY_RE.findall(out[-3000:]):
        key = "error" if word.startswith("error") else word
        counts[key] = counts.get(key, 0) + int(n)
    return {"exit": code, "passed": code == 0, "counts": counts,
            "seconds": round(time.time() - t0, 1), "tail": out[-4000:]}


def grade(task: Task, dest: Path) -> dict[str, Any]:
    """Grade the finished workspace ``dest``. Diff is taken before tests go in."""
    diff = diff_stats(dest)
    if task.kind == "question":
        answer_path = dest / task.answer_file
        text = answer_path.read_text(encoding="utf-8", errors="replace") if answer_path.exists() else ""
        hits = {rx: bool(re.search(rx, text, re.IGNORECASE | re.MULTILINE)) for rx in task.answer_checks}
        forbidden = {rx: bool(re.search(rx, text, re.IGNORECASE | re.MULTILINE)) for rx in task.answer_forbid}
        score = sum(hits.values()) / len(hits) if hits else 0.0
        return {"kind": "question", "passed": bool(text) and all(hits.values()) and not any(forbidden.values()),
                "score": round(score, 3), "checks": hits, "forbidden": forbidden,
                "answer_chars": len(text), "answer": text[:6000], "diff": diff}
    install_hidden_tests(task, dest)
    tests = run_tests(task, dest)
    return {"kind": "bugfix", "passed": tests["passed"], "score": 1.0 if tests["passed"] else 0.0,
            "tests": tests, "diff": diff}


def validate(task: Task, scratch: Path) -> dict[str, Any]:
    """Hidden tests must fail on base and pass on the fix."""
    if task.kind == "question":
        return {"task": task.id, "ok": True, "note": "question task: graded by answer checks"}
    ws = scratch / f"validate-{task.id}"
    snapshot(task.repo, task.base, ws)
    install_hidden_tests(task, ws)
    before = run_tests(task, ws)
    snapshot(task.repo, task.fix, ws)
    after = run_tests(task, ws)
    shutil.rmtree(ws, ignore_errors=True)
    ok = (not before["passed"]) and after["passed"]
    return {"task": task.id, "ok": ok,
            "base": {"exit": before["exit"], "counts": before["counts"], "s": before["seconds"]},
            "fix": {"exit": after["exit"], "counts": after["counts"], "s": after["seconds"]},
            "base_tail": before["tail"][-1500:] if not ok else "",
            "fix_tail": after["tail"][-1500:] if not ok else ""}
