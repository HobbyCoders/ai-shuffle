"""pytest plugin: list a run's failed tests for local_ci.run's one rerun.

local_ci.run (hosted Windows parts, Linux backend shards) reruns exactly the
tests in $LOCAL_CI_FAILED_FILE once. The list is written only after the whole
run returned TESTS_FAILED with no collection error, and after config cleanup
(scripts/ci/test_scratch.py removes its scratch there) finished: a collection
error, an interruption, a crash, a kill or a failed cleanup leaves no list, so
nothing is rerun and the command fails.

On the rerun, $LOCAL_CI_PASSED_FILE lists the collected tests, and only when
the rerun was completely clean: it exited 0, every collected test reported
setup, call and teardown, each one passed (no xfail), nothing was deselected,
and the terminal summary counted nothing but passes (and warnings). Any other
outcome in any phase (a skip, xfail, xpass, error or failure) or any missing
report leaves no list. A caller compares the list with the failed tests.

Ids are written as an absolute file path plus the rest of the node id, so a
suite with its own rootdir (fluid/tests/unit) reruns from the checkout root.
Under pytest-xdist only the controller writes; it receives every worker's reports.
"""
import os

import pytest

PHASES = ["setup", "call", "teardown"]
# Terminal summary categories of a clean run: "" holds passed setup/teardown.
CLEAN_STATS = {"passed", "", "warnings"}

_config = None
_failed: list[str] = []
_collection_failed = False
# The rerun: what was collected, the passed phases of each test, anything else.
_collected: list[str] | None = None
_passed_phases: dict[str, list[str]] = {}
_unclean = False


def _target(nodeid: str) -> str:
    file, _, rest = nodeid.partition("::")
    target = os.path.join(str(_config.rootpath), file)
    return f"{target}::{rest}" if rest else target


def _controller() -> bool:
    return _config is not None and not hasattr(_config, "workerinput")


def pytest_configure(config):
    global _config
    _config = config


def pytest_collectreport(report):
    global _collection_failed, _unclean
    if report.failed:
        _collection_failed = True
    if not report.passed:
        _unclean = True


def pytest_deselected(items):
    global _unclean
    if items:
        _unclean = True


def pytest_collection_finish(session):
    global _collected
    _collected = [item.nodeid for item in session.items]


def pytest_runtest_logreport(report):
    global _unclean
    if not _controller():
        return
    if report.failed:
        _failed.append(_target(report.nodeid))
    if report.passed and not hasattr(report, "wasxfail"):
        _passed_phases.setdefault(report.nodeid, []).append(report.when)
    else:
        _unclean = True


def _clean_rerun() -> list[str] | None:
    """The collected tests, when every phase of every one passed and nothing else happened."""
    reporter = _config.pluginmanager.get_plugin("terminalreporter")
    if (_unclean or not _collected or reporter is None
            or not set(reporter.stats) <= CLEAN_STATS
            or set(_passed_phases) != set(_collected)
            or any(_passed_phases[nodeid] != PHASES for nodeid in _collected)):
        return None
    return [_target(nodeid) for nodeid in _collected]


def _write(variable: str, tests: list[str] | None) -> None:
    path = os.environ.get(variable)
    if path and tests:
        with open(path, "w", encoding="utf-8") as handle:
            handle.writelines(f"{test}\n" for test in tests)


@pytest.hookimpl(wrapper=True)
def pytest_cmdline_main(config):
    # Returns only after the session, unconfigure and config cleanup all did;
    # an exception from any of them propagates past the writes below.
    status = yield
    if _controller():
        if status == pytest.ExitCode.TESTS_FAILED and not _collection_failed:
            _write("LOCAL_CI_FAILED_FILE", _failed)
        if status == pytest.ExitCode.OK:
            _write("LOCAL_CI_PASSED_FILE", _clean_rerun())
    return status
