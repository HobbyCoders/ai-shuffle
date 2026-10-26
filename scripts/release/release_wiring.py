"""Read-only check of the stable release workflow wiring: python scripts/release/release_wiring.py

Verifies that release.yml and the Docker workflow it calls keep guarded image
digest reuse, lifecycle verification before any push, exact-tag artifacts and
alias promotion only after every artifact (or a verified Windows skip). Missing
wiring raises ValueError. This is not authorization to tag or publish.
"""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def verify(root):
    workflow = yaml.load((root / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
    inputs = workflow.get("on", {}).get("workflow_dispatch", {}).get("inputs", {})
    if "image_digest" not in inputs:
        raise ValueError("Release must support guarded image_digest reuse")
    jobs = workflow.get("jobs", {})
    promotion = jobs.get("promote-image", {})
    required = {"create-release", "buyer-install", "source-stable", "docker", "android-apk", "linux-release", "desktop", "windows-policy"}
    if not required <= set(promotion.get("needs", [])):
        raise ValueError("Docker aliases must wait for every release artifact and source signing")
    expected = "always() && !failure() && !cancelled() && needs.create-release.outputs.promote_latest == 'true' && "
    expected += " && ".join(f"needs.{job}.result == 'success'" for job in
                            ("create-release", "docker", "buyer-install", "source-stable", "android-apk", "linux-release"))
    expected += " && needs.windows-policy.result == 'success' && (needs.desktop.result == 'success' || "
    expected += "(needs.windows-policy.outputs.build == 'false' && needs.desktop.result == 'skipped'))"
    if "".join(promotion.get("if", "").split()) != "".join(expected.split()):
        raise ValueError("Promotion must require successful artifacts or an explicit Windows not-needed decision")
    if jobs.get("docker", {}).get("with", {}).get("promote_latest") != "false":
        raise ValueError("Release builder must not move aliases")
    if jobs.get("linux-build", {}).get("uses") != "./.github/workflows/desktop-linux-artifact.yml":
        raise ValueError("Release must call the exact-tag Linux artifact workflow")
    linux = jobs.get("linux-release", {})
    if "linux-build" not in linux.get("needs", []) or not any("attach_linux.py" in s.get("run", "") for s in linux.get("steps", [])):
        raise ValueError("Linux artifacts/checksums need durable release attachment and read-back")
    desktop = jobs.get("desktop", {})
    if (inputs.get("build_desktop", {}).get("type") != "boolean"
            or desktop.get("if") != "needs.create-release.outputs.promote_latest == 'true' && needs.windows-policy.outputs.build == 'true'"
            or set(desktop.get("needs", [])) != {"create-release", "windows-policy"}
            or desktop.get("uses") != "./.github/workflows/desktop-installer.yml"):
        raise ValueError("Windows installer release wiring is missing")
    policy = jobs.get("windows-policy", {})
    if (policy.get("needs") != "preflight" or
            not any("windows_installer_policy.py" in s.get("run", "") for s in policy.get("steps", []))):
        raise ValueError("Windows decision must follow preflight and verify hub/input evidence")
    if jobs.get("android-apk", {}).get("uses") != "./.github/workflows/android.yml":
        raise ValueError("Release must call the android-apk workflow")
    def require(actual, expected, reason):
        if actual != expected:
            raise ValueError(reason)

    release_tag = "${{ needs.create-release.outputs.release_tag }}"
    docker = jobs.get("docker", {})
    require(docker.get("uses"), "./.github/workflows/docker-publish.yml", "Release must call the guarded Docker workflow")
    require(docker.get("with", {}).get("image_digest"), "${{ inputs.image_digest }}", "Release must forward the accepted image_digest")
    require(docker.get("with", {}).get("release_tag"), release_tag, "Docker must use the exact release tag")
    for name in ("android-apk", "desktop"):
        require(jobs[name].get("with", {}).get("release_tag"), release_tag, f"{name} must use the exact release tag")
    require(jobs["android-apk"].get("with", {}).get("publish_download"), "true", "android-apk must publish the APK download")
    require(jobs["linux-build"].get("with", {}).get("version"), "${{ needs.preflight.outputs.release_version }}", "Linux build must use the exact release version")
    attach = next(s for s in linux["steps"] if "attach_linux.py" in s.get("run", ""))
    require(attach.get("env", {}).get("RELEASE_TAG"), "${{ needs.preflight.outputs.release_tag }}", "Linux attachment must use the exact release tag")
    require(attach.get("run"), 'python3 scripts/release/attach_linux.py --tag "$RELEASE_TAG" --directory linux-artifact', "Linux attachment must verify and publish the requested tag")
    checkout = next(s for s in linux["steps"] if s.get("uses") == "actions/checkout@v4")
    require(checkout.get("with", {}).get("ref"), "refs/tags/${{ needs.preflight.outputs.release_tag }}", "Linux attachment checkout must be the exact tag")

    called = yaml.load((root / ".github/workflows/docker-publish.yml").read_text(), Loader=yaml.BaseLoader)
    call = called.get("on", {}).get("workflow_call", {})
    require(call.get("inputs", {}).get("image_digest", {}).get("type"), "string", "Called Docker workflow must accept image_digest")
    require(call.get("outputs", {}).get("digest", {}).get("value"), "${{ jobs.build.outputs.digest }}", "Docker workflow must return the verified digest")
    build = called.get("jobs", {}).get("build", {})
    require(build.get("outputs", {}).get("digest"), "${{ steps.published.outputs.digest }}", "Docker job must return the verified digest")
    steps = build.get("steps", [])
    def step(name):
        matches = [s for s in steps if s.get("name") == name]
        if len(matches) != 1:
            raise ValueError(f"Expected one Docker step: {name}")
        return matches[0]
    reuse = step("Verify and load an existing candidate digest")
    candidate = step("Build candidate image")
    verify = step("Verify actual container lifecycle and UI")
    push = step("Push the candidate without rebuilding")
    recorded = step("Record immutable image digest")
    require(reuse.get("if"), "inputs.image_digest != ''", "Digest reuse must run only for a supplied digest")
    require(reuse.get("env", {}).get("IMAGE_DIGEST"), "${{ inputs.image_digest }}", "Reuse must verify the supplied digest")
    require(reuse.get("env", {}).get("SOURCE"), "${{ inputs.release_tag && 'release-source' || '.' }}", "Reuse must verify the exact source checkout")
    require(reuse.get("env", {}).get("RELEASE_TAG"), "${{ inputs.release_tag }}", "Reuse must bind the version tag")
    if not reuse.get("run", "").startswith('python3 scripts/release/verify_image.py --digest "$IMAGE_DIGEST" --checkout "$SOURCE"\n'):
        raise ValueError("Reuse must verify image provenance before any further action")
    for item in (candidate, push):
        require(item.get("if"), "inputs.image_digest == ''", "A reused digest must never rebuild or push a new candidate")
    require(candidate.get("with", {}).get("push"), "false", "Build must not publish before verification")
    require(candidate.get("with", {}).get("load"), "true", "Build must load the candidate for verification")
    require(push.get("run"), "python3 scripts/release/push_candidate.py", "Candidate publication must use the immutable-tag guard")
    require(verify.get("if"), "startsWith(github.ref, 'refs/tags/') || inputs.release_tag != '' || inputs.candidate_tag != '' || inputs.image_digest != ''", "All release/candidate/reused images require lifecycle verification")
    require(recorded.get("env", {}).get("REUSED_DIGEST"), "${{ inputs.image_digest }}", "Output must preserve the accepted digest")
    order = [steps.index(s) for s in (reuse, candidate, verify, push, recorded)]
    if order != sorted(order):
        raise ValueError("Docker reuse, lifecycle verification, push and digest output must stay ordered")
    source = step("Checkout release source")
    require(source.get("with", {}).get("ref"), "refs/tags/${{ inputs.release_tag }}", "Docker source checkout must use the exact release tag")
    require(source.get("with", {}).get("path"), "release-source", "Docker release-source path must match provenance verification")
    return "digest reuse, final alias ordering and durable Linux/Android artifacts"


def main():
    print(verify(ROOT))


if __name__ == "__main__":
    main()
