"""Stage a native release from source plus an already-built frontend.

No network calls: run_local.py --setup installs the vendored Python harness and
builds both Node tools from the included lockfiles on the target platform.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def stage_release(repo: Path, destination: Path) -> None:
    destination = destination.resolve()
    if destination.exists():
        raise ValueError("Release destination must be new; refusing to overwrite files")
    required = [
        "app/main.py",
        "frontend/build/index.html",
        "vendor/fluid/pyproject.toml",
        "tools/package-lock.json",
        "browser-mcp/package-lock.json",
    ]
    missing = [name for name in required if not (repo / name).is_file()]
    if missing:
        raise ValueError(f"Incomplete release inputs: {', '.join(missing)}")
    ignore = shutil.ignore_patterns(
        "node_modules",
        "dist",
        "__pycache__",
        "*.pyc",
        ".pytest_cache",
        ".env",
        ".env.*",
        "*.egg-info",
        ".git",
    )

    def source_ignore(directory: str, names: list[str]) -> set[str]:
        excluded = set(ignore(directory, names))
        if Path(directory) == repo / "app":
            excluded.add("static")  # never mix a local stale build into the archive
        return excluded

    for name in ("app", "vendor/fluid", "tools", "browser-mcp"):
        shutil.copytree(repo / name, destination / name, ignore=source_ignore)
    shutil.copytree(
        repo / "frontend/build", destination / "app/static", dirs_exist_ok=True
    )
    for name in (
        "requirements.txt",
        "requirements-native.txt",
        "run_local.py",
        ".env.example",
        "LICENSE",
    ):
        shutil.copy2(repo / name, destination / name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    stage_release(Path(__file__).resolve().parents[1], args.destination)
