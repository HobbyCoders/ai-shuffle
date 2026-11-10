"""CI policy: fast local push checks, full PR suites on standard free runners.

Publishing still requires explicit dispatch or an attested local full pass.
Unfiltered PR suites have no secrets or publishing permissions. Android additionally
runs path-filtered unsigned native checks (debug emulator suite and minified
release smoke); its release-only steps stay owner-gated. UI journeys run whole
on main; a pull request runs the ones its changed files need.
"""
from pathlib import Path
import json
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
PR_CHECKS = {
    "ci-backend.yml", "ci-fluid.yml", "ci-frontend.yml", "desktop.yml",
    "desktop-source-checks.yml", "windows-suite.yml", "ci-instance-identity.yml",
    "ci-artifact-policy.yml", "ci-browser-workspace.yml", "container-postgres.yml",
}
# What an APK is built from: the two Android workflows run on PRs changing these.
APK_INPUTS = ["android/**", "mobile/**", "frontend/**"]
# The only workflows that may start without a person dispatching them.
AUTOMATIC = {
    "desktop-source.yml": {"schedule", "workflow_dispatch", "workflow_run"},
    "main-suite.yml": {"push", "schedule", "workflow_dispatch"},
    "android-webview-cache.yml": {"push", "schedule", "workflow_dispatch"},
    "android.yml": {"pull_request", "workflow_dispatch", "workflow_call"},
    "ci-android-minified.yml": {"pull_request", "workflow_dispatch", "workflow_call"},
    "runtime-overlay-acceptance.yml": {"pull_request", "workflow_dispatch"},
    **{name: {"pull_request", "workflow_dispatch", "workflow_call"} for name in PR_CHECKS},
}
RECORD_PASS = "./.github/workflows/record-pass.yml"
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
                    "group": name[:-4] + "-${{ github.event.pull_request.number || github.ref }}"
                             "${{ github.event_name == 'schedule' && '-daily' || github.event_name == 'workflow_dispatch' && '-manual' || '' }}",
                    "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
                })
                for forbidden in ("secrets.", "pull_request_target", ".draft", "secrets: inherit"):
                    self.assertNotIn(forbidden, text)
                for key, job in config["jobs"].items():
                    if job.get("uses") == RECORD_PASS:
                        # Leaves a pass record for main-suite; runs on ubuntu-latest itself.
                        self.assertEqual(set(job), {"needs", "if", "uses", "with"})
                        self.assertIn("github.event_name == 'pull_request'", job["if"])
                        continue
                    # Only the journey selection reads earlier runs and pass records.
                    allowed = ({"contents": "read", "actions": "read", "pull-requests": "read"}
                               if (name, key) == ("ci-frontend.yml", "journey-plan") else {"contents": "read"})
                    self.assertEqual(job.get("permissions", {"contents": "read"}), allowed)
                    runner = job.get("runs-on")
                    if runner == "${{ matrix.os }}":
                        self.assertEqual(job["strategy"]["matrix"]["os"],
                            "${{ fromJSON((github.event_name == 'pull_request' || inputs.main_suite == true) && "
                            "'[\"windows-latest\"]' || '[\"windows-latest\", \"macos-latest\"]') }}")
                    else:
                        self.assertIn(runner, {"ubuntu-latest", "windows-latest"})

    def test_android_native_pr_checks_cover_the_apk_inputs_on_free_runners(self):
        config = workflow("android.yml")
        self.assertEqual(config["on"]["pull_request"], {"paths": [
            *APK_INPUTS, ".github/workflows/android.yml",
            "scripts/ci/test-cost-policy.py", "tests/scripts/test_owner_publication_policy.py",
        ]})
        self.assertEqual(config["permissions"], {"contents": "read"})
        self.assertEqual(config["concurrency"], {
            "group": "android-${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}",
            "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
        })
        self.assertEqual(set(config["jobs"]), {"build", "android-ui", "publish"})
        for name in ("build", "android-ui"):
            job = config["jobs"][name]
            self.assertNotIn("if", job)  # Includes drafts and forks.
            self.assertEqual(job.get("permissions", config["permissions"]), {"contents": "read"})
        for job in config["jobs"].values():
            self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertEqual([leg["api"] for leg in config["jobs"]["android-ui"]["strategy"]["matrix"]["include"]], ["34", "36"])

    def test_minified_android_smoke_runs_on_the_same_apk_inputs_and_stays_read_only(self):
        name = "ci-android-minified.yml"
        config = workflow(name)
        self.assertEqual(config["on"]["pull_request"], {"paths": [
            *APK_INPUTS, ".github/workflows/ci-android-minified.yml", "scripts/ci/test-cost-policy.py",
        ]})
        self.assertEqual(config["permissions"], {"contents": "read"})
        self.assertEqual(config["concurrency"], {
            "group": name[:-4] + "-${{ github.event.pull_request.number || github.ref }}",
            "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
        })
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        for forbidden in ("secrets.", "pull_request_target", ".draft", "secrets: inherit"):
            self.assertNotIn(forbidden, text)
        for job in config["jobs"].values():
            self.assertNotIn("if", job)  # Includes drafts and forks.
            self.assertEqual(job.get("permissions", {"contents": "read"}), {"contents": "read"})
            self.assertEqual(job["runs-on"], "ubuntu-latest")

    def test_ui_journeys_run_whole_off_pull_requests_and_skip_only_on_an_empty_selection(self):
        jobs = workflow("ci-frontend.yml")["jobs"]
        plan, journeys, gate = jobs["journey-plan"], jobs["journeys"], jobs["javascript"]
        for forbidden in ("if", "continue-on-error"):
            self.assertNotIn(forbidden, plan)
        self.assertEqual(plan["outputs"], {"only": "${{ steps.select.outputs.only }}",
                                           "shards": "${{ steps.select.outputs.shards }}",
                                           "key": "${{ steps.select.outputs.key }}"})
        # Reads earlier main-suite runs, pass records and the merged pull request; never writes.
        self.assertEqual(plan["permissions"], {"contents": "read", "actions": "read", "pull-requests": "read"})
        checkout, select = plan["steps"]
        # A pull request needs the merge commit's parents; a push the history
        # back to the last commit main-suite passed on.
        self.assertEqual(checkout["with"], {"fetch-depth": "${{ github.event_name == 'pull_request' && 2 || 0 }}"})
        self.assertEqual(select["env"], {"GH_TOKEN": "${{ github.token }}"})
        self.assertEqual(select["run"], 'python3 -m scripts.devloop.ci matrix --shards 16 >> "$GITHUB_OUTPUT"')
        for step in plan["steps"]:
            for forbidden in ("if", "continue-on-error", "shell"):
                self.assertNotIn(forbidden, step)
        self.assertEqual(journeys["needs"], "journey-plan")
        self.assertEqual(journeys["if"], "needs.journey-plan.outputs.shards != '[]'")
        self.assertNotIn("continue-on-error", journeys)
        self.assertEqual(journeys["strategy"]["matrix"]["shard"],
                         "${{ fromJSON(needs.journey-plan.outputs.shards) }}")
        self.assertEqual(journeys["env"], {"JOURNEY_SELECTION": "${{ needs.journey-plan.outputs.only }}"})
        self.assertLessEqual({"journey-plan", "journeys"}, set(gate["needs"]))
        check = gate["steps"][0]["run"]
        expected = json.loads(check.split("const expected = ", 1)[1].split(";", 1)[0])
        self.assertLessEqual({"journey-plan", "journeys"}, set(expected))
        self.assertIn("const noJourneys = plan.result === 'success' && plan.outputs?.shards === '[]';", check)
        # The other checks skip only when main-suite found a pull request's record for them.
        self.assertIn("const proven = process.env.CHECKS_PROVEN === 'true';", check)
        self.assertIn("const skipped = name === 'journeys' ? noJourneys : name !== 'journey-plan' && proven;", check)
        self.assertIn("assert.equal(results[name].result, skipped ? 'skipped' : 'success', name);", check)
        self.assertEqual(check.count("'skipped'"), 1)
        self.assertEqual(gate["steps"][0]["env"]["CHECKS_PROVEN"], "${{ inputs.checks_proven }}")

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
        condition = config["jobs"]["publish"]["if"]
        for clause in ("always()", "needs.gate.result == 'success'", "needs.gate.outputs.ready == 'true'",
                       "needs.ui.result == 'skipped'", "needs.ui.result == 'success'"):
            self.assertIn(clause, condition)
        self.assertIn("needs.gate.outputs.ui-reused != 'true'", config["jobs"]["ui"]["if"])
        self.assertNotIn("optional", config["jobs"]["ui"]["with"])

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

    def test_journey_map_is_validated_by_javascript_contracts(self):
        jobs = workflow("ci-frontend.yml")["jobs"]
        job = jobs["contracts"]
        gate = jobs["javascript"]
        self.assertIn("contracts", gate["needs"])
        expected = json.loads(gate["steps"][0]["run"].split("const expected = ", 1)[1].split(";", 1)[0])
        self.assertIn("contracts", expected)
        # Skipped only when main-suite found a pull request's record for these files.
        self.assertEqual(job["if"], "inputs.checks_proven != true")
        self.assertNotIn("continue-on-error", job)
        self.assertNotIn("shell", job.get("defaults", {}).get("run", {}))
        step = next(step for step in job["steps"]
                    if step.get("name") == "Validate UI journey map")
        self.assertEqual(step["run"].splitlines(), [
            "node --test frontend/tests/journeys/select.test.mjs",
            "node frontend/tests/journeys/select.mjs --check",
        ])
        self.assertEqual(step.get("working-directory", job["defaults"]["run"]["working-directory"]), ".")
        self.assertNotIn("if", step)
        self.assertNotIn("continue-on-error", step)
        self.assertNotIn("shell", step)

    def test_manual_checks_use_standard_runners_and_keep_backend_parallelism(self):
        backend = workflow("ci-backend.yml")
        javascript = workflow("ci-frontend.yml")
        self.assertEqual(backend["on"]["workflow_dispatch"]["inputs"]["runner_cpus"]["default"], "2")
        self.assertEqual(javascript["on"]["workflow_dispatch"], "")
        shard = backend["jobs"]["backend-shard"]
        self.assertEqual(shard["strategy"], {"fail-fast": "false", "matrix": {"shard": ["1", "2", "3", "4"]}})
        self.assertEqual(backend["jobs"]["backend"]["name"], "Backend tests")
        self.assertEqual(backend["jobs"]["backend"]["if"], "always() && inputs.gated_by_caller != true")
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
        allowed = {"ubuntu-latest", "ubuntu-24.04", "ubuntu-24.04-arm", "windows-latest", "macos-latest", "macos-14"}
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
                    elif runner == "${{ matrix.runner }}":
                        matrix = job["strategy"]["matrix"]
                        self.assertEqual(set(matrix), {"include"},
                                         "matrix.runner support is include-only; axes create extra jobs")
                        entries = matrix["include"]
                        self.assertTrue(entries)
                        for entry in entries:
                            self.assertIn(entry["runner"], allowed)
                    else:
                        self.assertIn(runner, allowed)

    def test_runner_axis_cannot_hide_a_paid_job_behind_free_include_entries(self):
        config = workflow("runtime-overlay-acceptance.yml")
        config["jobs"]["acceptance"]["strategy"]["matrix"]["runner"] = ["macos-latest-xlarge"]
        original_workflow = workflow

        def mixed_matrix(name):
            return config if name == "runtime-overlay-acceptance.yml" else original_workflow(name)

        result = unittest.TestResult()
        guard = CostPolicyTests("test_public_app_workflows_use_standard_hosted_runners")
        with patch(f"{__name__}.workflow", side_effect=mixed_matrix):
            guard.run(result)
        self.assertEqual(result.testsRun, 1)
        self.assertFalse(result.errors)
        self.assertEqual(len(result.failures), 1, "Paid runner axis escaped the free-runner guard")

    def test_runtime_overlay_acceptance_is_path_scoped_and_non_publishing(self):
        config = workflow("runtime-overlay-acceptance.yml")
        self.assertEqual(config["on"]["pull_request"]["paths"], [
            ".github/workflows/runtime-overlay-acceptance.yml",
            "scripts/release/overlay_acceptance.py",
            "docker/update_contract.py",
        ])
        self.assertEqual(config["permissions"], {"contents": "read"})
        text = (WORKFLOWS / "runtime-overlay-acceptance.yml").read_text()
        self.assertNotIn("secrets.", text)
        self.assertNotIn("continue-on-error", text)
        job = config["jobs"]["acceptance"]
        self.assertEqual({entry["runner"] for entry in job["strategy"]["matrix"]["include"]},
                         {"ubuntu-24.04", "ubuntu-24.04-arm"})
        build = next(step for step in job["steps"]
                     if step.get("uses") == "docker/build-push-action@v6")
        self.assertEqual(build["with"]["push"], "false")

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
