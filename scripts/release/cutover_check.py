"""Read-only cutover preflight in the public checkout.

--private-export E --public-root S --old-repo HobbyCoders/ai-shuffle-private
--new-repo HobbyCoders/ai-shuffle --private-checkout PATH [--json]

Checks live bridge/identity/freeze evidence, immutable sequence, catalog and the
reviewed first public PR. This is NOT authorization to rename, deploy or publish.
Missing evidence is FAIL, never a warning counted as success. Physical worker
canaries and the complete historical sequence inventory remain owner sign-offs.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.ci import public_cutover as cutover
from scripts.ci.release_preflight import verify_catalog, verify_merged_pr
from scripts.release.bridge_attestation import attest


def checks(root, args):
    results = []
    def check(name, fn):
        try:
            reason = fn()
            results.append({"check": name, "passed": True, "reason": reason or "verified"})
        except Exception as error:
            # Do not leak subprocess/GitHub bodies.
            results.append({"check": name, "passed": False, "reason": str(error) if isinstance(error, ValueError) else type(error).__name__})
    def clean():
        if cutover.git(root, "status", "--porcelain"):
            raise ValueError("Checkout must be clean")
    def identities():
        if (args.old_repo, args.new_repo) != (cutover.OLD_REPO, cutover.NEW_REPO):
            raise ValueError("Unexpected cutover repository names")
        metadata = cutover.api(args.new_repo)
        if metadata.get("full_name") != args.new_repo or metadata.get("private") is not False:
            raise ValueError("New repository must be public with the expected full name")
        _, record = cutover.mapping(root)
        if (not record or type(metadata.get("id")) is not int or metadata["id"] <= 0
                or metadata["id"] == record["old_repository_id"]):
            raise ValueError("New public repository must have a distinct numeric identity")
        branch = cutover.api(args.new_repo, "branches/main")
        if branch.get("protected") is not True or branch.get("commit", {}).get("sha") != cutover.git(root, "rev-parse", "HEAD"):
            raise ValueError("Checkout must be the exact protected public main revision")
    def frozen():
        # Repository-wide freeze also covers newly added/manual publishers.
        # Individual workflow state can remain "active" while Actions is off.
        for repository in (args.old_repo, args.new_repo, "HobbyCoders/hobbycoders-cloud"):
            if cutover.api(repository, "actions/permissions").get("enabled") is not False:
                raise ValueError(f"Actions must be disabled for {repository}")
            for status in ("queued", "in_progress", "waiting", "pending", "requested"):
                runs = cutover.api(repository, f"actions/runs?status={status}&per_page=1")
                if runs.get("total_count") != 0:
                    raise ValueError(f"Workflow runs must be drained for {repository}")
    def staging():
        import yaml
        workflow = yaml.load((root / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
        inputs = workflow.get("on", {}).get("workflow_dispatch", {}).get("inputs", {})
        if "image_digest" not in inputs:
            raise ValueError("Release must support guarded image_digest reuse")
        jobs = workflow.get("jobs", {})
        promotion = jobs.get("promote-image", {})
        required = {"create-release", "buyer-install", "source-stable", "docker", "android-apk", "linux-release", "desktop"}
        if not required <= set(promotion.get("needs", [])):
            raise ValueError("Docker aliases must wait for every release artifact and source signing")
        expected = "always() && !failure() && !cancelled() && needs.create-release.outputs.promote_latest == 'true' && "
        expected += " && ".join(f"needs.{job}.result == 'success'" for job in
                                ("create-release", "docker", "buyer-install", "source-stable", "android-apk", "linux-release"))
        expected += " && (needs.desktop.result == 'success' || (!inputs.build_desktop && needs.desktop.result == 'skipped'))"
        if "".join(promotion.get("if", "").split()) != "".join(expected.split()):
            raise ValueError("Promotion must require successful artifacts and allow skipped Windows only when not requested")
        if jobs.get("docker", {}).get("with", {}).get("promote_latest") != "false":
            raise ValueError("Release builder must not move aliases")
        if jobs.get("linux-build", {}).get("uses") != "./.github/workflows/desktop-linux-artifact.yml":
            raise ValueError("Release must call the exact-tag Linux artifact workflow")
        linux = jobs.get("linux-release", {})
        if "linux-build" not in linux.get("needs", []) or not any("attach_linux.py" in s.get("run", "") for s in linux.get("steps", [])):
            raise ValueError("Linux artifacts/checksums need durable release attachment and read-back")
        if "build_desktop" not in inputs or jobs.get("desktop", {}).get("uses") != "./.github/workflows/desktop-installer.yml":
            raise ValueError("Windows installer release wiring is missing")
        if jobs.get("android-apk", {}).get("uses") != "./.github/workflows/android.yml":
            raise ValueError("Merge the Play-preparation android-apk release caller before cutover")
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
        return "digest reuse, final alias ordering and durable Linux/Android artifacts; full staging remains follow-up #2335"
    check("guarded image reuse and promotion", staging)
    check("clean checkout", clean)
    check("repository identities", identities)
    check("all publishers frozen/drained", frozen)
    check("signed bridge and exact export tree", lambda: attest(root, args.private_export, args.public_root, args.private_checkout) and "verified")
    check("committed container sequence", lambda: str(cutover.container_sequence(root)))
    check("public catalog bytes", lambda: verify_catalog(root, remote=True))
    check("merged public release PR", lambda: verify_merged_pr(cutover.git(root, "rev-parse", "HEAD")))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("private-export", "public-root", "old-repo", "new-repo"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--private-checkout", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    results = checks(Path.cwd(), args)
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for row in results:
            print(f"{'PASS' if row['passed'] else 'FAIL'} {row['check']}: {row['reason']}")
    raise SystemExit(0 if all(row["passed"] for row in results) else 1)


if __name__ == "__main__":
    main()
