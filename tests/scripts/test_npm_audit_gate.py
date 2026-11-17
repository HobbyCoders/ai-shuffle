"""The npm audit gate blocks like --audit-level=high except for dated, dev-only exceptions."""
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
OTHER = "https://github.com/advisories/GHSA-aaaa-bbbb-cccc"


def advisory(name, severity, url=PICO, title="ReDoS", source=1):
    return {"source": source, "name": name, "dependency": name, "title": title,
            "url": url, "severity": severity, "range": "<=4.0.7"}


def report(*entries):
    return {"auditReportVersion": 2, "vulnerabilities": {
        name: {"name": name, "severity": severity, "via": via, "nodes": [f"node_modules/{name}"]}
        for name, severity, via in entries}}


def lock(*names, dev=True):
    return {f"node_modules/{name}": ({"dev": True} if dev else {}) for name in names}


DEV = lock("picomatch", "vite", "forked-picomatch")
# picomatch carries the advisory; vite is "high" only because it depends on it.
PICOMATCH_ONLY = report(("picomatch", "high", [advisory("picomatch", "high")]),
                        ("vite", "high", ["picomatch", "tinyglobby"]))
EXCEPTION = [{"id": "GHSA-fc5q-39xc-vjpf", "packages": ["picomatch"], "reason": "dev only",
              "expires": dt.date(2026, 12, 1)}]


def test_excepted_advisory_and_its_dependents_pass():
    blocking, excepted, unused = gate.evaluate(PICOMATCH_ONLY, EXCEPTION, TODAY, DEV)
    assert blocking == [] and unused == []
    assert [(f["id"], f["package"]) for f in excepted] == [("GHSA-fc5q-39xc-vjpf", "picomatch")]


def test_without_the_exception_the_same_report_blocks():
    blocking, _, _ = gate.evaluate(PICOMATCH_ONLY, [], TODAY, DEV)
    assert [(f["id"], f["severity"]) for f in blocking] == [("GHSA-fc5q-39xc-vjpf", "high")]


def test_exception_expires():
    blocking, excepted, _ = gate.evaluate(PICOMATCH_ONLY, EXCEPTION, dt.date(2026, 12, 2), DEV)
    assert excepted == [] and blocking[0]["why"] == "exception expired 2026-12-01"
    assert gate.evaluate(PICOMATCH_ONLY, EXCEPTION, dt.date(2026, 12, 1), DEV)[0] == []


@pytest.mark.parametrize("packages", [lock("picomatch", dev=False), {}, None])
def test_exception_never_covers_a_runtime_or_unknown_copy(packages):
    blocking, excepted, _ = gate.evaluate(PICOMATCH_ONLY, EXCEPTION, TODAY, packages)
    assert excepted == [] and "dev-only" in blocking[0]["why"]


def test_one_runtime_copy_among_dev_copies_blocks():
    data = report(("picomatch", "high", [advisory("picomatch", "high")]))
    data["vulnerabilities"]["picomatch"]["nodes"].append("node_modules/x/node_modules/picomatch")
    packages = {**DEV, "node_modules/x/node_modules/picomatch": {}}
    assert len(gate.evaluate(data, EXCEPTION, TODAY, packages)[0]) == 1


def test_other_advisories_still_block_even_on_an_excepted_package():
    data = report(("picomatch", "high", [advisory("picomatch", "high"), advisory("picomatch", "critical", OTHER)]),
                  ("vite", "high", [advisory("vite", "high", OTHER), "picomatch"]))
    blocking, _, _ = gate.evaluate(data, EXCEPTION, TODAY, DEV)
    assert sorted((f["id"], f["package"]) for f in blocking) == [
        ("GHSA-aaaa-bbbb-cccc", "picomatch"), ("GHSA-aaaa-bbbb-cccc", "vite")]


def test_entries_sharing_an_id_are_each_evaluated():
    data = report(("lib", "high", [advisory("lib", "high", OTHER, source=1),
                                   advisory("lib", "moderate", OTHER, source=2)]))
    assert [f["severity"] for f in gate.evaluate(data, [], TODAY, DEV)[0]] == ["high"]


