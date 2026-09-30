"""Release notes expose every distribution and safe, version-pinned Docker setup."""
from pathlib import Path
import os
import re
import shlex
import shutil
import subprocess

from fluid.tools.shell import resolve_shell

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def release_body():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    steps = workflow["jobs"]["create-release"]["steps"]
    return next(step["with"]["body"] for step in steps if "body" in step.get("with", {}))


def test_release_downloads_include_desktop_mobile_and_docker():
    body = release_body()
    assert "| **Desktop** |" in body
    assert "| **Mobile** |" in body
    assert "| **Docker** |" in body
    assert "https://hobbycoders.com/desktop" in body
    assert "run the installer" in body
    assert "newest stable release" in body and "latest code from main" not in body
    assert "Start AI Shuffle.cmd" not in body
    assert "https://play.google.com/store/apps/details?id=com.hobbycoders.aishuffle" in body
    assert "releases/download/" not in body
    assert "Signed Android APK" not in body
    assert "Desktop installers are opt-in" in body
    assert "https://github.com/orgs/HobbyCoders/packages/container/package/ai-shuffle-server" in body
    assert "docs/install" in body


def test_docker_commands_pin_release_and_preserve_runtime_data():
    body = release_body().replace("${{ env.RELEASE_VERSION }}", "0.8.12")
    script = re.search(r"```bash\n(.*?)```", body, re.DOTALL).group(1)
    commands = script.replace("\\\n", "").strip().splitlines()
    image = "ghcr.io/hobbycoders/ai-shuffle-server:0.8.12"
    assert shlex.split(commands[0]) == ["docker", "pull", image]
    run = shlex.split(commands[1])
    assert run[:3] == ["docker", "run", "-d"]
    assert run[-1] == image
    assert run[run.index("--stop-timeout") + 1] == "90"
    assert run[run.index("--shm-size") + 1] == "1g"
    assert run[run.index("-p") + 1] == "127.0.0.1:8000:8000"
    mounts = [run[i + 1].split(":", 1)[1] for i, arg in enumerate(run) if arg == "-v"]
    assert set(mounts) == {
        "/data", "/workspace", "/home/appuser/.fluid", "/home/appuser/.claude",
        "/home/appuser/.local", "/home/appuser/.config/gh", "/home/appuser/chrome-profile",
    }
    assert ":latest" not in body
    assert "back up first" in body
    assert "external database must stay on their previous" in body


def test_release_version_outputs_feed_play_and_docker(tmp_path):
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    steps = workflow["jobs"]["preflight"]["steps"]
    script = next(step["run"] for step in steps if step.get("id") == "version")
    # Windows' PATH may resolve "bash" to the WSL launcher. Use the same
    # native Git Bash discovery as our tools, while Linux runs actual Bash.
    if os.name == "nt":
        shell = resolve_shell()
        assert shell.name == "git-bash", "Install Git Bash to exercise the release script"
        bash = shell.path
    else:
        bash = shutil.which("bash")
        assert bash is not None, "Install Bash to exercise the release script"
    # Preserve OS runtime inputs, not credentials, BASH_ENV or release outputs
    # inherited from the worker. Git Bash needs SystemRoot on Windows.
    runtime_env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP")
                   if key in os.environ}
    output_dir = tmp_path / "release outputs with spaces"
    output_dir.mkdir()
    env_file, output = output_dir / "env", output_dir / "output"
    for raw in ("v1.2.3", "V1.2.3", " 1.2.3 ", "v1.2.3-rc.1"):
        env_file.write_text("")
        output.write_text("")
        result = subprocess.run([bash, "-e", "-c", script], capture_output=True, text=True,
                                cwd=tmp_path, timeout=15,
                                env={**runtime_env, "VERSION": raw,
                                     "GITHUB_ENV": env_file.as_posix(),
                                     "GITHUB_OUTPUT": output.as_posix()})
        assert result.returncode == 0, result.stdout + result.stderr
        version = raw.strip().lstrip("vV")
        assert output.read_text() == f"tag=v{version}\nversion={version}\n"
        assert f"RELEASE_VERSION={version}\n" in env_file.read_text()
    for raw in ("main", "v1.2", "v1.2.3;exit 0", "../v1.2.3", "v1.2.3.4", "v01.2.3", "v1.2.3-"):
        env_file.write_text("")
        output.write_text("")
        result = subprocess.run([bash, "-e", "-c", script], capture_output=True, text=True,
                                cwd=tmp_path, timeout=15,
                                env={**runtime_env, "VERSION": raw,
                                     "GITHUB_ENV": env_file.as_posix(),
                                     "GITHUB_OUTPUT": output.as_posix()})
        assert result.returncode == 1, result.stdout + result.stderr
        assert "::error::Invalid release version" in result.stdout
        assert env_file.read_text() == ""
        assert output.read_text() == ""


