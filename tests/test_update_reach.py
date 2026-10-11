"""scripts/ci/update_reach.py: which changes the in-app update cannot deliver (no git calls)."""
import hashlib
import importlib.util
from pathlib import Path
import re

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


def contract_with(**names):
    """docker/update_contract.py with some declarations replaced (Python literals)."""
    text = (ROOT / "docker/update_contract.py").read_text()
    for name, value in names.items():
        assert re.search(rf"(?m)^{name} = ", text), name
        text += f"\n{name} = {value!r}\n"  # module globals: the last assignment wins
    return text


def test_published_runtime_matches_the_image_contract():
    # Images compare this string with the one they carry; the check must
    # compute what publish-source.mjs signs (update_contract --manifest).
    assert reach.published_runtime(tree()) == contract.manifest_fields()["runtime"] == contract.LEGACY_RUNTIME


def test_published_runtime_of_a_revision_before_the_capability_contract():
    # Before #1821 the runtime was a hash of whole files (FILES).
    legacy = b'FILES = ("a.txt", "b.txt")\n'
    read = tree({reach.CONTRACT: legacy, "a.txt": b"one", "b.txt": b"two"})
    expected = hashlib.sha256(b"a.txt\0one\0b.txt\0two\0").hexdigest()
    assert reach.published_runtime(read) == expected
    # A file missing at that revision leaves it unknown rather than guessed.
    assert reach.published_runtime(tree({reach.CONTRACT: legacy, "a.txt": None})) is None


def test_comment_only_edits_to_formerly_fingerprinted_files_pass():
    # Any byte of these eight files used to force a new image (#1754, #1865).
    changes = {name: (ROOT / name).read_text() + "\n# comment only\n"
               for name in ("Dockerfile", "entrypoint.sh", "docker/container_runtime.py",
                            "docker/application_updates.py", "docker/start-chrome.sh",
                            "docker/chrome-supervisor.sh", "docker/chrome-doctor.sh")}
    groups = groups_for(changes)
    assert groups["docker-image"] == [] and groups["image-pin"] == []
    assert reach.verdict(groups) == []


def test_supervisor_and_chrome_script_changes_are_reported_not_failed():
    # #1754 changed start-chrome.sh and the Unraid worker needed a new image;
    # now the updated copy runs on images with the hand-over.
    groups = groups_for({"docker/start-chrome.sh": "#!/bin/sh\nexit 0\n", "docker/application_updates.py": "x = 1\n"})
    assert groups["image-supervisor"] == ["docker/application_updates.py", "docker/start-chrome.sh"]
    assert groups["docker-image"] == [] and groups["docker-image-copy"] == []
    assert groups["macos-linux-desktop"] == []
    assert reach.verdict(groups) == []


def test_requiring_a_new_capability_fails_until_the_owner_accepts_it():
    text = contract_with(CAPABILITIES={**contract.CAPABILITIES, "python-3.13": "Python 3.13"},
                         PROVIDES=(*contract.PROVIDES, "python-3.13"), REQUIRES=("base-1", "python-3.13"))
    groups = groups_for({reach.CONTRACT: text})
    assert groups["docker-image"] == [reach.CONTRACT]
    assert reach.verdict(groups) == ["docker-image"]
    assert reach.verdict(groups, reach.allowed_by_labels("bug, update-reach:docker-image")) == []
    message = reach.render("base", "head", groups, ["docker-image"], [])
    assert "Needs a newer Docker image" in message and "update-reach:docker-image" in message


def test_recommending_a_capability_does_not_fail():
    text = contract_with(CAPABILITIES={**contract.CAPABILITIES, "python-3.13": "Python 3.13"},
                         PROVIDES=(*contract.PROVIDES, "python-3.13"), RECOMMENDS=("python-3.13",))
    assert reach.verdict(groups_for({reach.CONTRACT: text})) == []


def test_unpinned_image_input_change_fails_with_what_to_do():
    dockerfile = (ROOT / "Dockerfile").read_text() + "\nRUN true\n"
    groups = groups_for({"Dockerfile": dockerfile})
    assert groups["image-pin"] == ["Dockerfile"]
    assert reach.verdict(groups) == ["image-pin"]
    assert "python docker/update_contract.py --pin" in reach.render("b", "h", groups, ["image-pin"], [])


def test_pinned_image_input_change_is_reported_only():
    dockerfile = (ROOT / "Dockerfile").read_text() + "\nRUN true\n"
    head = tree({"Dockerfile": dockerfile.encode()})
    pin = contract.image_inputs_digest(head)
    text = contract_with(IMAGE_INPUTS_PIN=pin)
    groups = groups_for({"Dockerfile": dockerfile, reach.CONTRACT: text})
    assert groups["image-pin"] == [] and groups["image-inputs"] == ["Dockerfile"]
    assert groups["docker-image"] == []
    assert reach.verdict(groups) == []


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
    groups = groups_for({"requirements-dev.txt": "x", "scripts/credential_keys.py": "x",
                         "fluid/tests/unit/test_x.py": "x"})
    assert groups["docker-image-copy"] == ["requirements-dev.txt"]
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


def test_draft_note_keeps_groups_and_owner_labels_visible():
    groups = groups_for({reach.LAUNCHER_ENTRY: "export const changed = true;"})
    failing = reach.verdict(groups)
    assert failing == ["windows-launcher"]
    note = reach.render("base", "head", groups, failing, [], draft=True)
    assert "NOTE — OUT OF REACH: windows-launcher" in note
    assert "update-reach:windows-launcher" in note
    assert "DRAFT PR only" in note
    assert "FAILED:" not in note
    # Advisory reporting never changes the strict verdict used for publication.
    assert reach.verdict(groups) == ["windows-launcher"]
    assert "FAILED:" in reach.render("base", "head", groups, failing, [])
