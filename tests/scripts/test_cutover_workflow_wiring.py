"""Mutate real release workflow inputs/steps; the wiring verifier must reject them."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest
import yaml

from scripts.release import promote_image, release_wiring

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
    "step_order", "windows_policy_missing", "windows_policy_bypass", "windows_policy_ungated", "windows_skip_unchecked"])
def test_workflow_preflight_rejects_broken_release_delivery(tmp_path, mutation):
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
    elif mutation == "windows_policy_missing": del jobs["windows-policy"]
    elif mutation == "windows_policy_bypass": jobs["desktop"]["if"] = "needs.create-release.outputs.promote_latest == 'true'"
    elif mutation == "windows_policy_ungated": jobs["windows-policy"]["needs"] = "create-release"
    elif mutation == "windows_skip_unchecked": jobs["promote-image"]["if"] = jobs["promote-image"]["if"].replace("needs.windows-policy.outputs.build == 'false' && ", "")
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
    if mutation is None:
        assert release_wiring.verify(tmp_path)
    else:
        with pytest.raises(ValueError):
            release_wiring.verify(tmp_path)


def _source_step(workflow, job):
    data = yaml.load((ROOT / ".github/workflows" / workflow).read_text(), Loader=yaml.BaseLoader)
    return next(step for step in data["jobs"][job]["steps"] if step.get("id") == "source")["run"]


def test_no_workflow_hides_a_failing_command_in_an_output_echo():
    # echo "name=$(cmd)" >> "$GITHUB_OUTPUT" discards cmd's exit status and
    # records an empty value (an empty APPLICATION_SEQUENCE reached a build).
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        for line in path.read_text().splitlines():
            if "GITHUB_OUTPUT" in line and re.search(r'echo\s+"?[A-Za-z_]+=\$\(', line):
                assert "|| true" in line, f"{path.name}: {line.strip()}"


@pytest.mark.skipif(shutil.which("bash") is None or sys.platform == "win32", reason="GitHub's Linux bash step")
@pytest.mark.parametrize("workflow,job", [("container-postgres.yml", "container-postgres"),
                                          ("docker-publish.yml", "build")])
@pytest.mark.parametrize("sequence,ok", [("print(3081)", True), ('raise SystemExit("boom")', False),
                                         ("print('')", False), ("print(0)", False)])
def test_source_step_fails_loudly_on_a_bad_sequence(tmp_path, workflow, job, sequence, ok):
    repo = tmp_path / "repo"
    (repo / "scripts/release").mkdir(parents=True)
    (repo / "android/ci").mkdir(parents=True)
    (repo / "scripts/release/sequence_offset.py").write_text(sequence + "\n")
    (repo / "android/ci/resolve-version-name.py").write_text("print('1.1.0')\n")
    for args in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                  "commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    output = tmp_path / "output"
    env = {**os.environ, "GITHUB_OUTPUT": str(output), "GITHUB_WORKSPACE": str(repo), "RELEASE_TAG": ""}
    result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _source_step(workflow, job)],
                            cwd=repo, env=env, capture_output=True, text=True)
    assert (result.returncode == 0) is ok, result.stderr
    written = output.read_text() if output.exists() else ""
    assert ("sequence=3081\n" in written) if ok else "sequence=" not in written
