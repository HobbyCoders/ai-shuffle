"""CI policy: fast local push checks, full PR suites on standard free runners.

Publishing still requires explicit dispatch or an attested local full pass.
PR checks have no secrets, publishing permissions, draft skips or path filters.
"""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
PR_CHECKS = {
    "ci-backend.yml", "ci-fluid.yml", "ci-frontend.yml", "desktop.yml",
    "desktop-source-checks.yml", "windows-suite.yml", "ci-instance-identity.yml",
    "ci-artifact-policy.yml", "ci-browser-workspace.yml", "container-postgres.yml",
}
# The only workflows that may start without a person dispatching them.
AUTOMATIC = {
    "desktop-source.yml": {"schedule", "workflow_dispatch", "workflow_run"},
    "main-suite.yml": {"push", "workflow_dispatch"},
    **{name: {"pull_request", "workflow_dispatch", "workflow_call"} for name in PR_CHECKS},
}
FORBIDDEN = {"pull_request", "pull_request_target", "merge_group", "schedule", "workflow_run",
             "check_run", "check_suite", "issue_comment", "pull_request_review"}


def workflow(name):
    return yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def triggers(config):
    on = config["on"]
    if isinstance(on, str):
        return {on}
    return set(on)


class CostPolicyTests(unittest.TestCase):
    def test_only_pr_checks_and_attested_publication_run_automatically(self):
        names = sorted(path.name for path in WORKFLOWS.glob("*.yml"))
        self.assertGreater(len(names), 10)
        for name in names:
            with self.subTest(workflow=name):
                found = triggers(workflow(name))
                self.assertFalse((found - AUTOMATIC.get(name, set())) & FORBIDDEN,
                                 f"{name} starts on forbidden events")
                if name in AUTOMATIC:
                    self.assertEqual(found, AUTOMATIC[name])
                else:
                    self.assertLessEqual(found, {"workflow_dispatch", "workflow_call"},
                                         f"{name} must be manual or called by a release")

    def test_pr_suites_are_unfiltered_read_only_and_cancel_superseded_runs(self):
        for name in sorted(PR_CHECKS):
            config = workflow(name)
            text = (WORKFLOWS / name).read_text(encoding="utf-8")
            with self.subTest(workflow=name):
                self.assertEqual(config["on"]["pull_request"], "")
                self.assertEqual(config["permissions"], {"contents": "read"})
                self.assertEqual(config["concurrency"], {
                    "group": name[:-4] + "-${{ github.event.pull_request.number || github.ref }}",
                    "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
                })
                for forbidden in ("secrets.", "pull_request_target", ".draft", "secrets: inherit"):
                    self.assertNotIn(forbidden, text)
                for job in config["jobs"].values():
                    self.assertEqual(job.get("permissions", {"contents": "read"}), {"contents": "read"})
                    runner = job.get("runs-on")
                    if runner == "${{ matrix.os }}":
                        self.assertEqual(job["strategy"]["matrix"]["os"],
                            "${{ fromJSON((github.event_name == 'pull_request' || inputs.main_suite == true) && "
                            "'[\"windows-latest\"]' || '[\"windows-latest\", \"macos-latest\"]') }}")
                    else:
                        self.assertIn(runner, {"ubuntu-latest", "windows-latest"})

    def test_stable_release_requires_explicit_dispatch(self):
        self.assertEqual(triggers(workflow("release.yml")), {"workflow_dispatch"})
        self.assertEqual(workflow("desktop-source.yml")["on"]["schedule"], [{"cron": "0 6 * * *"}])

    def test_no_pr_only_machinery_is_left(self):
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(workflow=path.name):
                for leftover in ("ci:full", "require-full-ci.py", "android-inputs.py", "paths-filter"):
                    self.assertNotIn(leftover, text)
        self.assertFalse((ROOT / "scripts/ci/require-full-ci.py").exists())

    def test_edge_publication_waits_for_the_owner_local_pass(self):
        config = workflow("desktop-source.yml")
        self.assertEqual(set(config["on"]), {"schedule", "workflow_dispatch", "workflow_run"})
        gate = config["jobs"]["gate"]
        for clause in ("github.event_name == 'schedule'", "github.ref == 'refs/heads/main'",
                       "github.actor_id == '76577322'", "github.triggering_actor == github.actor"):
            self.assertIn(clause, gate["if"])
        step = next(s for s in gate["steps"] if "edge_publication_gate.py" in s.get("run", ""))
        # Selection may choose an ancestor, but can only authorize a revision
        # through the owner's successful local attestation, never its label.
        self.assertEqual(step["run"], "python scripts/ci/edge_publication_gate.py --select")
        self.assertEqual(step["id"], "ci")
        self.assertEqual(gate["outputs"]["ready"], "${{ steps.ci.outputs.ready }}")
        self.assertEqual(gate["outputs"]["revision"], "${{ steps.ci.outputs.revision }}")
        import importlib.util
        spec = importlib.util.spec_from_file_location("cost_policy_gate", ROOT / "scripts/ci/edge_publication_gate.py")
        publication = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(publication)
        self.assertEqual(publication.CONTEXT, "local/full-suite")
        self.assertEqual(publication.OWNER_ID, "76577322")
        newer, ancestor = "a" * 40, "b" * 40
        def status(state, creator):
            return {"context": publication.CONTEXT, "state": state, "creator": {"id": creator}}
        statuses = {
            newer: [status("success", "7"), status("pending", publication.OWNER_ID)],
            ancestor: [status("success", publication.OWNER_ID)],
        }
        api = lambda endpoint: ({"workflow_runs": []} if endpoint.startswith("actions/")
                                else statuses[endpoint.split("/")[1]])
        self.assertEqual(publication.newest_attested(api, [newer, ancestor])[0], ancestor)
        statuses[ancestor] = [status("failure", publication.OWNER_ID)]
        self.assertIsNone(publication.newest_attested(api, [newer, ancestor])[0])
        statuses[newer] = [status("success", publication.OWNER_ID)]
        self.assertEqual(publication.newest_attested(api, [newer, ancestor])[0], newer)
        self.assertEqual(config["jobs"]["publish"]["needs"], ["gate", "ui"])
        self.assertEqual(config["jobs"]["ui"]["needs"], "gate")
        self.assertEqual(config["jobs"]["ui"]["permissions"], {"contents": "read"})
        self.assertEqual(config["jobs"]["publish"]["if"], "needs.gate.outputs.ready == 'true'")

    def test_ios_macos_job_is_unsigned_and_bounded(self):
        config = workflow("ios.yml")
        self.assertEqual(triggers(config), {"workflow_dispatch"})
        self.assertEqual(config["permissions"], {"contents": "read"})
        job = config["jobs"]["simulator"]
        self.assertEqual(list(config["jobs"]), ["simulator"])
        self.assertLessEqual(int(job["timeout-minutes"]), 30)
        text = (WORKFLOWS / "ios.yml").read_text(encoding="utf-8")
        # Simulator-only CI: no signing material, no uploads, no publishing.
        for forbidden in ["secrets.", "environment:", "upload-artifact", "altool", "-exportArchive", "archive "]:
            self.assertNotIn(forbidden, text)
        self.assertIn("CODE_SIGNING_ALLOWED=NO", text)

    def test_manual_checks_use_standard_runners_and_keep_backend_parallelism(self):
        backend = workflow("ci-backend.yml")
        javascript = workflow("ci-frontend.yml")
        self.assertEqual(backend["on"]["workflow_dispatch"]["inputs"]["runner_cpus"]["default"], "2")
        self.assertEqual(javascript["on"]["workflow_dispatch"], "")
        shard = backend["jobs"]["backend-shard"]
        self.assertEqual(shard["strategy"], {"fail-fast": "false", "matrix": {"shard": ["1", "2", "3", "4"]}})
        self.assertEqual(backend["jobs"]["backend"]["name"], "Backend tests")
        self.assertEqual(backend["jobs"]["backend"]["if"], "always()")
        for config, key, timeout in [(backend, "backend-shard", "35"), (backend, "backend-contracts", "35"),
                                     (backend, "backend-security", "35"), (javascript, "frontend", "20")]:
            self.assertEqual(config["jobs"][key]["timeout-minutes"], timeout)
            self.assertEqual(config["jobs"][key]["runs-on"], "ubuntu-latest")
        command = next(step["run"] for step in shard["steps"] if step.get("name") == "Run fast test subset")
        self.assertIn('-n 2', command)
        self.assertIn('-p scripts.ci.backend_shards', command)

    def test_public_app_workflows_use_standard_hosted_runners(self):
        # These workflows are moving to hobbycoders-cloud in a separate PR.
        private_cloud = {
            "deploy-hub.yml", "deploy-license.yml", "deploy-site.yml", "deploy-uptime.yml",
            "ci-license.yml", "ci-uptime.yml", "withdraw-release.yml",
        }
        allowed = {"ubuntu-latest", "ubuntu-24.04", "windows-latest", "macos-latest", "macos-14"}
        for path in sorted(WORKFLOWS.glob("*.yml")):
            if path.name in private_cloud:
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(workflow=path.name):
                self.assertNotIn("blacksmith", text.lower())
                for job in workflow(path.name)["jobs"].values():
                    if "runs-on" not in job:  # Reusable workflow call.
                        continue
                    runner = job["runs-on"]
                    if runner == "${{ matrix.os }}":
                        matrix = job["strategy"]["matrix"]["os"]
                        if isinstance(matrix, list):
                            self.assertLessEqual(set(matrix), allowed)
                        else:
                            self.assertIn("fromJSON((github.event_name == 'pull_request' || inputs.main_suite == true)", matrix)
                    else:
                        self.assertIn(runner, allowed)

    def test_docker_jobs_use_branch_scoped_github_cache_and_verify_before_push(self):
        for name in ("build-branch.yml", "container-postgres.yml", "docker-publish.yml"):
            with self.subTest(workflow=name):
                jobs = workflow(name)["jobs"]
                job = jobs.get("build", jobs.get("container-postgres"))
                steps = job["steps"]
                setup = next(s for s in steps if s.get("uses") == "docker/setup-buildx-action@v3")
                self.assertNotIn("cache-key", setup.get("with", {}))
                build = next(s for s in steps if s.get("uses") == "docker/build-push-action@v6")
                options = build["with"]
                self.assertEqual(options["cache-from"], "type=gha,scope=Dockerfile")
                self.assertEqual(options["cache-to"], "type=gha,mode=max,scope=Dockerfile")
                self.assertEqual(options["load"], "true")
                self.assertEqual(options["push"], "false")
                self.assertEqual(options["platforms"], "linux/amd64")
                verify = next(i for i, s in enumerate(steps)
                              if "scripts/verify_container_postgres.py" in s.get("run", ""))
                self.assertGreater(verify, steps.index(build))
                if name != "container-postgres.yml":
                    command = ("python3 scripts/release/push_candidate.py" if name == "docker-publish.yml"
                               else 'docker push "$tag"')
                    push = next(i for i, s in enumerate(steps) if command in s.get("run", ""))
                    self.assertGreater(push, verify)
                    if name == "docker-publish.yml":
                        # Reuse skips build/push but still exercises the actual
                        # pulled container. Untagged candidates must verify too.
                        for step in (build, steps[push]):
                            self.assertEqual(step["if"], "inputs.image_digest == ''")
                        self.assertEqual(steps[verify]["if"],
                                         "startsWith(github.ref, 'refs/tags/') || inputs.release_tag != '' || inputs.candidate_tag != '' || inputs.image_digest != ''")
                self.assertNotIn("fstrim", (WORKFLOWS / name).read_text(encoding="utf-8"))

    def test_browser_diagnostics_are_failure_only_and_short_lived(self):
        steps = workflow("ci-browser-workspace.yml")["jobs"]["workspace"]["steps"]
        upload = next(step for step in steps if step.get("uses", "").startswith("actions/upload-artifact"))
        self.assertEqual(upload["if"], "failure()")
        self.assertEqual(upload["with"]["retention-days"], "3")

    def test_local_checks_cover_what_ci_used_to_run(self):
        import sys
        sys.path.insert(0, str(ROOT / "scripts"))
        import local_ci  # pyright: ignore[reportMissingImports]
        suites = {suite.name for suite in local_ci.SUITES}
        for name in ("backend", "fluid", "frontend", "mobile", "tools", "browser-mcp", "docs", "desktop",
                     "android-bridge", "android-play", "workflows", "update-reach"):
            self.assertIn(name, suites)
        self.assertNotIn("registry", suites, "the private registry has been retired")
        hook = (ROOT / ".githooks/pre-push").read_text(encoding="utf-8")
        self.assertIn("scripts/local_ci.py", hook)
        self.assertIn("pre-push", hook)


if __name__ == "__main__":
    unittest.main()
