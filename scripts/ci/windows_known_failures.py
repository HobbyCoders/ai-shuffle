"""pytest plugin: skip tests known to fail on Windows, so a Windows worker can still push.

scripts/local_ci.py loads this plugin (-p windows_known_failures) on Windows
only; it lives outside both `tests` packages so fluid and the backend can
both load it. Each line of
scripts/ci/windows_known_failures.txt names a test (`path::name`, without
parameters) or a whole file. A listed test is skipped with the tracking issue
as the reason; anything not listed still fails. Linux workers run every test.
Remove lines as the tests are fixed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIST = ROOT / "scripts/ci/windows_known_failures.txt"
ISSUE = "https://github.com/HobbyCoders/ai-shuffle/issues/2215"


def load(path: Path = LIST) -> tuple[set[str], set[str]]:
    tests, files = set(), set()
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            (tests if "::" in entry else files).add(entry)
    return tests, files


def base_id(nodeid: str) -> str:
    return nodeid.replace("\\", "/").split("[", 1)[0]


TESTS, FILES = load()


def pytest_ignore_collect(collection_path, config):
    try:
        relative = Path(collection_path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return None
    return True if relative in FILES else None


def pytest_collection_modifyitems(config, items):
    marker = pytest.mark.skip(reason=f"known Windows failure, {ISSUE}")
    for item in items:
        if base_id(item.nodeid) in TESTS:
            item.add_marker(marker)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if TESTS or FILES:
        terminalreporter.write_line(
            f"Not covered on Windows: {len(TESTS)} tests and {len(FILES)} files listed in "
            f"scripts/ci/windows_known_failures.txt ({ISSUE}).")
