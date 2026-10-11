"""Networked dependency reports; findings and unavailable audits warn by default."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIP_AUDIT_VERSION = "2.9.0"
NPM_PACKAGES = ("frontend", "android/hub", "browser-mcp", "desktop", "tools")


def command(argv, cwd=ROOT, timeout=180):
    # Do not echo registry URLs, environment, or stderr (may contain credentials).
    return subprocess.run([shutil.which(argv[0]) or argv[0], *argv[1:]], cwd=cwd,
                          capture_output=True, text=True, timeout=timeout, check=False)


def audit_python() -> Path:
    # Per checkout, ignored by git; never mutate the host interpreter.
    env = ROOT / "venv" / "local-audit"
    python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        result = command([sys.executable, "-m", "venv", str(env)])
        if result.returncode:
            raise RuntimeError("cannot create audit venv")
    installed = command([str(python), "-c",
                         "from importlib.metadata import version; print(version('pip-audit'))"])
    if installed.returncode or installed.stdout.strip() != PIP_AUDIT_VERSION:
        result = command([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                          "pip-audit==" + PIP_AUDIT_VERSION])
        if result.returncode:
            raise RuntimeError("cannot install pinned pip-audit")
    return python


def pip_summary(data):
    deps = data["dependencies"]
    affected = {dep["name"]: len(dep.get("vulns", [])) for dep in deps if dep.get("vulns")}
    skipped = sum(bool(dep.get("skip_reason")) for dep in deps)
    count = sum(affected.values())
    top = ", ".join(f"{name} ({n})" for name, n in
                    sorted(affected.items(), key=lambda item: (-item[1], item[0]))[:8]) or "none"
    # PyPI's advisory response has no severity: don't invent severity scores.
    return f"{count} advisories, severity unknown (PyPI); top packages: {top}; skipped: {skipped}", bool(count or skipped)


def npm_summary(data):
    counts = data["metadata"]["vulnerabilities"]
    severities = ("critical", "high", "moderate", "low", "info")
    names = sorted(data["vulnerabilities"], key=lambda name: (
        severities.index(data["vulnerabilities"][name]["severity"]), name))
    summary = ", ".join(f"{key}={counts.get(key, 0)}" for key in severities)
    top = ", ".join(f"{name} ({data['vulnerabilities'][name]['severity']})" for name in names[:8]) or "none"
    return f"{summary}; top packages: {top}", bool(counts.get("high", 0) or counts.get("critical", 0))


def audit(strict=False):
    failed = False
    try:
        python = audit_python()
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        python = None
    # Fluid has a pyproject rather than requirements.txt; audit its runtime deps
    # separately without publishing/building/installing the local package.
    manifests = [("requirements.txt", ROOT / "requirements.txt")]
    manifests += [(str(path.relative_to(ROOT)), path) for path in sorted((ROOT / "fluid").glob("requirements*.txt"))]
    with tempfile.TemporaryDirectory(prefix="local-audit-") as temp:
        project = ROOT / "fluid" / "pyproject.toml"
        if project.exists():
            requirements = Path(temp) / "fluid-requirements.txt"
            dependencies = tomllib.loads(project.read_text(encoding="utf-8"))["project"]["dependencies"]
            requirements.write_text("\n".join(dependencies) + "\n", encoding="utf-8")
            manifests.append(("fluid/pyproject.toml", requirements))
        for name, path in manifests:
            try:
                if python is None:
                    raise RuntimeError("pip-audit unavailable")
                result = command([str(python), "-m", "pip_audit", "-r", str(path),
                                  "--format=json", "--progress-spinner=off", "--timeout=30"])
                if result.returncode not in (0, 1):
                    raise RuntimeError("audit failed")
                summary, problem = pip_summary(json.loads(result.stdout))
            except (OSError, RuntimeError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
                summary, problem = "UNAVAILABLE (tool, resolver or advisory service failed; no clean bill of health)", True
            failed |= problem
            print(f"Dependency audit Python {name}: {summary}", flush=True)
    for directory in NPM_PACKAGES:
        if not (ROOT / directory / "package-lock.json").exists():
            continue
        try:
            result = command(["npm", "audit", "--audit-level=high", "--package-lock-only", "--ignore-scripts", "--json"],
                             cwd=ROOT / directory)
            if result.returncode not in (0, 1):
                raise RuntimeError("audit failed")
            data = json.loads(result.stdout)
            if data.get("error"):
                raise RuntimeError("registry audit failed")
            summary, problem = npm_summary(data)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            summary, problem = "UNAVAILABLE (tool or registry failed; no clean bill of health)", True
        failed |= problem
        print(f"Dependency audit npm {directory}: {summary}", flush=True)
    if failed:
        print("Dependency audit: " + ("FAILED (--strict-audit)" if strict else "WARNING only; triage findings, or use --strict-audit"))
    return int(strict and failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict-audit", action="store_true")
    raise SystemExit(audit(parser.parse_args().strict_audit))