def test_release_orchestration_defaults_to_docker_and_play_only():
    workflow = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
    desktop = workflow["jobs"]["desktop"]
    assert workflow["on"]["workflow_dispatch"]["inputs"]["build_desktop"] == {
        "description": "Also build a Windows installer (desktop normally updates in app)",
        "required": "false", "type": "boolean", "default": "false",
    }
    assert desktop["if"] == "github.event_name == 'workflow_dispatch' && inputs.build_desktop"
    assert desktop["uses"] == "./.github/workflows/desktop-installer.yml"
    assert workflow["jobs"]["docker"]["uses"] == "./.github/workflows/docker-publish.yml"
    assert workflow["permissions"]["packages"] == "write"
    for name in ("desktop", "docker", "android"):
        assert workflow["jobs"][name]["with"]["release_tag"] == "${{ needs.create-release.outputs.release_tag }}"
    assert "./.github/workflows/android.yml" not in str(workflow)


def test_docker_is_called_once_per_release_and_remains_manually_available():
    workflow = yaml.load((ROOT / ".github/workflows/docker-publish.yml").read_text(), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch", "workflow_call"}
    assert workflow["on"]["workflow_call"]["inputs"]["release_tag"]["required"] == "true"
    assert workflow["on"]["workflow_dispatch"]["inputs"]["no_cache"]["default"] == "false"
    assert workflow["on"]["workflow_dispatch"]["inputs"]["promote_latest"]["default"] == "false"


def meta_tags():
    workflow = yaml.safe_load((ROOT / ".github/workflows/docker-publish.yml").read_text())
    steps = {step.get("id"): step for step in workflow["jobs"]["build"]["steps"]}
    return [line.strip() for line in steps["meta"]["with"]["tags"].splitlines()
            if line.strip() and not line.strip().startswith("#")]


def test_only_a_promoted_release_moves_latest_and_main_builds_are_edge():
    tags = meta_tags()
    latest = next(tag for tag in tags if tag.startswith("type=raw,value=latest"))
    assert "inputs.release_tag != ''" in latest and "inputs.promote_latest" in latest
    assert "!contains(inputs.release_tag, '-')" in latest
    assert "is_default_branch" not in "".join(tags)
    edge = next(tag for tag in tags if tag.startswith("type=raw,value=edge"))
    assert "inputs.release_tag == ''" in edge and "refs/heads/main" in edge
    for pattern in ("{{major}}.{{minor}}", "{{major}},"):
        tag = next(tag for tag in tags if pattern in tag)
        assert "inputs.promote_latest" in tag
    release = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    assert release["jobs"]["docker"]["with"]["promote_latest"] == (
        "${{ needs.create-release.outputs.promote_latest == 'true' }}")


def test_images_go_to_the_private_package_only():
    for name in ("docker-publish.yml", "build-branch.yml"):
        workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
        assert workflow["env"]["IMAGE_NAME"] == "${{ github.repository_owner }}/ai-shuffle-server"
    assert "ghcr.io/hobbycoders/ai-shuffle:" not in release_body()


def test_promote_step_moves_latest_only_for_the_newest_final_release(tmp_path):
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    steps = workflow["jobs"]["create-release"]["steps"]
    script = next(step["run"] for step in steps if step.get("id") == "promote")
    bash = shutil.which("bash")
    if os.name == "nt" or bash is None:
        pytest.skip("needs POSIX bash and git")
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1"}

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)
    git("init", "-q")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "x")
    for tag in ("v0.8.18", "v0.9.0", "v0.9.1-rc.1", "v6-phase1", "v6-phase2"):
        git("tag", tag)
    output = tmp_path / "output"
    for version, expected in (("v0.9.1", "true"), ("v0.9.0", "true"), ("v0.8.19", "false"),
                              ("v0.9.2-rc.1", "false"), ("v1.0.0", "true")):
        output.write_text("")
        result = subprocess.run([bash, "-e", "-c", script], cwd=repo, capture_output=True, text=True, timeout=15,
                                env={**env, "VERSION": version, "GITHUB_OUTPUT": str(output)})
        assert result.returncode == 0, result.stderr
        assert output.read_text() == f"promote_latest={expected}\n", version


