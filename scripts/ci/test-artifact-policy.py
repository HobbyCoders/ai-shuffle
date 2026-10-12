"""Storage contracts for workflow artifacts; no GitHub credentials required."""
import itertools
import json
from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
PLAY_RETENTION = "${{ inputs.publish_to_play && 1 || 7 }}"

# Execute the actual inline cleanup code against an in-memory GitHub API.
CLEANUP_RUNNER = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {script, names, mode} = JSON.parse(fs.readFileSync(0, 'utf8'));
let artifacts = [
  ...names.map((name, i) => ({name, id: i + 1})),
  {name: 'installed-desktop-evidence', id: 90},
  {name: 'container-postgres-other', id: 91},
  {name: names[0] + '-other', id: 92},
];
if (mode === 'empty') artifacts = artifacts.filter(a => a.id >= 90);
const originalIds = artifacts.map(a => a.id);
const deleted = [];
let listings = 0;
const listWorkflowRunArtifacts = () => { throw new Error('Use pagination'); };
const github = {
  paginate: async (method, params) => {
    assert.equal(method, listWorkflowRunArtifacts);
    assert.deepEqual(params, {owner: 'owner', repo: 'repo', run_id: 123, per_page: 100});
    listings++;
    return [...artifacts];
  },
  rest: {actions: {
    listWorkflowRunArtifacts,
    deleteArtifact: async params => {
      assert.deepEqual(Object.keys(params).sort(), ['artifact_id', 'owner', 'repo']);
      assert.equal(params.owner, 'owner');
      assert.equal(params.repo, 'repo');
      assert.ok(params.artifact_id < 90, 'Never delete evidence or near-matching names');
      if (mode === 'denied') throw new Error('forbidden');
      deleted.push(params.artifact_id);
      if (mode !== 'stale') artifacts = artifacts.filter(a => a.id !== params.artifact_id);
    },
  }},
};
(async () => {
  const run = new (Object.getPrototypeOf(async function() {}).constructor)('github', 'context', 'core', script);
  const execute = () => run(github, {repo: {owner: 'owner', repo: 'repo'}, runId: 123}, {info() {}});
  if (mode === 'denied') {
    await assert.rejects(execute, /forbidden/);
    assert.deepEqual(artifacts.map(a => a.id), originalIds);
  } else if (mode === 'stale') {
    await assert.rejects(execute, /not removed/);
  } else {
    await execute();
    assert.equal(listings, 2, 'Read back after deletion');
    assert.deepEqual(deleted, mode === 'empty' ? [] : names.map((_, i) => i + 1));
    assert.deepEqual(artifacts.map(a => a.id), [90, 91, 92]);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


class UniqueKeyLoader(yaml.BaseLoader):
    """Keep Actions scalar spellings, but reject keys GitHub would reject."""

    def construct_mapping(self, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in mapping:
                raise yaml.constructor.ConstructorError(
                    "while constructing a workflow mapping", node.start_mark,
                    f"duplicate mapping key {key!r}", key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def workflow(name):
    with (WORKFLOWS / name).open(encoding="utf-8") as source:
        return yaml.load(source, Loader=UniqueKeyLoader)


def uploads(job):
    return [s for s in job.get("steps", []) if s.get("uses", "").startswith("actions/upload-artifact@")]


class ArtifactPolicyTests(unittest.TestCase):
    def test_all_workflow_mappings_have_unique_keys(self):
        paths = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
        self.assertGreater(len(paths), 0)
        for path in paths:
            with self.subTest(file=path.name):
                workflow(path.name)

    def test_duplicate_keys_are_rejected_at_each_mapping_depth(self):
        cases = [
            ("on: push\non: pull_request\n", "on"),
            ("jobs:\n  build: {}\n  build: {}\n", "build"),
            ("jobs:\n  verify:\n    env:\n      EXPECT_SIGNED: true\n"
             "    runs-on: windows-latest\n    env:\n      RELEASE_TAG: v1.2.3\n", "env"),
            ("jobs:\n  verify:\n    steps:\n      - env:\n          TOKEN: first\n"
             "          TOKEN: second\n        run: check\n", "TOKEN"),
        ]
        for source, key in cases:
            with self.subTest(key=key):
                with self.assertRaises(yaml.constructor.ConstructorError) as failure:
                    yaml.load(source, Loader=UniqueKeyLoader)
                self.assertEqual(failure.exception.problem, f"duplicate mapping key {key!r}")
                self.assertIsNotNone(failure.exception.problem_mark)
                self.assertIn("line", str(failure.exception))

    def test_keys_can_repeat_in_different_jobs_and_actions_on_stays_a_string(self):
        source = ("on: push\njobs:\n  first:\n    env:\n      NAME: first\n"
                  "  second:\n    env:\n      NAME: second\n")
        parsed = yaml.load(source, Loader=UniqueKeyLoader)
        self.assertEqual(parsed["on"], "push")
        self.assertEqual(parsed["jobs"]["first"]["env"]["NAME"], "first")
        self.assertEqual(parsed["jobs"]["second"]["env"]["NAME"], "second")

    def test_windows_verifier_keeps_both_release_and_signing_inputs(self):
        verify = workflow("desktop-installer.yml")["jobs"]["verify"]
        self.assertEqual(verify["env"], {
            "EXPECT_SIGNED": "${{ needs.build.outputs.signed }}",
            "RELEASE_TAG": "${{ inputs.release_tag }}",
        })

    def test_every_upload_has_a_short_explicit_retention(self):
        count = 0
        for path in WORKFLOWS.glob("*.yml"):
            for job in workflow(path.name)["jobs"].values():
                for step in uploads(job):
                    count += 1
                    with self.subTest(file=path.name, name=step["with"]["name"]):
                        days = step["with"].get("retention-days", "")
                        if path.name == "android-play.yml":
                            self.assertEqual(days, PLAY_RETENTION)
                        else:
                            self.assertTrue(days.isdigit(), "Missing explicit retention-days")
                            self.assertIn(int(days), range(1, 8))
        self.assertGreater(count, 10)

    def test_android_stages_only_the_emulator_and_download_apks(self):
        jobs = workflow("android.yml")["jobs"]
        build = jobs["build"]
        exported = {s["with"]["name"]: s for s in uploads(build)}
        self.assertEqual(set(exported), {"android-instrumentation-apks", "android-download"})
        self.assertEqual(exported["android-instrumentation-apks"]["with"]["retention-days"], "1")
        self.assertNotIn("if", exported["android-instrumentation-apks"])
        for name in ["Decode release keystore", "Build release APK", "Verify APK is signed with the release key", "Prepare public APK download", "Upload public download files"]:
            step = next(s for s in build["steps"] if s.get("name") == name)
            self.assertEqual(step["if"], jobs["publish"]["if"])
        self.assertEqual(exported["android-download"]["with"]["retention-days"], "1")
        self.assertEqual(jobs["android-ui"]["needs"], "build")
        self.assertEqual(len(jobs["android-ui"]["strategy"]["matrix"]["include"]), 2)
        self.assertEqual(jobs["publish"]["needs"], ["account-hub", "build", "android-ui"])

    def test_manual_only_binary_upload_gates_execute(self):
        android = workflow("android.yml")["jobs"]["publish"]["if"]
        desktop = workflow("desktop.yml")
        preview = next(s for j in desktop["jobs"].values() for s in uploads(j) if s["with"]["name"] == "windows-preview-installer")
        self.assertEqual(desktop["on"]["workflow_dispatch"]["inputs"]["upload_preview"]["default"], "false")
        cases = []
        for event, ref, enabled, tag, owner, same_rerun_actor in itertools.product(
            ["pull_request", "workflow_dispatch", "push"],
            ["refs/heads/main", "refs/heads/feature", "refs/tags/v1.2.3"],
            [False, True], ["", "v1.2.3"], [False, True], [False, True],
        ):
            inputs = {"publish_download": enabled, "upload_preview": enabled, "release_tag": tag}
            github = {"event_name": event, "ref": ref, "repository": "HobbyCoders/ai-shuffle",
                      "actor_id": "76577322" if owner else "7", "actor": "example-maintainer",
                      "triggering_actor": "example-maintainer" if same_rerun_actor else "another-maintainer"}
            permitted_ref = ref == "refs/heads/main" or ref.startswith("refs/tags/v")
            permitted_event = ((event == "workflow_dispatch" and permitted_ref)
                               or (event == "push" and ref.startswith("refs/tags/v")))
            android_allowed = (enabled and owner and same_rerun_actor and permitted_event
                               and (bool(tag) or ref == "refs/heads/main"))
            cases.append({"inputs": inputs, "github": github, "android": android_allowed,
                          "desktop": event == "workflow_dispatch" and enabled})
        runner = "const {android,desktop,cases}=JSON.parse(require('fs').readFileSync(0,'utf8')); for(const c of cases) for(const [key,expr] of Object.entries({android,desktop})) require('assert').strictEqual(new Function('inputs','github','startsWith','return '+expr)(c.inputs,c.github,(s,p)=>s.startsWith(p)), c[key]);"
        subprocess.run(["node", "-e", runner], input=json.dumps({"android": android, "desktop": preview["if"], "cases": cases}), text=True, check=True)
        self.assertEqual(preview["with"]["retention-days"], "1")
        steps = next(j["steps"] for j in desktop["jobs"].values() if preview in j.get("steps", []))
        verify = next(s for s in steps if s.get("name") == "Install and exercise actual Windows app")
        self.assertNotIn("if", verify)
        self.assertIn("verify-windows-preview.mjs", verify["run"])

    def test_release_staging_cleanup_runs_only_after_success(self):
        release = workflow("release.yml")
        for file, caller, names in [("android-play.yml", "android", ["ai-shuffle-play-bundle"]), ("desktop-installer.yml", "desktop", ["windows-installer-candidate", "windows-stable-installer"])]:
            publish = workflow(file)["jobs"]["publish"]
            cleanup = publish["steps"][-1]
            self.assertEqual(cleanup["name"], "Remove published build staging artifacts")
            self.assertNotIn("if", cleanup, "Use the implicit success() gate, never always()")
            self.assertEqual(cleanup["continue-on-error"], "true")
            self.assertEqual(publish["permissions"]["actions"], "write")
            self.assertEqual(release["jobs"][caller]["permissions"]["actions"], "write")
            for mode in ("success", "empty", "denied", "stale"):
                with self.subTest(workflow=file, mode=mode):
                    subprocess.run(["node", "-e", CLEANUP_RUNNER], input=json.dumps({"script": cleanup["with"]["script"], "names": names, "mode": mode}), text=True, check=True)

    def test_ci_diagnostics_upload_only_on_failure(self):
        diagnostics = {
            ("android.yml", "android-ui"): "android-ui-verification-api-${{ matrix.api }}",
            ("ci-browser-workspace.yml", "workspace"): "browser-workspace-screenshots",
            ("container-postgres.yml", "container-postgres"): "container-postgres-${{ github.sha }}",
            ("build-branch.yml", "build"): "container-postgres-${{ github.sha }}",
        }
        for (file, job), name in diagnostics.items():
            with self.subTest(file=file):
                step = next(s for s in uploads(workflow(file)["jobs"][job]) if s["with"]["name"] == name)
                self.assertEqual(step["if"], "failure()")
                self.assertEqual(step["with"]["retention-days"], "3")
        # Release evidence is kept whatever the outcome; this policy must not touch it.
        release = uploads(workflow("docker-publish.yml")["jobs"]["build"])[0]
        self.assertTrue(release["if"].startswith("always() && "))
        installer = next(s for s in uploads(workflow("desktop-installer.yml")["jobs"]["verify"]) if s["with"]["name"] == "installed-desktop-evidence")
        self.assertEqual(installer["if"], "always()")

    def test_manual_play_bundle_and_failed_installer_remain_recoverable(self):
        play = workflow("android-play.yml")
        self.assertEqual(uploads(play["jobs"]["bundle"])[0]["with"]["retention-days"], PLAY_RETENTION)
        self.assertIn("inputs.publish_to_play", play["jobs"]["publish"]["if"])
        installer = workflow("desktop-installer.yml")
        for job in ("build", "verify"):
            binary = uploads(installer["jobs"][job])[0]
            self.assertEqual(binary["with"]["retention-days"], "1")
        download = next(s for s in installer["jobs"]["verify"]["steps"] if s.get("uses", "").startswith("actions/download-artifact@"))
        self.assertEqual(download["with"]["run-id"], "${{ inputs.candidate_run_id || github.run_id }}")

    def test_platform_installer_publishes_the_tagged_android_download(self):
        config = workflow("platform-installer.yml")
        self.assertEqual(config["on"]["workflow_dispatch"]["inputs"]["platform"]["options"], ["mac", "linux", "android"])
        build = config["jobs"]["android-build"]
        # The APK comes from Android CI's own release path, built for this tag.
        self.assertEqual(build["uses"], "./.github/workflows/android.yml")
        self.assertEqual(build["with"], {"publish_download": "true", "release_tag": "${{ github.ref_name }}"})
        self.assertIn("inputs.platform == 'android'", build["if"])
        self.assertIn("github.ref_type == 'tag'", build["if"])
        publish = config["jobs"]["publish"]
        self.assertEqual(publish["needs"], "android-build")
        self.assertEqual(publish["environment"], "release-signing")
        self.assertIn("needs.android-build.result == 'success'", publish["if"])
        self.assertNotIn("always()", publish["if"])
        download = next(s for s in publish["steps"] if s.get("uses", "").startswith("actions/download-artifact@"))
        self.assertEqual(download["with"]["run-id"], "${{ inputs.build_run_id || github.run_id }}")
        # android.yml uploads exactly this artifact name; the publisher reads its files.
        uploaded = {s["with"]["name"] for s in uploads(workflow("android.yml")["jobs"]["build"])}
        self.assertIn("android-download", uploaded)
        run = "\n".join(s.get("run", "") for s in publish["steps"])
        self.assertIn("name=android-download", run)
        self.assertIn("sha256sum --check AI-Shuffle-Android.apk.sha256", run)
        self.assertIn('publish-platform-installer.mjs "$PLATFORM" artifact "$RELEASE_TAG"', run)
        self.assertFalse((WORKFLOWS / "desktop-platform-installer.yml").exists())


if __name__ == "__main__":
    unittest.main()
