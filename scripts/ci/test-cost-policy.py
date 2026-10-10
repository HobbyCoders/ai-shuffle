"""CI cost policy: GitHub Actions does not test code.

Tests run on the developer's machine (scripts/local_ci.py and the pre-push
hook). Workflows start automatically only to publish: a release tag, and
edge publication from main once a worker has posted a local full-suite pass.
Everything else is a manual dispatch for what no local machine can do.
"""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
# The only workflows that may start without a person dispatching them.
AUTOMATIC = {
    "desktop-source.yml": {"push", "status", "workflow_dispatch"},
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
                self.assertFalse(found & FORBIDDEN, f"{name} starts on {sorted(found & FORBIDDEN)}")
                if name in AUTOMATIC:
                    self.assertEqual(found, AUTOMATIC[name])
                else:
                    self.assertLessEqual(found, {"workflow_dispatch", "workflow_call"},
                                         f"{name} must be manual or called by a release")

    def test_automatic_pushes_are_narrow(self):
        self.assertEqual(workflow("release.yml")["on"]["push"], {"tags": ["v*.*.*"]})
        self.assertEqual(workflow("desktop-source.yml")["on"]["push"], {"branches": ["main"]})

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
        self.assertEqual(config["on"]["status"] or None, None)
        gate = config["jobs"]["gate"]
        for clause in ("github.event.context == 'local/full-suite'", "github.event.state == 'success'",
                       "github.event.sender.id == 76577322", "github.event.sha == github.sha"):
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

    def test_manual_benchmarks_keep_their_runner_inputs(self):
        backend = workflow("ci-backend.yml")
        javascript = workflow("ci-frontend.yml")
        self.assertEqual(backend["on"]["workflow_dispatch"]["inputs"]["runner_cpus"]["default"], "2")
        self.assertEqual(javascript["on"]["workflow_dispatch"]["inputs"]["runner_cpus"]["default"], "4")
        self.assertEqual(backend["jobs"]["backend"]["env"]["PYTEST_WORKERS"], "${{ inputs.runner_cpus == '4' && '4' || '2' }}")
        for config, key, timeout in [(backend, "backend", "35"), (javascript, "frontend", "20")]:
            self.assertEqual(config["jobs"][key]["timeout-minutes"], timeout)
        command = next(step["run"] for step in backend["jobs"]["backend"]["steps"] if step.get("name") == "Run fast test subset")
        self.assertIn('-n "$PYTEST_WORKERS"', command)

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
                     "hub", "site", "license", "registry", "uptime", "android-play", "workflows", "update-reach"):
            self.assertIn(name, suites)
        hook = (ROOT / ".githooks/pre-push").read_text(encoding="utf-8")
        self.assertIn("scripts/local_ci.py", hook)
        self.assertIn("pre-push", hook)


if __name__ == "__main__":
    unittest.main()
