"""Fail-closed pip-audit with bounded retries for PyPI request timeouts only.

Three attempts share 180 seconds (including 2s/4s backoff); each HTTP request
has a 30-second timeout. Findings, unknown failures and process deadlines are
never retried. Process-tree cleanup has its own bounded grace (up to 30 seconds
on Windows). The CLI preserves both output streams; local reports may redact
raw diagnostics as before.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

from verification_evidence import _terminate_tree

TOTAL_TIMEOUT = 180
BACKOFF = (2, 4)
NETWORK_TIMEOUT = 30
_PYPI_TIMEOUT = re.compile(
    r"^requests\.exceptions\.(?:ReadTimeout|ConnectTimeout): "
    r"HTTPSConnectionPool\(host=['\"]pypi\.org['\"], port=443\): .+$"
)


def _retryable(result):
    # Never turn a partial findings report into success on a later attempt.
    # A generic 'timeout' substring could instead be a resolver/configuration
    # error, TLS failure, or text in an advisory. Match the final exception only.
    lines = result.stderr.rstrip().splitlines()
    return (result.returncode == 1 and not result.stdout.strip() and bool(lines)
            and not re.search(r"Found \d+ known vulnerabilit", result.stderr)
            and _PYPI_TIMEOUT.fullmatch(lines[-1]) is not None)


def _text(output):
    # TimeoutExpired can carry bytes even when the subprocess uses text=True.
    return output.decode("utf-8", errors="replace") if isinstance(output, bytes) else output or ""


def _run_once(argv, *, cwd, timeout):
    # pip-audit can spawn pip's resolver. A direct-child timeout leaves it
    # running (and on Windows can leave capture pipes open). Reuse the existing
    # bounded tree cleanup: at most 30s beyond the audit deadline on Windows.
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
               if os.name == "nt" else {"start_new_session": True})
    with subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, errors="replace", **options) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:
            _terminate_tree(process)
            raise
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def run_audit(arguments, *, python=sys.executable, cwd=None):
    argv = [str(python), "-m", "pip_audit", *arguments,
            f"--timeout={NETWORK_TIMEOUT}", "--progress-spinner=off"]
    deadline = time.monotonic() + TOTAL_TIMEOUT
    diagnostics = ""
    for attempt in range(len(BACKOFF) + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return subprocess.CompletedProcess(argv, 124, "", diagnostics +
                                               "pip-audit: total audit deadline exceeded\n")
        try:
            result = _run_once(argv, cwd=cwd, timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            return subprocess.CompletedProcess(argv, 124, _text(exc.stdout), diagnostics +
                                               _text(exc.stderr) +
                                               f"\npip-audit: total {TOTAL_TIMEOUT}s audit deadline exceeded\n")
        except OSError as exc:
            return subprocess.CompletedProcess(argv, 127, "", diagnostics + f"pip-audit: {exc}\n")
        diagnostics += result.stderr
        if not _retryable(result) or attempt == len(BACKOFF):
            return subprocess.CompletedProcess(argv, result.returncode, result.stdout, diagnostics)
        delay = BACKOFF[attempt]
        # Do not sleep or launch another subprocess outside the total budget.
        if deadline - time.monotonic() <= delay:
            return subprocess.CompletedProcess(argv, result.returncode, result.stdout, diagnostics +
                                               "\npip-audit: insufficient retry budget\n")
        diagnostics += (f"\npip-audit: PyPI request timeout; retry {attempt + 2}/{len(BACKOFF) + 1} "
                        f"in {delay}s (total budget {TOTAL_TIMEOUT}s)\n")
        time.sleep(delay)


def main(arguments):
    result = run_audit(arguments)
    print(result.stdout, end="", file=sys.stdout, flush=True)
    print(result.stderr, end="", file=sys.stderr, flush=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