def test_same_advisory_on_an_unlisted_package_blocks():
    data = report(("forked-picomatch", "high", [advisory("forked-picomatch", "high")]))
    assert len(gate.evaluate(data, EXCEPTION, TODAY, DEV)[0]) == 1


def test_moderate_and_low_do_not_block_like_audit_level_high():
    data = report(("hono", "moderate", [advisory("hono", "moderate", OTHER)]),
                  ("x", "low", [advisory("x", "low", OTHER)]))
    assert gate.evaluate(data, [], TODAY, DEV) == ([], [], [])


def test_unused_exception_is_reported():
    assert gate.evaluate(report(), EXCEPTION, TODAY, DEV) == ([], [], ["GHSA-fc5q-39xc-vjpf"])


@pytest.mark.parametrize("data", [None, {}, {"error": {"code": "ENOLOCK"}}, {"vulnerabilities": []}])
def test_unreadable_report_fails_closed(data):
    with pytest.raises(ValueError):
        gate.evaluate(data, EXCEPTION, TODAY, DEV)


def fake_run(stdout, returncode=1):
    def run(command, **kwargs):
        assert command == ["npx", "-y", "npm@11", "audit", "--audit-level=high", "--json"]
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")
    return run


@pytest.fixture
def lockfile(tmp_path):
    path = tmp_path / "package-lock.json"
    path.write_text(json.dumps({"lockfileVersion": 3, "packages": DEV}))
    return path


def test_main_exit_codes(capsys, lockfile):
    def main(stdout, returncode=1):
        return gate.main(run=fake_run(stdout, returncode), today=TODAY, lockfile=lockfile)

    assert main(json.dumps(PICOMATCH_ONLY)) == 0
    assert "Excepted until 2026-12-01" in capsys.readouterr().out
    other = report(("lib", "critical", [advisory("lib", "critical", "https://github.com/advisories/GHSA-zzzz")]))
    assert main(json.dumps(other)) == 1
    assert "::error::GHSA-zzzz in lib (critical)" in capsys.readouterr().out
    assert main("npm ERR! network") == 1
    assert main(json.dumps(report()), returncode=0) == 0
    moderate = report(("hono", "moderate", [advisory("hono", "moderate", OTHER)]))
    assert main(json.dumps(moderate), returncode=0) == 0


@pytest.mark.parametrize("stdout,returncode", [
    (json.dumps(report()), -9),          # killed after writing a clean report
    (json.dumps(report()), 2),
    (json.dumps(report()), 1),           # npm saw high findings the report does not show
    (json.dumps(PICOMATCH_ONLY), 0),     # report and exit status disagree
])
def test_main_fails_closed_on_unaccounted_exit_status(stdout, returncode, lockfile, capsys):
    assert gate.main(run=fake_run(stdout, returncode), today=TODAY, lockfile=lockfile) == 1
    assert "could not be evaluated" in capsys.readouterr().out


def test_main_without_a_lockfile_fails(tmp_path):
    assert gate.main(run=fake_run(json.dumps(PICOMATCH_ONLY)), today=TODAY,
                     lockfile=tmp_path / "missing.json") == 1


def test_checked_in_exceptions_are_complete_and_dated():
    for entry in gate.load_exceptions():
        assert entry["id"].startswith("GHSA-") and entry["packages"]


def test_incomplete_exception_is_rejected(tmp_path):
    path = tmp_path / "exceptions.json"
    path.write_text(json.dumps([{"id": "GHSA-x", "packages": ["p"], "expires": "2026-12-01"}]))
    with pytest.raises(ValueError):
        gate.load_exceptions(path)


def test_javascript_pass_record_reads_the_gate_and_its_exceptions():
    sys.path.insert(0, str(ROOT / "scripts" / "ci"))
    import plan
    include = plan.AREAS["javascript-checks"]["include"]
    assert "scripts/npm_audit_gate.py" in include and "scripts/npm_audit_exceptions.json" in include
