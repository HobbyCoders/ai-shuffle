"""Run tasks × arms × trials, one at a time, in one shared workspace directory.

Every arm works in the same absolute directory (``--workspace``, also the app
project's path), refilled from the task's base snapshot before each run, so
paths in prompts, tool output and test runs are identical across arms. Runs
are strictly sequential: one shared workspace, one ChatGPT account, and a
shared host whose load would otherwise skew wall-clock numbers.

Arms:

* ``cc``    stock Claude Code (``claude -p``) through ``anthropic_proxy``.
* ``fluid`` bare fluid (``fluid_cli_arm``), no app.
* ``app``   a real AI Shuffle session on this worker, driven over loopback.

The arm order rotates per (task, trial), so no arm always runs first or last.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from scripts.harness_h2h import tasks as T
from scripts.harness_h2h import trace as TR
from scripts.harness_h2h.metered import default_token_path

REPO_ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
ARMS = ("cc", "fluid", "app")
APP_PROJECT_ID = "harness-h2h-ws"
APP_PROFILE_ID = "harness-h2h-bench"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _isolated_env(run_dir: Path) -> dict[str, str]:
    """No user config, no GitHub credentials: a fresh HOME per run."""
    home = run_dir / "home"
    (home / ".config").mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GH_", "GITHUB_", "ANTHROPIC_", "CLAUDE_", "OPENAI_", "FLUID_"))}
    env.update({
        "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
        "GH_CONFIG_DIR": str(home / ".config" / "gh"),
        "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    (home / ".gitconfig").write_text("[user]\n\tname = bench\n\temail = bench@localhost\n")
    return env


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:  # noqa: BLE001
            pass


def _pump(proc: subprocess.Popen, out_path: Path, deadline: float) -> tuple[int | None, bool]:
    """Copy stdout lines to ``out_path`` stamped with receipt time."""
    timed_out = False
    import selectors
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    buf = b""
    with out_path.open("w", encoding="utf-8") as fh:
        while True:
            if time.time() > deadline:
                timed_out = True
                _kill_group(proc)
                break
            events = sel.select(timeout=1.0)
            if not events:
                if proc.poll() is not None:
                    break
                continue
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                fh.write(json.dumps({"t": time.time(), "line": line.decode(errors="replace")}) + "\n")
                fh.flush()
        if buf:
            fh.write(json.dumps({"t": time.time(), "line": buf.decode(errors="replace")}) + "\n")
    try:
        code = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        code = None
    return code, timed_out


# ─── arms ───────────────────────────────────────────────────────────────────


def run_cc(task: T.Task, ws: Path, run_dir: Path, *, model: str, effort: str, token: Path) -> dict:
    env = _isolated_env(run_dir)
    port = _free_port()
    proxy_env = {**os.environ, "PYTHONPATH": f"{REPO_ROOT / 'fluid'}:{REPO_ROOT}"}
    proxy = subprocess.Popen(
        [PY, "-m", "scripts.harness_h2h.anthropic_proxy", "--port", str(port), "--model", model,
         "--effort", effort, "--log", str(run_dir / "calls.jsonl"),
         "--request-log", str(run_dir / "requests.jsonl"), "--token", str(token)],
        cwd=REPO_ROOT, env=proxy_env, stdout=(run_dir / "proxy.log").open("w"),
        stderr=subprocess.STDOUT, start_new_session=True)
    try:
        import httpx
        for _ in range(100):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            raise RuntimeError("proxy did not start")
        cfg = run_dir / "home" / ".claude"
        cfg.mkdir(parents=True, exist_ok=True)
        env.update({
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
            "ANTHROPIC_API_KEY": "sk-ant-h2h-proxy",
            "CLAUDE_CONFIG_DIR": str(cfg),
            "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        })
        claude = shutil.which("claude") or "claude"
        t0 = time.time()
        proc = subprocess.Popen(
            [claude, "-p", task.prompt, "--output-format", "stream-json", "--verbose",
             "--dangerously-skip-permissions"],
            cwd=ws, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True)
        code, timed_out = _pump(proc, run_dir / "stream.jsonl", t0 + task.timeout_s)
        t1 = time.time()
    finally:
        _kill_group(proxy)
    tr = TR.from_claude_stream(run_dir / "stream.jsonl", run_dir / "calls.jsonl")
    return {"t_start": t0, "t_end": t1, "exit": code, "timed_out": timed_out, "trace": tr}


def run_fluid(task: T.Task, ws: Path, run_dir: Path, *, model: str, effort: str, token: Path) -> dict:
    env = _isolated_env(run_dir)
    env["PYTHONPATH"] = f"{REPO_ROOT / 'fluid'}:{REPO_ROOT}"
    prompt_file = run_dir / "prompt.txt"
    store = run_dir / "store"
    t0 = time.time()
    proc = subprocess.Popen(
        [PY, "-m", "scripts.harness_h2h.fluid_cli_arm", "--cwd", str(ws), "--prompt-file",
         str(prompt_file), "--store", str(store), "--calls", str(run_dir / "calls.jsonl"),
         "--model", model, "--effort", effort, "--token", str(token)],
        cwd=REPO_ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, start_new_session=True)
    code, timed_out = _pump(proc, run_dir / "stream.jsonl", t0 + task.timeout_s)
    t1 = time.time()
    tr = TR.from_fluid_store(store, run_dir / "calls.jsonl", run_dir / "stream.jsonl")
    return {"t_start": t0, "t_end": t1, "exit": code, "timed_out": timed_out, "trace": tr}


def run_app(task: T.Task, ws: Path, run_dir: Path, *, model: str, effort: str, app_url: str) -> dict:
    from scripts.harness_h2h.app_client import AppClient
    client = AppClient(app_url)
    try:
        client.ensure_project(APP_PROJECT_ID, str(ws))
        t0 = time.time()
        sid = client.start(task.prompt, project=APP_PROJECT_ID, profile=APP_PROFILE_ID,
                           model=model, effort=effort)
        (run_dir / "session_id").write_text(sid)
        done, timed_out = client.wait_idle(sid, deadline=t0 + task.timeout_s)
        t1 = time.time()
        if timed_out:
            client.stop(sid)
        events = client.events(sid)
        (run_dir / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        session = client.session(sid)
        (run_dir / "session.json").write_text(json.dumps(session, default=str)[:5_000_000])
        try:
            client.archive(sid)
        except Exception:  # noqa: BLE001
            pass
    finally:
        client.close()
    tr = TR.from_app_events(events, session)
    return {"t_start": t0, "t_end": done or t1, "exit": 0 if not timed_out else None,
            "timed_out": timed_out, "trace": tr, "session_id": sid}


# ─── matrix ─────────────────────────────────────────────────────────────────


def _order(arms: list[str], task_index: int, trial: int) -> list[str]:
    k = (task_index + trial) % len(arms)
    return arms[k:] + arms[:k]


def run_one(task: T.Task, arm: str, trial: int, *, out: Path, workspace: Path, model: str,
            effort: str, token: Path, app_url: str) -> dict:
    run_dir = out / "runs" / task.id / arm / f"t{trial}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    (run_dir / "prompt.txt").write_text(task.prompt, encoding="utf-8")
    T.snapshot(task.repo, task.base, workspace)
    result: dict[str, Any] = {"task": task.id, "arm": arm, "trial": trial, "model": model,
                              "effort": effort, "started_at": time.time(), "run_id": uuid.uuid4().hex}
    try:
        if arm == "cc":
            res = run_cc(task, workspace, run_dir, model=model, effort=effort, token=token)
        elif arm == "fluid":
            res = run_fluid(task, workspace, run_dir, model=model, effort=effort, token=token)
        elif arm == "app":
            res = run_app(task, workspace, run_dir, model=model, effort=effort, app_url=app_url)
        else:
            raise ValueError(arm)
        result.update({k: v for k, v in res.items() if k != "trace"})
        result["trace"] = res["trace"]
        result["wall_s"] = round(res["t_end"] - res["t_start"], 2)
    except Exception as exc:  # noqa: BLE001
        result["harness_error"] = f"{type(exc).__name__}: {exc}"
    try:
        grade = T.grade(task, workspace)
    except Exception as exc:  # noqa: BLE001
        grade = {"passed": False, "score": 0.0, "grade_error": f"{type(exc).__name__}: {exc}"}
    patch = (grade.get("diff") or {}).pop("patch", "")
    (run_dir / "agent.patch").write_text(patch, encoding="utf-8")
    result["grade"] = grade
    result["contamination"] = TR.contamination((result.get("trace") or {}).get("tools", []))
    (run_dir / "result.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return result


def run_matrix(*, tasks: list[T.Task], arms: list[str], trials: int, out: Path, workspace: Path,
               model: str, effort: str, token: Path | None, app_url: str, skip_done: bool = True) -> int:
    token = token or default_token_path()
    out.mkdir(parents=True, exist_ok=True)
    (out / "config.json").write_text(json.dumps({
        "arms": arms, "trials": trials, "model": model, "effort": effort,
        "workspace": str(workspace), "tasks": [t.id for t in tasks],
        "claude_version": subprocess.run(["claude", "--version"], capture_output=True,
                                         text=True).stdout.strip(),
        "repo_head": subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                                    capture_output=True, text=True).stdout.strip(),
    }, indent=2))
    for trial in range(trials):
        for ti, task in enumerate(tasks):
            for arm in _order(arms, ti, trial):
                done = out / "runs" / task.id / arm / f"t{trial}" / "result.json"
                if skip_done and done.exists():
                    continue
                print(f"[{time.strftime('%H:%M:%S')}] {task.id} {arm} t{trial} ...", flush=True)
                r = run_one(task, arm, trial, out=out, workspace=workspace, model=model,
                            effort=effort, token=token, app_url=app_url)
                g = r.get("grade") or {}
                tr = r.get("trace") or {}
                print(f"    passed={g.get('passed')} wall={r.get('wall_s')}s "
                      f"calls={len(tr.get('calls', []))} tools={len(tr.get('tools', []))} "
                      f"err={r.get('harness_error') or ''}", flush=True)
    T.clear_dir(workspace)
    return 0
