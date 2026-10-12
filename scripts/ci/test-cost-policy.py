"""CI cost policy: GitHub Actions does not test code.

Tests run on the developer's machine (scripts/local_ci.py and the pre-push
hook). Workflows start automatically only to publish: a release tag, and
daily nightly publication from main after a worker posts a local full-suite pass.
Everything else is a manual dispatch for what no local machine can do.
"""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
# The only workflows that may start without a person dispatching them.
AUTOMATIC = {
    "desktop-source.yml": {"schedule", "workflow_dispatch"},
    "release.yml": {"push", "workflow_dispatch"},
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
    def test_no_workflow_tests_pull_requests_or_runs_on_its_own(self):
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

    def test_automatic_pushes_are_narrow(self):
        self.assertEqual(workflow("release.yml")["on"]["push"], {"tags": ["v*.*.*"]})
        self.assertEqual(workflow("desktop-source.yml")["on"]["schedule"], [{"cron": "0 6 * * *"}])

    def test_no_pr_only_machinery_is_left(self):
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(workflow=path.name):
                for leftover in ("github.event.pull_request", "ci:full", "require-full-ci.py",
                                 "android-inputs.py", "github.base_ref", "paths-filter"):
                    self.assertNotIn(leftover, text)
        self.assertFalse((ROOT / "scripts/ci/require-full-ci.py").exists())

    def test_edge_publication_waits_for_the_owner_local_pass(self):
        config = workflow("desktop-source.yml")
        self.assertEqual(set(config["on"]), {"schedule", "workflow_dispatch"})
        gate = config["jobs"]["gate"]
        for clause in ("github.event_name == 'schedule'", "github.ref == 'refs/heads/main'",
                       "github.actor_id == '76577322'", "github.triggering_actor == github.actor"):
            self.assertIn(clause, gate["if"])
        step = next(s for s in gate["steps"] if "edge_publication_gate.py" in s.get("run", ""))
        self.assertEqual(step["name"], "Require a local full-suite pass on this revision")
        self.assertEqual(config["jobs"]["publish"]["needs"], "gate")
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
        self.assertEqual(backend["jobs"]["backend"]["env"]["PYTEST_WORKERS"], "${{ inputs.runner_cpus == '4' && '4' || '2' }}")
        for config, key, timeout in [(backend, "backend", "35"), (javascript, "frontend", "20")]:
            self.assertEqual(config["jobs"][key]["timeout-minutes"], timeout)
            self.assertEqual(config["jobs"][key]["runs-on"], "ubuntu-latest")
        command = next(step["run"] for step in backend["jobs"]["backend"]["steps"] if step.get("name") == "Run fast test subset")
        self.assertIn('-n "$PYTEST_WORKERS"', command)

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
                        self.assertLessEqual(set(job["strategy"]["matrix"]["os"]), allowed)
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
                    push = next(i for i, s in enumerate(steps)
                                if 'docker push "$tag"' in s.get("run", ""))
                    self.assertGreater(push, verify)
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
                     "hub", "site", "license", "uptime", "android-play", "workflows", "update-reach"):
            self.assertIn(name, suites)
        self.assertNotIn("registry", suites, "the private registry has been retired")
        hook = (ROOT / ".githooks/pre-push").read_text(encoding="utf-8")
        self.assertIn("scripts/local_ci.py", hook)
        self.assertIn("pre-push", hook)


if __name__ == "__main__":
    unittest.main()
