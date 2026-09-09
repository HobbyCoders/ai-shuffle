"""Publishing the bundled mobile client requires explicit worker rollout approval."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_android_publish_requires_explicit_opt_in():
    workflow = yaml.safe_load((ROOT / ".github/workflows/android.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    for event in ("workflow_dispatch", "workflow_call"):
        inputs = triggers[event].get("inputs", {}) if triggers[event] else {}
        assert inputs.get("publish_download", {}).get("default") is False
    assert "inputs.publish_download" in workflow["jobs"]["publish"]["if"]


def test_versioned_android_release_respects_worker_rollout_gate():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    assert workflow["jobs"]["android"]["with"]["publish_download"] == "${{ vars.ANDROID_WORKERS_READY == 'true' }}"
