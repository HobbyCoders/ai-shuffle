"""The npm audit gate blocks like --audit-level=high except for dated exceptions."""
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import npm_audit_gate as gate

TODAY = dt.date(2026, 11, 17)
PICO = "https://github.com/advisories/GHSA-fc5q-39xc-vjpf"


def advisory(name, severity, url=PICO, title="ReDoS"):
    return {"source": 1, "name": name, "dependency": name, "title": title,
            "url": url, "severity": severity, "range": "<=4.0.7"}


def report(*entries):
    return {"auditReportVersion": 2, "vulnerabilities": {
        name: {"name": name, "severity": severity, "via": via}
        for name, severity, via in entries}}


# picomatch carries the advisory; vite is "high" only because it depends on it.
PICOMATCH_ONLY = report(("picomatch", "high", [advisory("picomatch", "high")]),
                        ("vite", "high", ["picomatch", "tinyglobby"]))
EXCEPTION = [{"id": "GHSA-fc5q-39xc-vjpf", "packages": ["picomatch"], "reason": "dev only",
              "expires": dt.date(2026, 12, 1)}]


def test_excepted_advisory_and_its_dependents_pass():
    blocking, excepted, unused = gate.evaluate(PICOMATCH_ONLY, EXCEPTION, TODAY)
    assert blocking == [] and unused == []
    assert [(f["id"], f["package"]) for f in excepted] == [("GHSA-fc5q-39xc-vjpf", "picomatch")]


def test_without_the_exception_the_same_report_blocks():
    blocking, _, _ = gate.evaluate(PICOMATCH_ONLY, [], TODAY)
    assert [(f["id"], f["severity"]) for f in blocking] == [("GHSA-fc5q-39xc-vjpf", "high")]


def test_exception_expires():
    blocking, excepted, _ = gate.evaluate(PICOMATCH_ONLY, EXCEPTION, dt.date(2026, 12, 2))
    assert excepted == [] and blocking[0]["expired"] == "2026-12-01"
    assert gate.evaluate(PICOMATCH_ONLY, EXCEPTION, dt.date(2026, 12, 1))[0] == []


def test_other_advisories_still_block_even_on_an_excepted_package():
    other = "https://github.com/advisories/GHSA-aaaa-bbbb-cccc"
    data = report(("picomatch", "high", [advisory("picomatch", "high"), advisory("picomatch", "critical", other)]),
                  ("vite", "high", [advisory("vite", "high", other), "picomatch"]))
    blocking, _, _ = gate.evaluate(data, EXCEPTION, TODAY)
    assert sorted((f["id"], f["package"]) for f in blocking) == [
        ("GHSA-aaaa-bbbb-cccc", "picomatch"), ("GHSA-aaaa-bbbb-cccc", "vite")]


def test_same_advisory_on_an_unlisted_package_blocks():
    data = report(("forked-picomatch", "high", [advisory("forked-picomatch", "high")]))
    assert len(gate.evaluate(data, EXCEPTION, TODAY)[0]) == 1


def test_moderate_and_low_do_not_block_like_audit_level_high():
    other = "https://github.com/advisories/GHSA-mmmm-mmmm-mmmm"
    data = report(("hono", "moderate", [advisory("hono", "moderate", other)]),
                  ("x", "low", [advisory("x", "low", other)]))
    assert gate.evaluate(data, [], TODAY) == ([], [], [])


def test_unused_exception_is_reported():
    assert gate.evaluate(report(), EXCEPTION, TODAY) == ([], [], ["GHSA-fc5q-39xc-vjpf"])


@pytest.mark.parametrize("data", [None, {}, {"error": {"code": "ENOLOCK"}}, {"vulnerabilities": []}])
def test_unreadable_report_fails_closed(data):
    with pytest.raises(ValueError):
        gate.evaluate(data, EXCEPTION, TODAY)


def fake_run(stdout, returncode=1):
    def run(command, **kwargs):
        assert command == ["npx", "-y", "npm@11", "audit", "--json"]
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")
    return run


def test_main_exit_codes(capsys):
    assert gate.main(run=fake_run(json.dumps(PICOMATCH_ONLY)), today=TODAY) == 0
    assert "Excepted until 2026-12-01" in capsys.readouterr().out
    other = report(("lib", "critical", [advisory("lib", "critical", "https://github.com/advisories/GHSA-zzzz")]))
    assert gate.main(run=fake_run(json.dumps(other)), today=TODAY) == 1
    assert "::error::GHSA-zzzz in lib (critical)" in capsys.readouterr().out
    assert gate.main(run=fake_run("npm ERR! network"), today=TODAY) == 1
    assert gate.main(run=fake_run(json.dumps(report()), returncode=0), today=TODAY) == 0


def test_checked_in_exceptions_are_complete_and_dated():
    entries = gate.load_exceptions()
    for entry in entries:
        assert entry["id"].startswith("GHSA-") and entry["packages"]
        # An exception is a stopgap for an unpatched advisory, never open-ended.
        assert entry["expires"] - TODAY <= dt.timedelta(days=45)


def test_incomplete_exception_is_rejected(tmp_path):
    path = tmp_path / "exceptions.json"
    path.write_text(json.dumps([{"id": "GHSA-x", "packages": ["p"], "expires": "2026-12-01"}]))
    with pytest.raises(ValueError):
        gate.load_exceptions(path)
