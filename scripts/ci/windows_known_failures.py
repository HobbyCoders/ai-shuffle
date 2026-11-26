"""pytest plugin: skip tests known to fail on Windows, so a Windows worker can still push.

scripts/local_ci.py loads this plugin (-p windows_known_failures) on Windows
only; it lives outside both `tests` packages so fluid and the backend can
both load it. Each line of
scripts/ci/windows_known_failures.txt names a test (`path::name`, without
parameters) or a whole file. A listed test is skipped, naming the list
as the reason; anything not listed still fails. Linux workers run every test.
Remove lines as the tests are fixed.
"""
from __future__ import annotations

import sysconfig
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STDLIB = Path(sysconfig.get_paths()["stdlib"]).resolve()
# Windows lets only an elevated process (or Developer Mode) create symlinks.
NO_SYMLINK_PRIVILEGE = 1314
SYMLINK_REASON = "this Windows shell may not create symlinks (needs elevation or Developer Mode)"
LIST = ROOT / "scripts/ci/windows_known_failures.txt"
# Each entry or group names why it fails on Windows; small fixes beat listing.
SOURCE = "scripts/ci/windows_known_failures.txt"


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
    marker = pytest.mark.skip(reason=f"known Windows failure, listed in {SOURCE}")
    for item in items:
        if repo_id(item) in TESTS:
            item.add_marker(marker)


def repo_id(item) -> str:
    """The item's id relative to the repository root.

    pytest ids are relative to the rootdir, which is fluid/ for the fluid
    suite, so the file part is rebuilt from the item's path.
    """
    try:
        relative = Path(item.path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return base_id(item.nodeid)
    name = item.nodeid.split("::", 1)[1] if "::" in item.nodeid else ""
    return base_id(f"{relative}::{name}")


def symlink_refused_in_test_code(excinfo) -> bool:
    """A test (or its fixture) could not make its own symlink: no privilege here.

    Only the test's own call counts. When app code makes the symlink, the
    error stays a failure, since a desktop install runs unelevated too.
    """
    if excinfo is None or getattr(excinfo.value, "winerror", None) != NO_SYMLINK_PRIVILEGE:
        return False
    for entry in reversed(excinfo.traceback):
        path = Path(str(entry.path)).resolve()
        if path.is_relative_to(STDLIB):
            continue  # pathlib's symlink_to and friends: look at their caller
        return path.is_relative_to(ROOT) and "tests" in path.relative_to(ROOT).parts
    return False


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    report = (yield).get_result()
    if report.failed and symlink_refused_in_test_code(call.excinfo):
        report.outcome = "skipped"
        report.longrepr = (str(item.path), item.location[1] or 0, f"Skipped: {SYMLINK_REASON}")


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if TESTS or FILES:
        terminalreporter.write_line(
            f"Not covered on Windows: {len(TESTS)} tests and {len(FILES)} files listed in "
            f"{SOURCE}.")
