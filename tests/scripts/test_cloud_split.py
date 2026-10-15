"""App checks and publishers do not need the private Cloud service checkout."""
import json
from pathlib import Path
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("workflow,job,script", [
    ("desktop-source.yml", "publish", "publish-source.mjs"),
    ("release.yml", "source-stable", "publish-source.mjs"),
    ("desktop-installer.yml", "publish", "publish-installer.mjs"),
    ("platform-installer.yml", "publish", "publish-platform-installer.mjs"),
])
def test_publishers_install_their_own_pinned_cli(workflow, job, script):
    config = yaml.load((ROOT / ".github/workflows" / workflow).read_text(), Loader=yaml.BaseLoader)
    steps = config["jobs"][job]["steps"]
    cli_dir = ".publication-control/scripts/source-publisher" if workflow == "desktop-source.yml" else "scripts/source-publisher"
    assert any(s.get("working-directory") == cli_dir and "npm ci" in s.get("run", "") for s in steps)
    source = (ROOT / "desktop/scripts" / script).read_text()
    assert "scripts/source-publisher/node_modules/wrangler/bin/wrangler.js" in source
    assert "android/hub" not in source
    manifest = json.loads((ROOT / "scripts/source-publisher/package.json").read_text())
    lock = json.loads((ROOT / "scripts/source-publisher/package-lock.json").read_text())
    assert lock["packages"]["node_modules/wrangler"]["version"] == manifest["devDependencies"]["wrangler"]


@pytest.mark.parametrize("path", sorted((ROOT / ".github/workflows").glob("*.yml")), ids=lambda p: p.name)
def test_workflow_job_dependencies_exist(path):
    config = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    jobs = config["jobs"]
    for name, job in jobs.items():
        needs = job.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        assert set(needs) <= jobs.keys(), f"{path.name}: {name} depends on missing jobs: {set(needs) - jobs.keys()}"


def test_android_publication_only_needs_the_retained_app_jobs():
    config = yaml.load((ROOT / ".github/workflows/android.yml").read_text(), Loader=yaml.BaseLoader)
    assert set(config["jobs"]["publish"]["needs"]) == {"build", "android-ui"}
    assert "account-hub" not in config["jobs"]


def test_no_cloud_service_checkout_or_workflow_dependencies():
    for name in ("hub", "license", "site", "uptime"):
        assert not (ROOT / "android" / name).exists()
        assert not (ROOT / ".github/workflows" / f"deploy-{name}.yml").exists()
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        for service in ("hub", "license", "site", "uptime"):
            assert f"android/{service}" not in path.read_text(), path
    assert (ROOT / "app/network/hub.py").is_file()
    assert (ROOT / "app/core/license.py").is_file()
    assert (ROOT / "docs/content/api-reference").is_dir()
