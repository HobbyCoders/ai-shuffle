"""Run one Linux backend shard's pytest command; rerun its failed tests once.

ci-backend.yml's backend-shard job runs its pytest command through this, with
the hosted Windows parts' retry (local_ci.run with LOCAL_CI_RETRY_FAILED=1): a
failed command reruns only its failed tests, once, serially. Tests that pass
the second time pass the shard but are listed as flaky in the job summary and
as warnings (windows_parts.report_flaky), so they still get fixed. A collection
error, more than local_ci.MAX_RETRIED failures, or a test that fails twice
fails the shard.

The shard plugin (scripts/ci/backend_shards.py) writes its receipt for a
complete run whose only fault was failed tests, listing them under ``failed``.
scripts/ci/record_failures.py lists them for the rerun only after that pytest
process, its config cleanup included, returned normally. After the rerun this
marks the receipt with ``rerun_passed``, and only when the rerun ran exactly
the tests the receipt lists and each one passed its call phase (a skip or an
xfail is not a pass); backend_gate.py refuses a receipt whose failed tests
have no passing rerun. The first run's
JUnit report and --durations output stay as they were, failures included.

    python scripts/ci/backend_shard_run.py -- python -m pytest ... -p scripts.ci.backend_shards --backend-shard 3
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "ci"))

import local_ci as ci  # noqa: E402
from windows_parts import _shown, report_flaky  # noqa: E402


def mark_rerun(receipt_path: Path, rerun: list[str], passed: list[str]) -> None:
    """Record on the shard receipt that its failed tests passed their rerun.

    ``rerun`` is what was rerun; ``passed`` is what passed its call phase there.
    Both must equal the receipt's failed tests: a rerun exits 0 when a test
    skips, xfails or is not collected, and none of those is a pass.
    """
    if not receipt_path.is_file():
        raise ValueError(f"no shard receipt at {receipt_path} to record the rerun on")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    failed = sorted(receipt.get("failed", []))
    if sorted(set(rerun)) != failed:
        raise ValueError("the rerun tests differ from the tests the shard receipt lists as failed")
    if sorted(set(passed)) != failed:
        raise ValueError("the rerun did not pass exactly the failed tests "
                         f"(passed: {sorted(set(passed))}; failed: {failed})")
    receipt["rerun_passed"] = failed
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", type=Path, default=ROOT / "backend-results",
                        help="the shard plugin's --backend-results directory")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- python -m pytest ...")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if command[1:3] != ["-m", "pytest"]:
        parser.error("the command must be: <python> -m pytest ...")
    os.environ[ci.RETRY_FAILED_ENV] = "1"
    try:
        ci.run(command)
        if ci.FLAKY:
            mark_rerun(args.results / "receipt.json", [_shown(test) for test in ci.FLAKY],
                       [_shown(test) for test in ci.RERUN_PASSED])
    except (ci.Failure, ValueError) as error:
        # FLAKY is set only by a rerun that exited 0; that rerun did not pass here.
        print(f"\nFAILED: {error}", file=sys.stderr)
        return 1
    report_flaky(ci.FLAKY)
    note = f" ({len(ci.FLAKY)} flaky test(s) passed on a rerun)" if ci.FLAKY else ""
    print(f"\nBackend shard passed{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
