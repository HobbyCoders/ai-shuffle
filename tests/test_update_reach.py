"""scripts/ci/update_reach.py: which changes the in-app update cannot deliver (no git calls)."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reach = _module("update_reach", "scripts/ci/update_reach.py")
contract = _module("update_contract", "docker/update_contract.py")


def tree(overrides=None):
    """This checkout's files as a revision, with some paths changed (None deletes)."""
    overrides = overrides or {}

    def read(path):
        if path in overrides:
            return overrides[path]
        file = ROOT / path
        return file.read_bytes() if file.is_file() else None
    return read


def groups_for(changes):
    head = {path: (data if data is None else data.encode()) for path, data in changes.items()}
    return reach.classify(changes, tree(), tree(head))


def test_fingerprint_matches_the_image_contract():
    # The image bakes update_contract.fingerprint into image.json; the check
    # must compute the same digest or it would miss the "image required" case.
    assert reach.contract_fingerprint(tree()) == contract.fingerprint(ROOT)
    assert reach.contract_files(tree()) == contract.FILES


def test_any_contract_file_edit_requires_a_new_image_even_a_comment():
    dockerfile = (ROOT / "Dockerfile").read_text() + "\n# comment only\n"
    groups = groups_for({"Dockerfile": dockerfile})
    assert groups["docker-image"] == ["Dockerfile"]
    assert reach.verdict(groups) == ["docker-image"]
    assert reach.verdict(groups, ["docker-image"]) == []


def test_chrome_launch_script_change_requires_a_new_image():
    # #1754 changed this file and the Unraid worker needed a new edge image.
    groups = groups_for({"docker/start-chrome.sh": "#!/bin/sh\n"})
    assert groups["docker-image"] == ["docker/start-chrome.sh"]


def test_application_code_reaches_docker_and_windows_installs():
    groups = groups_for({"app/main.py": "changed", "frontend/src/app.html": "changed",
                         "desktop/src/main/index.ts": "changed", "desktop/src/main/worker.ts": "changed"})
    assert reach.verdict(groups) == []
    # The full desktop app (index.ts and below) runs from the updated source.
    assert groups["windows-launcher"] == []
    # macOS and Linux have no in-place update at all, so it is still reported.
    assert groups["macos-linux-desktop"] == ["app/main.py", "desktop/src/main/index.ts",
                                             "desktop/src/main/worker.ts", "frontend/src/app.html"]


def test_installed_launcher_code_never_reaches_installed_windows_apps():
    groups = groups_for({"desktop/src/main/installed-host.ts": "changed",
                         "desktop/src/main/source-updates.ts": "changed"})
    assert groups["windows-launcher"] == ["desktop/src/main/installed-host.ts", "desktop/src/main/source-updates.ts"]
    assert reach.verdict(groups) == ["windows-launcher"]


def test_launcher_closure_follows_imports_and_requires_but_stops_at_the_full_app():
    files = reach.launcher_files(tree())
    assert {"desktop/src/main/managed-entry.ts", "desktop/src/main/installed-entry.ts",
            "desktop/src/main/installed-host.ts", "desktop/src/main/source-updates.ts",
            "desktop/scripts/source-profile.cjs"} <= files
    assert "desktop/src/main/index.ts" not in files
    assert "desktop/src/main/worker.ts" not in files


def test_type_only_imports_are_not_part_of_the_launcher():
    read = tree({
        "desktop/src/main/managed-entry.ts": b"import type { A } from './only-types';\nimport { b } from './runtime';\n",
        "desktop/src/main/only-types.ts": b"export type A = 1;\n",
        "desktop/src/main/runtime.ts": b"const c = require('./required');\n",
        "desktop/src/main/required.ts": b"export {};\n",
    })
    assert reach.launcher_files(read) == {"desktop/src/main/managed-entry.ts", "desktop/src/main/runtime.ts",
                                          "desktop/src/main/required.ts"}


def test_trust_key_change_fails_for_every_platform():
    groups = groups_for({reach.TRUST_KEY: "new key"})
    assert groups["update-trust-key"] == [reach.TRUST_KEY]
    assert groups["windows-packaging"] == []
    assert "update-trust-key" in reach.verdict(groups)


def test_image_only_copies_fail_but_owner_tooling_is_reported():
    groups = groups_for({"docker/install-claude-cli.sh": "x", "requirements-dev.txt": "x",
                         "scripts/credential_keys.py": "x", "fluid/tests/unit/test_x.py": "x"})
    assert groups["docker-image-copy"] == ["docker/install-claude-cli.sh", "requirements-dev.txt"]
    assert groups["image-tooling"] == ["scripts/credential_keys.py"]
    # fluid/ ships in the archive in part; its tests never run in the image.
    assert reach.verdict(groups) == ["docker-image-copy"]


def test_android_native_and_packaging_are_notes_only():
    groups = groups_for({"android/app/build.gradle.kts": "x", "android/hub/src/index.ts": "x",
                         "desktop/electron-builder.yml": "x"})
    assert groups["android-native"] == ["android/app/build.gradle.kts"]
    assert groups["windows-packaging"] == ["desktop/electron-builder.yml"]
    assert reach.verdict(groups) == []


def test_dockerfile_copy_parsing_skips_build_stages():
    dockerfile = ("FROM node AS build\nCOPY frontend/ ./\nFROM python\n"
                  "COPY --chown=a:a app/ ./app/\nCOPY --from=build /x /y\nCOPY a.sh b.sh ./docker/\n")
    assert reach.dockerfile_copies(dockerfile) == ["app", "a.sh", "b.sh"]


def test_archive_membership_honours_exclusions():
    archive = _module("source_archive", "scripts/source_archive.py")
    assert reach.in_archive("frontend/src/x.svelte", archive.RUNTIME_PATHS, archive.EXCLUDED)
    assert not reach.in_archive("frontend/tests/x.spec.ts", archive.RUNTIME_PATHS, archive.EXCLUDED)
    assert not reach.in_archive("frontend/src/x.test.ts", archive.RUNTIME_PATHS, archive.EXCLUDED)
    assert not reach.in_archive("docs/reviews/x.md", archive.RUNTIME_PATHS, archive.EXCLUDED)
