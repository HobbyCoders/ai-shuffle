"""Release orchestration contracts and public download read-back failures."""
import io
import json
from pathlib import Path

import pytest
import yaml

import scripts.ci.verify_release_downloads as check

ROOT = Path(__file__).resolve().parents[2]
REVISION = "a" * 40


def workflow(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


def downloads():
    return {"platforms": {name: {"version": "1.2.3", "revision": REVISION} for name in check.WORKFLOWS}}


def test_all_current_downloads():
    check.verify(downloads(), "v1.2.3", REVISION)


@pytest.mark.parametrize("platform", ["win", "linux", "android"])
@pytest.mark.parametrize("fault", ["absent", "null", "old-version", "wrong-revision"])
def test_each_missing_or_stale_download_names_its_recovery(platform, fault):
    payload = downloads()
    if fault == "absent":
        del payload["platforms"][platform]
    elif fault == "null":
        payload["platforms"][platform] = None
    else:
        payload["platforms"][platform]["version" if fault == "old-version" else "revision"] = "old"
    with pytest.raises(ValueError) as error:
        check.verify(payload, "v1.2.3", REVISION)
    assert str(error.value).startswith(platform + ":")
    assert check.WORKFLOWS[platform] in str(error.value)
    assert "v1.2.3" in str(error.value)
    if platform != "win":
        assert "release_assets=true (no build run ID)" in str(error.value)
    assert len(str(error.value).splitlines()) == 1


@pytest.mark.parametrize("payload", [{}, {"platforms": None}, [], {"platforms": {"win": None}}])
def test_malformed_or_empty_list_reports_every_missing_platform(payload):
    with pytest.raises(ValueError) as error:
        check.verify(payload, "v1.2.3", REVISION)
    assert len(str(error.value).splitlines()) == 3
    for platform in check.WORKFLOWS:
        assert platform + ":" in str(error.value)


@pytest.mark.parametrize("failure", [None, "stale", "http", "invalid-json"])
def test_cli_readback_exits_nonzero_on_stale_or_unavailable_hub(monkeypatch, capsys, failure):
    monkeypatch.setattr("sys.argv", ["check", "--tag", "v1.2.3", "--revision", REVISION, "--activation-result", "success"])
    def fetch(request, timeout):
        assert request.full_url == check.URL
        assert timeout == 30
        if failure == "http":
            raise OSError("private response body")
        if failure == "invalid-json":
            return io.StringIO("private response body")
        return io.StringIO(json.dumps({} if failure == "stale" else downloads()))
    monkeypatch.setattr(check, "urlopen", fetch)
    assert check.main() == (0 if failure is None else 1)
    output = capsys.readouterr()
    assert "private response body" not in output.err
    if failure:
        assert "::error::" in output.err
        for platform in check.WORKFLOWS:
            assert platform in output.err


def test_stable_and_staged_windows_cannot_be_skipped():
    wf = workflow("release.yml")
    jobs = wf["jobs"]
    assert "build_desktop" not in wf["on"]["workflow_dispatch"]["inputs"]
    assert jobs["desktop"]["if"] == "needs.create-release.outputs.promote_latest == 'true'"
    assert jobs["desktop"]["needs"] == "create-release"
    assert jobs["stage-desktop"]["if"] == "inputs.stage_only"
    for job in ("desktop", "stage-desktop"):
        assert jobs[job]["uses"] == "./.github/workflows/desktop-installer.yml"
    assert "needs.stage-desktop.result == 'success'" in jobs["stage-record"]["if"]
    assert "skipped" not in jobs["stage-record"]["if"]
    stage = next(s for s in jobs["stage-record"]["steps"] if s.get("name", "").startswith("Retain immutable"))
    assert "--windows" in stage["run"]


def test_both_publication_paths_register_platforms_with_the_existing_publisher():
    jobs = workflow("release.yml")["jobs"]
    for name, dependency in (("platform-installers", "promote-image"), ("promotion-platform-installers", "promotion")):
        job = jobs[name]
        assert job["needs"] == dependency
        assert job["strategy"] == {"fail-fast": "false", "matrix": {"platform": ["linux", "android"]}}
        assert job["uses"] == "./.github/workflows/platform-installer.yml"
        assert job["secrets"] == "inherit"
    assert jobs["platform-installers"]["with"]["build_run_id"] == "${{ format('{0}', github.run_id) }}"
    assert jobs["promotion-platform-installers"]["with"]["release_assets"] == "true"
    assert jobs["promotion-platform-installers"]["with"]["build_run_id"] == ""
    wf = workflow("platform-installer.yml")
    for trigger in ("workflow_dispatch", "workflow_call"):
        assert wf["on"][trigger]["inputs"]["release_assets"]["type"] == "boolean"
        assert wf["on"][trigger]["inputs"]["release_assets"]["default"] == "false"
    publisher = wf["jobs"]
    assert "!inputs.release_assets" in publisher["android-build"]["if"]
    steps = publisher["publish"]["steps"]
    download = next(s for s in steps if s.get("name") == "Download the sealed release assets without rebuilding")
    assert download["if"] == "inputs.release_assets"
    for asset in ("*.AppImage", "*.deb", "linux-build-provenance.json", "SHA256SUMS-linux", "AI-Shuffle-Android.apk", "AI-Shuffle-Android.apk.sha256", "android-build.json"):
        assert asset in download["run"]
    assert 'cp artifact/SHA256SUMS-linux artifact/SHA256SUMS' in download["run"]
    for step in steps:
        if step.get("uses", "").startswith("actions/download-artifact@") or step.get("name") == "Upload packages to the release":
            assert step["if"] == "${{ !inputs.release_assets }}"
    sign = next(s for s in steps if s.get("name") == "Sign and point the hub at the installer")
    assert "if" not in sign
    assert sign["run"] == 'node desktop/scripts/publish-platform-installer.mjs "$PLATFORM" artifact "$RELEASE_TAG"'


@pytest.mark.parametrize("stage,promote,latest,preflight,cancelled,expected", [
    (False, "", "true", "success", False, True),
    (False, "v1.2.3", "", "success", False, True),
    (True, "", "", "success", False, False),
    (False, "", "false", "success", False, False),
    (False, "v1.2.3", "", "failure", False, False),
    (False, "v1.2.3", "", "success", True, False),
])
def test_final_readback_runs_for_both_paths_even_after_publication_failure(stage, promote, latest, preflight, cancelled, expected):
    job = workflow("release.yml")["jobs"]["verify-downloads"]
    assert set(job["needs"]) == {"preflight", "create-release", "desktop", "promote-image", "platform-installers", "promotion", "promotion-platform-installers"}
    expression = job["if"]
    for name, value in {"inputs.stage_only": stage, "inputs.promote": promote,
                        "needs.create-release.outputs.promote_latest": latest,
                        "needs.preflight.result": preflight}.items():
        expression = expression.replace(name, repr(value))
    expression = expression.replace("always()", "True").replace("cancelled()", repr(cancelled))
    expression = expression.replace("&&", "and").replace("||", "or").replace("!inputs", "not inputs")
    import re
    expression = re.sub(r"!(?!=)", " not ", expression)
    assert bool(eval(expression, {"__builtins__": {}}, {})) is expected
    assert "verify_release_downloads.py" in job["steps"][-1]["run"]

@pytest.mark.parametrize("tag,newest,expected", [
    ("v1.1.0", "v1.1.0", True),
    ("v1.0.3", "v1.1.0", False),
    ("v1.2.0-rc.1", "v1.1.0", False),
])
def test_windows_only_publishes_for_the_newest_final_release(tag, newest, expected):
    # Exercise the producer's inline version decision and the consumer predicate.
    import os
    import subprocess
    import shutil
    from fluid.tools.shell import resolve_shell
    jobs = workflow("release.yml")["jobs"]
    step = next(s for s in jobs["create-release"]["steps"] if s.get("id") == "promote")
    # This shell function replaces only git tag enumeration, not the decision.
    script = 'git() { printf "%s\n" "$NEWEST"; }; export -f git\n' + step["run"]
    script = script.replace('>> "$GITHUB_OUTPUT"', '')
    env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP") if key in os.environ}
    shell = resolve_shell().path if os.name == "nt" else shutil.which("bash")
    assert shell is not None
    result = subprocess.run([shell, "-c", script], env={**env, "VERSION": tag, "NEWEST": newest},
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    latest = next(line.split("=", 1)[1] for line in result.stdout.splitlines() if line.startswith("promote_latest="))
    expression = jobs["desktop"]["if"].replace("needs.create-release.outputs.promote_latest", repr(latest))
    assert eval(expression, {"__builtins__": {}}, {}) is expected


@pytest.mark.parametrize("result", ["failure", "skipped", "cancelled"])
@pytest.mark.parametrize("payload", [downloads(), {}])
def test_failed_release_gates_never_recommend_manual_registration(monkeypatch, capsys, result, payload):
    monkeypatch.setattr("sys.argv", ["check", "--tag", "v1.2.3", "--revision", REVISION, "--activation-result", result])
    monkeypatch.setattr(check, "urlopen", lambda *args, **kwargs: io.StringIO(json.dumps(payload)))
    assert check.main() == 1
    message = capsys.readouterr().err
    assert "Release gates failed or were skipped" in message
    assert "do not manually register installers" in message
    assert "platform-installer.yml" not in message
    assert "desktop-installer.yml" not in message
    if not payload:
        for platform in check.WORKFLOWS:
            assert platform + ": missing or stale" in message


def test_final_readback_receives_the_correct_activation_result():
    step = workflow("release.yml")["jobs"]["verify-downloads"]["steps"][-1]
    assert step["env"]["ACTIVATION_RESULT"] == "${{ inputs.promote != '' && needs.promotion.result || needs.promote-image.result }}"
    assert '--activation-result "$ACTIVATION_RESULT"' in step["run"]