def test_release_requires_read_only_preflight_before_any_publication(tmp_path):
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    preflight = workflow["jobs"]["preflight"]
    assert all(permission == "read" for permission in preflight["permissions"].values())
    assert "secrets." not in str(preflight)
    assert "--github --remote-catalog" in preflight["steps"][-1]["run"]
    assert preflight["steps"][0]["with"]["fetch-depth"] == 0
    release = workflow["jobs"]["create-release"]
    assert release["needs"] == "preflight"
    assert release["environment"] == "release-signing"
    publish = release["steps"][-1]
    assert publish["with"]["make_latest"] == "${{ steps.promote.outputs.promote_latest }}"
    assert workflow["concurrency"]["cancel-in-progress"] is False
    script = next(s["run"] for s in preflight["steps"] if s.get("name", "").startswith("Require the workflow"))
    bash = resolve_shell().path if os.name == "nt" else shutil.which("bash")
    assert bash, "Git Bash or Bash is required to exercise release gates"
    runtime_env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP") if key in os.environ}
    for ref, expected in (("refs/tags/v0.8.19", 0), ("refs/heads/main", 1), ("refs/tags/v0.8.18", 1)):
        result = subprocess.run([bash, "-e", "-c", script], cwd=tmp_path,
                                env={**runtime_env, "WORKFLOW_REF": ref, "RELEASE_TAG": "v0.8.19"},
                                capture_output=True, text=True)
        assert result.returncode == expected, result.stderr


def test_signing_prerequisites_fail_before_publication(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    steps = workflow["jobs"]["create-release"]["steps"]
    gate = next(step for step in steps if step.get("name", "").startswith("Check stable signing"))
    assert steps.index(gate) < next(i for i, step in enumerate(steps) if "action-gh-release" in step.get("uses", ""))
    assert gate["if"] == "steps.promote.outputs.promote_latest == 'true'"
    script = gate["run"].split("<<'JS'\n", 1)[1].rsplit("\nJS", 1)[0]
    runtime_env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP") if key in os.environ}
    # Ephemeral test keys, never production keys or repository artifacts.
    key = Ed25519PrivateKey.generate()
    public = tmp_path / "desktop/build/application-update-key.pem"
    public.parent.mkdir(parents=True)
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    env = {**runtime_env, "DESKTOP_APPLICATION_SIGNING_KEY": key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
        "CLOUDFLARE_API_TOKEN": "fixture", "CLOUDFLARE_ACCOUNT_ID": "fixture",
        "ACCOUNT_HUB_DATABASE_ID": "11111111-1111-1111-1111-111111111111"}

    def run(values):
        return subprocess.run(["node", "--input-type=module", "-e", script], cwd=tmp_path, env=values,
                              capture_output=True, text=True, timeout=15)

    result = run(env)
    assert result.returncode == 0, result.stderr
    for name in gate["env"]:
        result = run({k: v for k, v in env.items() if k != name})
        assert result.returncode != 0 and f"{name} is missing" in result.stderr
    public.write_bytes(Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    result = run(env)
    assert result.returncode != 0 and "does not match" in result.stderr


def test_installation_pages_direct_android_users_to_play():
    for path in ("README.md", "android/README.md"):
        content = (ROOT / path).read_text()
        assert "https://play.google.com/store/apps/details?id=com.hobbycoders.aishuffle" in content
        assert "releases/download/android-latest/" not in content


def test_docker_release_rebuild_keeps_container_verification_gate():
    workflow = yaml.safe_load((ROOT / ".github/workflows/docker-publish.yml").read_text())
    steps = {step["name"]: step for step in workflow["jobs"]["build"]["steps"] if "name" in step}
    gate = "startsWith(github.ref, 'refs/tags/') || inputs.release_tag != ''"
    assert steps["Verify actual container lifecycle and UI"]["if"] == gate
    assert steps["Upload container evidence"]["if"] == f"always() && ({gate})"
    assert steps["Checkout release source"]["with"]["ref"] == "refs/tags/${{ inputs.release_tag }}"


def test_stable_update_is_signed_only_after_verification_in_a_protected_environment():
    release = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    job = release["jobs"]["source-stable"]
    assert set(job["needs"]) == {"create-release", "docker", "buyer-install"}
    assert job["environment"] == "release-signing"
    assert "needs.create-release.outputs.promote_latest == 'true'" in job["if"]
    assert "github.actor_id == '76577322'" in job["if"]
    checkout = job["steps"][0]
    assert checkout["with"]["ref"] == "refs/tags/${{ needs.create-release.outputs.release_tag }}"
    assert checkout["with"]["fetch-depth"] == 0
    publish = job["steps"][-1]
    assert publish["run"] == "node desktop/scripts/publish-source.mjs"
    assert publish["env"]["SOURCE_CHANNEL"] == "stable"
    assert publish["env"]["RELEASE_TAG"] == "${{ needs.create-release.outputs.release_tag }}"
    assert job["concurrency"]["group"] == "desktop-source-publish"
    # Automatic main publication cannot overwrite the stable release feed.
    edge = yaml.safe_load((ROOT / ".github/workflows/desktop-source.yml").read_text())
    assert edge["jobs"]["publish"]["steps"][-1]["env"]["SOURCE_CHANNEL"] == "edge"
