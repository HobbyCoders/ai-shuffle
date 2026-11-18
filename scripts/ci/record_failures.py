"""pytest plugin: list a run's failed tests for local_ci.run's one rerun.

local_ci.run (hosted Windows parts, Linux backend shards) reruns exactly the
tests in $LOCAL_CI_FAILED_FILE once. The list is written only after the whole
run returned TESTS_FAILED with no collection error, and after config cleanup
(scripts/ci/test_scratch.py removes its scratch there) finished: a collection
error, an interruption, a crash, a kill or a failed cleanup leaves no list, so
nothing is rerun and the command fails.

On the rerun, $LOCAL_CI_PASSED_FILE lists the tests whose call phase passed
(a skip, an xfail or an xpass is not a pass), written only when that run
exited 0, so a caller can check that every failed test really ran and passed.

Ids are written as an absolute file path plus the rest of the node id, so a
suite with its own rootdir (fluid/tests/unit) reruns from the checkout root.
Under pytest-xdist only the controller writes; it receives every worker's reports.
"""
import os

import pytest

_config = None
_failed: list[str] = []
_passed: list[str] = []
_collection_failed = False


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
    global _collection_failed
    if report.failed:
        _collection_failed = True


def pytest_runtest_logreport(report):
    if not _controller():
        return
    if report.failed:
        _failed.append(_target(report.nodeid))
    elif report.when == "call" and report.passed and not hasattr(report, "wasxfail"):
        _passed.append(_target(report.nodeid))


def _write(variable: str, tests: list[str]) -> None:
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
            _write("LOCAL_CI_PASSED_FILE", _passed)
    return status
