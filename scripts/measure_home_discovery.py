"""Reproduce #134 with synthetic HOME; no user files or model calls.

Run: PYTHONPATH=fluid python scripts/measure_home_discovery.py
Prints exact compiled instruction/skill prefix sections (characters and the
harness chars/4 token estimate), not provider-billed tokens or app tool schemas.
The other per-call sections and tools are unaffected by this discovery fix.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path


def load_discovery_revision(revision: str) -> None:
    """Compare the same compiler/fixture against tracked discovery source only."""
    repo = Path(__file__).resolve().parents[1]
    for name in ("instructions", "agents"):
        path = f"fluid/fluid/context/{name}.py"
        source = subprocess.check_output(["git", "show", f"{revision}:{path}"], cwd=repo, text=True)
        qualified = f"fluid.context.{name}"
        module = types.ModuleType(qualified)
        module.__file__ = str(repo / path)
        sys.modules[qualified] = module
        exec(compile(source, module.__file__, "exec"), module.__dict__)  # noqa: S102 — explicit tracked revision comparison


def measure(outside: str | None = None):
    from fluid.context.compiler import PromptInputs, compile_prompt
    from fluid.context.instructions import (
        discover_instruction_files,
        skill_search_roots,
    )
    from fluid.context.trim import estimate_tokens_of_text
    with tempfile.TemporaryDirectory(prefix="fluid-home-budget-") as temp:
        home = Path(temp) / "home"
        cwd = Path(temp) / "outside" if outside else home / "projects" / "foo"
        cwd.mkdir(parents=True)
        home.mkdir(exist_ok=True)
        if outside:
            (cwd / "AGENTS.md").write_text("Outside project guidance.\n", encoding="utf-8")
        if outside == "git":
            (cwd / ".git").mkdir()
        config = home / ".fluid"
        config.mkdir()
        (config / "AGENTS.md").write_text("Keep every safety rule.\n", encoding="utf-8")
        skill = config / "skills" / "example"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: example\ndescription: Example global skill.\n---\nDeferred body.\n",
            encoding="utf-8",
        )
        previous = {key: os.environ.get(key) for key in ("HOME", "USERPROFILE")}
        try:
            os.environ.update(HOME=str(home), USERPROFILE=str(home))
            prompt = compile_prompt(PromptInputs(
                cwd=cwd, include_env=False, include_agents=False, include_plugins=False,
            ))
            # Normalize the disposable path only for comparable provenance lengths.
            sections = [{"kind": s.kind, "scope": s.scope,
                         "text": s.text.replace(str(home), "/tmp/h")}
                        for s in prompt.sections if s.kind in ("memory", "skills")]
            for section in sections:
                text = section.pop("text")
                section.update(chars=len(text), estimated_tokens=estimate_tokens_of_text(text))
            normalized = (prompt.text or "").replace(str(home), "/tmp/h").replace(str(temp), "/tmp/fixture")
            return {
                "compiled_sha256": hashlib.sha256(normalized.encode()).hexdigest(),
                "compiled_chars": len(normalized),
                "sections": sections,
                "total_chars": sum(s["chars"] for s in sections),
                "estimated_tokens": sum(s["estimated_tokens"] for s in sections),
                "instruction_scopes": [f.scope for f in discover_instruction_files(cwd)],
                "home_skill_scopes": [scope for path, scope in skill_search_roots(cwd)
                                      if path.resolve() == (config / "skills").resolve()],
            }
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", help="load discovery modules from this git revision")
    parser.add_argument("--outside", choices=("git", "non-git"), help="compare a cwd outside HOME")
    args = parser.parse_args()
    if args.revision:
        load_discovery_revision(args.revision)
    print(json.dumps(measure(args.outside), indent=2))
