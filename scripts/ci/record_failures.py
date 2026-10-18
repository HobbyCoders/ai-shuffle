"""pytest plugin: append each failed test's id to $LOCAL_CI_FAILED_FILE.

windows_parts.py reruns exactly these tests once. Ids are written as an
absolute file path plus the rest of the node id, so a suite with its own
rootdir (fluid/tests/unit) reruns from the checkout root. Under pytest-xdist
only the controller writes; it receives every worker's reports.
"""
import os

_config = None


def pytest_configure(config):
    global _config
    _config = config


def pytest_runtest_logreport(report):
    path = os.environ.get("LOCAL_CI_FAILED_FILE")
    if not path or not report.failed or _config is None or hasattr(_config, "workerinput"):
        return
    file, _, rest = report.nodeid.partition("::")
    target = os.path.join(str(_config.rootpath), file)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{target}::{rest}\n" if rest else f"{target}\n")
