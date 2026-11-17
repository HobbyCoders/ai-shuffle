"""Blocking npm audit with narrow, dated exceptions for unpatched advisories.

Run from a package directory. It runs the same full audit as before
(``npx -y npm@11 audit --json``: every locked package, dev dependencies
included) and fails on any high or critical advisory, exactly like
``npm audit --audit-level=high``, except an advisory listed in
``scripts/npm_audit_exceptions.json`` for the named package until its expiry
date. Packages that are only "vulnerable" because they depend on an excepted
package (vite through picomatch, say) pass with it; any other advisory on
them still fails.

An exception is for an advisory with no patched release. It needs a reason
and an expiry; an expired exception fails the audit again. When an exception
is no longer reported the gate says so, so the entry can be deleted.
Anything it cannot read (npm errors, unparsable output) fails closed.
"""
from __future__ import annotations

import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path

EXCEPTIONS = Path(__file__).resolve().with_name("npm_audit_exceptions.json")
AUDIT = ["npx", "-y", "npm@11", "audit", "--json"]
BLOCKING = {"high", "critical"}
TIMEOUT = 300


def load_exceptions(path=EXCEPTIONS):
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    for entry in entries:
        missing = {"id", "packages", "reason", "expires"} - entry.keys()
        if missing or not entry["packages"] or not str(entry["reason"]).strip():
            raise ValueError(f"npm audit exception {entry.get('id')!r} is incomplete: {sorted(missing)}")
        entry["expires"] = _dt.date.fromisoformat(entry["expires"])
    return entries


def advisories(report):
    """Every advisory npm reported, once per (advisory, package)."""
    if not isinstance(report, dict) or "error" in report or not isinstance(report.get("vulnerabilities"), dict):
        raise ValueError("npm audit did not return a readable report")
    found = {}
    for vulnerability in report["vulnerabilities"].values():
        for via in vulnerability.get("via", []):
            if isinstance(via, dict):
                advisory = str(via.get("url", "")).rstrip("/").rsplit("/", 1)[-1] or str(via.get("source"))
                found[(advisory, via.get("name"))] = {
                    "id": advisory, "package": via.get("name"),
                    "severity": via.get("severity"), "title": via.get("title", ""),
                }
    return list(found.values())


def evaluate(report, exceptions, today):
    """Return (blocking findings, excepted findings, unused exception ids)."""
    blocking, excepted, used = [], [], set()
    for finding in advisories(report):
        if finding["severity"] not in BLOCKING:
            continue
        match = next((entry for entry in exceptions
                      if entry["id"] == finding["id"] and finding["package"] in entry["packages"]), None)
        if match is None:
            blocking.append(finding)
        elif today > match["expires"]:
            blocking.append({**finding, "expired": match["expires"].isoformat()})
        else:
            used.add(match["id"])
            excepted.append({**finding, "expires": match["expires"].isoformat()})
    unused = sorted({entry["id"] for entry in exceptions} - used)
    return blocking, excepted, unused


def main(argv=None, run=subprocess.run, today=None):
    today = today or _dt.date.today()
    try:
        result = run(AUDIT, capture_output=True, text=True, timeout=TIMEOUT)
        if result.stderr.strip():
            sys.stderr.write(result.stderr)
        blocking, excepted, unused = evaluate(json.loads(result.stdout or "null"), load_exceptions(), today)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"::error::npm audit could not be evaluated: {error}")
        return 1
    for finding in excepted:
        print(f"::warning::Excepted until {finding['expires']}: {finding['id']} in {finding['package']} "
              f"({finding['severity']}) {finding['title']}")
    for advisory in unused:
        print(f"::notice::npm audit exception {advisory} is not reported for this package; "
              "delete it from scripts/npm_audit_exceptions.json once no package reports it")
    for finding in blocking:
        expired = f" (exception expired {finding['expired']})" if "expired" in finding else ""
        print(f"::error::{finding['id']} in {finding['package']} ({finding['severity']}) "
              f"{finding['title']}{expired}")
    if blocking:
        print(f"npm audit: {len(blocking)} blocking high or critical advisory(ies)")
        return 1
    print(f"npm audit: no blocking advisories ({len(excepted)} excepted)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
