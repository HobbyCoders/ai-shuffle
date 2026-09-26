"""A fake ``gh`` executable for hermetic GitHub-integration tests.

``FakeGh`` writes a tiny Python script named ``gh`` into a temp ``bin``
directory and puts that directory first on ``PATH``, so the real
``GitHubService._run_gh`` -> ``subprocess.run(["gh", ...])`` path runs
unchanged and never reaches the network. Every invocation is appended to a
JSON-lines call log (argv, plus stdin when ``--body-file -`` is used), and the
reply is chosen by the first rule whose argv prefix matches.

Rules added later take precedence, so a test can start from ``signed_in()``
defaults and then override one command.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

_SCRIPT = """#!{python}
import json, os, sys
state = os.environ["FAKE_GH_DIR"]
args = sys.argv[1:]
stdin = sys.stdin.read() if "--body-file" in args else None
with open(os.path.join(state, "calls.jsonl"), "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"args": args, "stdin": stdin}}) + "\\n")
with open(os.path.join(state, "rules.json"), encoding="utf-8") as fh:
    rules = json.load(fh)
for rule in rules:
    prefix = rule["prefix"]
    if args[:len(prefix)] == prefix:
        sys.stdout.write(rule["stdout"])
        sys.stderr.write(rule["stderr"])
        sys.exit(rule["code"])
sys.stderr.write("fake gh: no rule for: " + " ".join(args))
sys.exit(1)
"""


def pr_json(
    number: int,
    head: str,
    *,
    title: str = "A pull request",
    state: str = "OPEN",
    base: str = "main",
    mergeable: str = "MERGEABLE",
    head_sha: str = "0" * 40,
    repo: str = "acme/app",
) -> dict[str, Any]:
    """One PR object in the shape ``gh pr list/view --json`` prints."""
    return {
        "number": number,
        "title": title,
        "body": f"body of #{number}",
        "state": state,
        "headRefName": head,
        "headRefOid": head_sha,
        "baseRefName": base,
        "author": {"login": "octo"},
        "url": f"https://github.com/{repo}/pull/{number}",
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-02T00:00:00Z",
        "mergeable": mergeable,
    }


def run_json(run_id: int, *, name: str = "CI", conclusion: str | None = "success") -> dict[str, Any]:
    """One workflow run in the shape ``gh run list/view --json`` prints."""
    return {
        "databaseId": run_id,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "headBranch": "main",
        "event": "push",
        "url": f"https://github.com/acme/app/actions/runs/{run_id}",
        "createdAt": "2026-01-01T00:00:00Z",
    }


class FakeGh:
    """Stub ``gh`` on PATH with a scripted reply table and a call log."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.dir = tmp_path / "fake-gh"
        bin_dir = self.dir / "bin"
        bin_dir.mkdir(parents=True)
        script = bin_dir / "gh"
        script.write_text(_SCRIPT.format(python=sys.executable), encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        monkeypatch.setenv("FAKE_GH_DIR", str(self.dir))
        self._rules: list[dict[str, Any]] = []
        self._save()
        (self.dir / "calls.jsonl").write_text("", encoding="utf-8")

    # -- rules ---------------------------------------------------------------
    def on(self, *prefix: str, stdout: Any = "", stderr: str = "", code: int = 0) -> FakeGh:
        if not isinstance(stdout, str):
            stdout = json.dumps(stdout)
        self._rules.insert(
            0, {"prefix": list(prefix), "stdout": stdout, "stderr": stderr, "code": code}
        )
        self._save()
        return self

    def signed_in(self, value: bool = True) -> FakeGh:
        return self.on("auth", "status", code=0 if value else 1,
                       stderr="" if value else "You are not logged into any GitHub hosts.")

    def _save(self) -> None:
        (self.dir / "rules.json").write_text(json.dumps(self._rules), encoding="utf-8")

    # -- call log ------------------------------------------------------------
    def calls(self, *prefix: str) -> list[dict[str, Any]]:
        lines = (self.dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()
        entries = [json.loads(line) for line in lines if line.strip()]
        return [e for e in entries if e["args"][: len(prefix)] == list(prefix)]

    def argvs(self, *prefix: str) -> list[list[str]]:
        return [e["args"] for e in self.calls(*prefix)]
