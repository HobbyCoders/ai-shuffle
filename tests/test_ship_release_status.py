"""Pure-function checks for scripts/ci/ship_release_status.py (no git/gh calls)."""
import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "ship_release_status.py"
_spec = importlib.util.spec_from_file_location("ship_release_status", _PATH)
srs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(srs)


def test_latest_final_tag_ignores_prereleases_and_sorts_numerically():
    tags = ["v0.8.9", "v0.8.22", "v0.8.23-rc.1", "v6-phase2", "v0.8.3"]
    assert srs.latest_final_tag(tags) == "v0.8.22"
    assert srs.latest_final_tag([]) is None


def test_next_patch():
    assert srs.next_patch("v0.8.22") == "v0.8.23"
    assert srs.next_patch(None) == "v0.1.0"


def test_frontend_only_changes_need_no_play_build():
    builds = srs.platforms_needing_builds(["frontend/src/a.svelte", "app/core/x.py"])
    assert builds["android"]["needed"] is False
    assert builds["desktop_installer"]["needed"] is False


def test_android_changes_need_play_build():
    builds = srs.platforms_needing_builds(["android/app/build.gradle.kts"])
    assert builds["android"] == {"needed": True, "example_paths": ["android/app/build.gradle.kts"]}


def test_ci_state_uses_newest_run_per_workflow_on_sha():
    runs = [
        {"workflowName": "CI Backend", "status": "completed", "conclusion": "success", "headSha": "abc"},
        {"workflowName": "CI Backend", "status": "completed", "conclusion": "failure", "headSha": "abc"},
        {"workflowName": "CI fluid", "status": "in_progress", "conclusion": None, "headSha": "abc"},
        {"workflowName": "CI JavaScript", "status": "completed", "conclusion": "success", "headSha": "old"},
    ]
    state = srs.ci_state(runs, "abc")
    assert state["CI Backend"] == "success"
    assert state["CI fluid"] == "pending"
    assert state["CI JavaScript"] == "missing"
