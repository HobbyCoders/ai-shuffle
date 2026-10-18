"""One part of `local_ci.py full`, for the hosted Windows suite's parallel jobs.

windows-suite.yml runs every part at once on its own runner, so the suite
takes the time of its slowest part instead of the sum, and a failed part
reruns alone (`gh run rerun <id> --failed`). Together the parts run what
`full` runs, each test file exactly once:

  backend-1 .. backend-N  the backend fast subset, files dealt round robin
  frontend                the frontend suite (build, svelte-check, Vitest)
  rest                    the security gate, then every other suite

A failed pytest command reruns only its failed tests, once (local_ci.run with
LOCAL_CI_RETRY_FAILED=1). Tests that pass the second time pass the part but
are listed as flaky in the job summary and as warnings, so they still get
fixed. A collection error, more than local_ci.MAX_RETRIED failures, or a test
that fails twice fails the part. The `rest` part keeps going after a failed
suite and fails at the end, so one run shows every broken suite.

    python scripts/ci/windows_parts.py --list
    python scripts/ci/windows_parts.py backend-3
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import local_ci as ci  # noqa: E402
import local_ci_stages  # noqa: E402

BACKEND_PARTS = 8
SEPARATE = ("backend", "frontend")


def parts() -> list[str]:
    return [*(f"backend-{i + 1}" for i in range(BACKEND_PARTS)), "frontend", "rest"]


def backend_files(index: int) -> list[str]:
    """The test files of backend-<index + 1>; each backend file is in exactly one part."""
    files = [f for batch in local_ci_stages.backend_batches(ci) for f in batch]
    return files[index::BACKEND_PARTS]


def rest_suites() -> list[str]:
    return [suite.name for suite in ci.SUITES if suite.name not in SEPARATE]


def _shown(test: str) -> str:
    file, sep, rest = test.partition("::")
    try:
        file = Path(file).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        pass
    return file + sep + rest


def report_flaky(flaky: list[str]) -> None:
    shown = [_shown(test) for test in flaky]
    for test in shown:
        print(f"::warning title=Flaky test::{test} failed, then passed on a rerun")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and shown:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("### Flaky tests (failed once, passed on a rerun)\n\n")
            handle.writelines(f"- `{test}`\n" for test in shown)


def run_part(name: str, python: str) -> None:
    """Run one part. Raises ci.Failure if it fails."""
    if name.startswith("backend-") and name[len("backend-"):].isdigit():
        index = int(name[len("backend-"):]) - 1
        if not 0 <= index < BACKEND_PARTS:
            raise ci.Failure(f"no part {name}; choose one of {', '.join(parts())}")
        # map_hits set: the backend test map is per machine, so none is merged here.
        ctx = ci.Context(mode="full", base="HEAD", files=None, python=python,
                         backend_selected=backend_files(index),
                         map_hits=Path(tempfile.mkdtemp(prefix="local-ci-map-")))
        print(f"=== {name}: {len(ctx.backend_selected)} backend test files", flush=True)
        ci.backend(ctx)
    elif name == "frontend":
        ci.run_suites("full", ["frontend"], python)
    elif name == "rest":
        failures = []
        for step, call in [("security", lambda: ci.security_check(python)),
                           *((suite, lambda s=suite: ci.run_suites("full", [s], python))
                             for suite in rest_suites())]:
            try:
                call()
            except ci.Failure as error:
                print(f"\nFAILED {step}: {error}", file=sys.stderr, flush=True)
                failures.append(f"{step}: {error}")
        if failures:
            raise ci.Failure("; ".join(failures))
    else:
        raise ci.Failure(f"no part {name}; choose one of {', '.join(parts())}")


def main(argv: list[str]) -> int:
    if argv == ["--list"]:
        print("\n".join(parts()))
        return 0
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    os.environ[ci.RETRY_FAILED_ENV] = "1"
    python = ci.choose_python()
    try:
        ci.python_preflight(python)
        run_part(argv[0], python)
    except ci.Failure as error:
        report_flaky(ci.FLAKY)
        print(f"\nFAILED: {error}", file=sys.stderr)
        return 1
    report_flaky(ci.FLAKY)
    note = f" ({len(ci.FLAKY)} flaky test(s) passed on a rerun)" if ci.FLAKY else ""
    print(f"\nPart {argv[0]} passed{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
