"""Keep real-process failure evidence outside pytest's pruned temp roots."""

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    report = (yield).get_result()
    if not report.failed:
        return
    module = item.getparent(pytest.Module)
    preserve = getattr(module, "_boot_smoke_preserve_failure", None)
    if preserve is not None:
        try:
            destination = preserve()
        except OSError as exc:
            # Preserve the original test failure even when the disk is full.
            report.sections.append(("boot smoke artifacts", f"Could not preserve logs: {exc}"))
        else:
            report.sections.append(("boot smoke artifacts", f"Failed boot logs preserved at: {destination}"))
