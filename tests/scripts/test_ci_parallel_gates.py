"""Fail-closed gates and lossless JavaScript/native/browser partitions."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def source_inventory():
    inventory = {}
    for path in (ROOT / "desktop/test").glob("source-updates*.test.mjs"):
        chunks = re.split(r"(?=^test\()", path.read_text(), flags=re.M)[1:]
        for chunk in chunks:
            name = re.match(r"test\((.*?),\s*(?:async|\()", chunk)[1]
            assert name not in inventory, name
            inventory[name] = hashlib.sha256(chunk.strip().encode()).hexdigest()
    return inventory


def test_source_update_cases_and_assertions_are_preserved():
    expected = json.loads((Path(__file__).with_name("source-updates-inventory.json")).read_text())
    assert source_inventory() == expected
    helper = (ROOT / "desktop/test/helpers/source-updates-fixture.mjs").read_text()
    assert "fs.mkdtemp(" in helper and "t.after(()=>fs.rm(" in helper


def workflow(name):
    import yaml
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


def test_gates_fail_closed_for_failed_cancelled_skipped_and_missing_branches():
    for filename, job in [("ci-frontend.yml", "javascript"),
                          ("ci-browser-workspace.yml", "browser-workspace"),
                          ("windows-suite.yml", "desktop-gate")]:
        gate = workflow(filename)["jobs"][job]
        assert "always()" in gate["if"]
        step = gate["steps"][0]
        command = step["run"].split("<<'JS'\n", 1)[1].rsplit("\nJS", 1)[0]
        expected = gate["needs"]
        success = {name: {"result": "success"} for name in expected}
        def run(results):
            return subprocess.run(["node", "--input-type=module", "-e", command],
                                  env={**os.environ, "RESULTS": json.dumps(results)},
                                  capture_output=True).returncode
        assert run(success) == 0
        for name in expected:
            for result in ["failure", "cancelled", "skipped", None]:
                broken = dict(success)
                if result is None:
                    del broken[name]
                else:
                    broken[name] = {"result": result}
                assert run(broken) != 0, (filename, name, result)


def test_all_parallel_branches_remain_unconditional_and_required():
    js = workflow("ci-frontend.yml")["jobs"]
    assert set(js["javascript"]["needs"]) == set(js) - {"javascript"}
    assert js["frontend"]["strategy"]["matrix"]["shard"] == ["1", "2", "3", "4"]
    browser = workflow("ci-browser-workspace.yml")["jobs"]
    assert browser["workspace"]["strategy"]["matrix"]["chrome-mode"] == ["external", "panel-demand", "direct-page-demand"]
    windows = workflow("windows-suite.yml")["jobs"]
    assert windows["desktop"]["strategy"]["matrix"]["shard"] == ["1", "2", "3"]
    for filename in ["ci-frontend.yml", "ci-browser-workspace.yml", "windows-suite.yml"]:
        config = workflow(filename)
        assert not config["on"]["pull_request"]
        assert config["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
        for key, job in config["jobs"].items():
            assert job["runs-on"] in ["ubuntu-latest", "windows-latest"]
            assert "continue-on-error" not in job
            for step in job["steps"]:
                assert "continue-on-error" not in step
                assert "--no-sandbox" not in step.get("run", "")
            if key in ["frontend", "workspace", "desktop"]:
                assert job["strategy"]["fail-fast"] == "false"


def desktop_evidence(directory):
    paths = [directory / f"windows-desktop-{i}" for i in range(1, 4)]
    expected = Counter(path.relative_to(ROOT / "desktop").as_posix()
                       for path in (ROOT / "desktop/test").rglob("*.test.mjs"))
    assert expected, "empty collection"
    union = Counter()
    totals = []
    for path in paths:
        actual = Counter(json.loads((path / "inventory.json").read_text()))
        assert actual, "missing or empty shard"
        text = (path / "desktop.tap").read_text(encoding="utf-8")
        assert re.search(r"^# fail 0$", text, re.M), "failing shard"
        totals.append(int(re.search(r"^# tests (\d+)$", text, re.M)[1]))
        union.update(actual)
    assert union == expected, f"missing: {expected - union}; extra: {union - expected}"
    test_source_update_cases_and_assertions_are_preserved()
    message = (f"Desktop shard union: {len(expected)} real executed files, exactly once; "
               f"{totals} = {sum(totals)} runtime tests (including nested cases). "
               f"All {len(source_inventory())} moved source-update test bodies match pre-split SHA-256 inventory.\n")
    print(message)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(message)


def test_workspace_cases_keep_real_browser_and_ui_security_checks():
    job = workflow("ci-browser-workspace.yml")["jobs"]["workspace"]
    assert job["env"]["RUN_BROWSER_WORKSPACE_INTEGRATION"] == "1"
    assert job["env"]["RUN_BROWSER_WORKSPACE_UI"] == "1"
    steps = {step.get("name"): step for step in job["steps"]}
    install = steps["Install isolated Chromium"]
    assert "if" not in install
    assert 'sudo env "PATH=$PATH" timeout --kill-after=10s 90s npx --no-install playwright-core install-deps chromium' in install["run"]
    assert "timeout --kill-after=10s 90s npx --no-install playwright-core install chromium" in install["run"]
    assert "||" not in install["run"]
    assert "kernel.apparmor_restrict_unprivileged_userns=0" in install["run"]
    assert "https://archive.ubuntu.com/ubuntu" in install["run"]
    assert "--allow-unauthenticated" not in install["run"]
    assert "trusted=yes" not in install["run"]
    assert steps["Cache managed Chromium"]["uses"] == "actions/cache@v4"
    assert "nested shadow auth fields" in steps["Verify shadow-root auth editor discovery and edits"]["run"]
    assert "verify-crumb-strip.mjs" in steps["Verify desktop breadcrumb strip layout"]["run"]
    assert "[$CHROME_MODE]" in steps["Verify shared browser and rendered desktop/mobile UI"]["run"]


def test_chromium_install_phases_fail_closed_without_retrying_tests(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    for program in ["sudo", "timeout", "node"]:
        shim = bin_dir / program
        shim.write_text(
            '#!/bin/sh\n'
            'printf "%s:%s\\n" "${0##*/}" "$*" >> "$CALLS"\n'
            'case "$*" in *install-deps*) exit "$OS_RESULT";; esac\n'
            'if [ "${0##*/}" = timeout ]; then exit "$BROWSER_RESULT"; fi\n')
        shim.chmod(0o755)
    for filename, job, step_name in [
        ("ci-frontend.yml", "browser-mcp", "Prepare sandboxed Chromium"),
        ("ci-browser-workspace.yml", "workspace", "Install isolated Chromium"),
    ]:
        command = next(step["run"] for step in workflow(filename)["jobs"][job]["steps"]
                       if step.get("name") == step_name)
        for os_result, browser_result in [(0, 0), (9, 0), (0, 7)]:
            calls.write_text("")
            bash = str(Path(os.environ["ProgramFiles"]) / "Git/bin/bash.exe") if os.name == "nt" else "bash"
            result = subprocess.run([bash, "-e", "-c", command],
                                    env={**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                                         "CALLS": calls.as_posix(), "OS_RESULT": str(os_result),
                                         "BROWSER_RESULT": str(browser_result)}, capture_output=True)
            assert result.returncode == (os_result or browser_result)
            recorded = calls.read_text()
            assert recorded.count("install-deps chromium") == 1
            assert recorded.count("install chromium") == (0 if os_result else 1)


def test_desktop_evidence_rejects_missing_duplicated_or_failed_shards(tmp_path):
    import pytest
    files = sorted(path.relative_to(ROOT / "desktop").as_posix()
                   for path in (ROOT / "desktop/test").rglob("*.test.mjs"))
    for index in range(1, 4):
        directory = tmp_path / f"windows-desktop-{index}"
        directory.mkdir()
        (directory / "inventory.json").write_text(json.dumps(files[index - 1::3]))
        (directory / "desktop.tap").write_text("# tests 1\n# fail 0\n")
    desktop_evidence(tmp_path)
    path = tmp_path / "windows-desktop-1/inventory.json"
    original = path.read_text()
    for wrong in [[], files, json.loads(original) + [files[0]]]:
        path.write_text(json.dumps(wrong))
        with pytest.raises(AssertionError):
            desktop_evidence(tmp_path)
    path.write_text(original)
    (tmp_path / "windows-desktop-1/desktop.tap").write_text("# tests 1\n# fail 1\n")
    with pytest.raises(AssertionError, match="failing shard"):
        desktop_evidence(tmp_path)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        desktop_evidence(tmp_path)


if __name__ == "__main__":
    assert sys.argv[1] == "--desktop-evidence"
    desktop_evidence(Path(sys.argv[2]))
