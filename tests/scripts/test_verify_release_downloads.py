"""Release orchestration contracts and public download read-back failures."""
import io
import json
from pathlib import Path

import pytest
import yaml

from scripts.ci import verify_release_downloads as check

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
    monkeypatch.setattr("sys.argv", ["check", "--tag", "v1.2.3", "--revision", REVISION])
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
    assert "if" not in jobs["desktop"]
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
    publisher = workflow("platform-installer.yml")["jobs"]
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
    assert set(job["needs"]) == {"preflight", "create-release", "desktop", "platform-installers", "promotion", "promotion-platform-installers"}
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
