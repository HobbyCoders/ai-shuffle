"""Mutate real release workflow inputs/steps; the cutover verifier must reject them."""
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts.release import cutover_check, promote_image

ROOT = Path(__file__).resolve().parents[2]


def test_release_notes_and_publisher_use_the_documented_public_image():
    release = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
    docker = yaml.load((ROOT / ".github/workflows/docker-publish.yml").read_text(), Loader=yaml.BaseLoader)
    compose = yaml.safe_load((ROOT / "docs/install/compose/compose.yaml").read_text())
    image = compose["services"]["ai-shuffle"]["image"].split(":", 1)[0]
    assert promote_image.IMAGE == image
    assert docker["env"]["REGISTRY"] + "/" + docker["env"]["IMAGE_NAME"].replace(
        "${{ github.repository_owner }}", "hobbycoders"
    ) == image
    notes = next(step["with"]["body"] for job in release["jobs"].values()
                 for step in job.get("steps", []) if step.get("name") == "Create GitHub Release")
    assert f"docker pull {image}:" in notes
    assert f"package/{image.rsplit('/', 1)[1]}" in notes
    assert "The image is public" in notes
    for obsolete in ("(private)", "The image is private", "docker login", "licensed registry", "token that can read"):
        assert obsolete not in notes


@pytest.mark.parametrize("mutation", [None, "digest_missing", "digest_replaced", "docker_tag", "docker_caller",
    "apk_disabled", "apk_tag", "windows_tag", "linux_version", "linux_tag", "linux_attach", "linux_checkout",
    "callee_input", "callee_output", "job_output", "reuse_if", "reuse_digest", "reuse_source", "reuse_command",
    "build_if", "build_push", "build_load", "push_if", "push_command", "verify_if", "output_digest", "source_tag",
    "step_order"])
def test_workflow_preflight_rejects_broken_release_delivery(tmp_path, monkeypatch, mutation):
    release = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
    docker = yaml.load((ROOT / ".github/workflows/docker-publish.yml").read_text(), Loader=yaml.BaseLoader)
    jobs = release["jobs"]
    steps = docker["jobs"]["build"]["steps"]
    named = {s.get("name"): s for s in steps}
    if mutation == "digest_missing": del jobs["docker"]["with"]["image_digest"]
    elif mutation == "digest_replaced": jobs["docker"]["with"]["image_digest"] = ""
    elif mutation == "docker_tag": jobs["docker"]["with"]["release_tag"] = "main"
    elif mutation == "docker_caller": jobs["docker"]["uses"] = "./.github/workflows/build-branch.yml"
    elif mutation == "apk_disabled": jobs["android-apk"]["with"]["publish_download"] = "false"
    elif mutation == "apk_tag": jobs["android-apk"]["with"]["release_tag"] = "main"
    elif mutation == "windows_tag": jobs["desktop"]["with"]["release_tag"] = "main"
    elif mutation == "linux_version": jobs["linux-build"]["with"]["version"] = "1.0.0"
    elif mutation == "linux_tag": jobs["linux-release"]["steps"][-1]["env"]["RELEASE_TAG"] = "main"
    elif mutation == "linux_attach": jobs["linux-release"]["steps"][-1]["run"] = "echo attach_linux.py"
    elif mutation == "linux_checkout": jobs["linux-release"]["steps"][0]["with"]["ref"] = "main"
    elif mutation == "callee_input": del docker["on"]["workflow_call"]["inputs"]["image_digest"]
    elif mutation == "callee_output": docker["on"]["workflow_call"]["outputs"]["digest"]["value"] = "other"
    elif mutation == "job_output": docker["jobs"]["build"]["outputs"]["digest"] = "other"
    elif mutation == "reuse_if": named["Verify and load an existing candidate digest"]["if"] = "false"
    elif mutation == "reuse_digest": named["Verify and load an existing candidate digest"]["env"]["IMAGE_DIGEST"] = "other"
    elif mutation == "reuse_source": named["Verify and load an existing candidate digest"]["env"]["SOURCE"] = "."
    elif mutation == "reuse_command": named["Verify and load an existing candidate digest"]["run"] = "echo unchecked"
    elif mutation == "build_if": named["Build candidate image"]["if"] = "always()"
    elif mutation == "build_push": named["Build candidate image"]["with"]["push"] = "true"
    elif mutation == "build_load": named["Build candidate image"]["with"]["load"] = "false"
    elif mutation == "push_if": named["Push the candidate without rebuilding"]["if"] = "always()"
    elif mutation == "push_command": named["Push the candidate without rebuilding"]["run"] = "docker push latest"
    elif mutation == "verify_if": named["Verify actual container lifecycle and UI"]["if"] = "false"
    elif mutation == "output_digest": named["Record immutable image digest"]["env"]["REUSED_DIGEST"] = ""
    elif mutation == "source_tag": named["Checkout release source"]["with"]["ref"] = "main"
    elif mutation == "step_order":
        steps.remove(named["Push the candidate without rebuilding"])
        steps.insert(0, named["Push the candidate without rebuilding"])
    destination = tmp_path / ".github/workflows"
    destination.mkdir(parents=True)
    (destination / "release.yml").write_text(yaml.safe_dump(release))
    (destination / "docker-publish.yml").write_text(yaml.safe_dump(docker))
    monkeypatch.setattr(cutover_check.cutover, "git", lambda *_: "")
    monkeypatch.setattr(cutover_check.cutover, "api", lambda *_: {})
    monkeypatch.setattr(cutover_check.cutover, "container_sequence", lambda *_: 1)
    monkeypatch.setattr(cutover_check, "attest", lambda *_: {})
    monkeypatch.setattr(cutover_check, "verify_catalog", lambda *_, **kw: None)
    monkeypatch.setattr(cutover_check, "verify_merged_pr", lambda *_: None)
    args = SimpleNamespace(old_repo=cutover_check.cutover.OLD_REPO, new_repo=cutover_check.cutover.NEW_REPO,
                           private_export="a"*40, public_root="b"*40, private_checkout=tmp_path)
    checks = cutover_check.checks(tmp_path, args)
    result = next(r for r in checks if r["check"] == "guarded image reuse and promotion")
    assert result["passed"] is (mutation is None), result
