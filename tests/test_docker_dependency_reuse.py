"""Signed-source dependency reuse: deterministic installs, not timing thresholds."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from docker import application_updates as updates
from tests.test_application_updates import B, C, feed, stub_venv  # reuse the real signed feed


@pytest.fixture
def build_feed(feed, monkeypatch, request):
    if sys.platform != "linux":
        pytest.skip("Docker dependency environments execute Linux bin/python venvs")
    updater, publish, *_ = feed
    monkeypatch.delenv("AI_SHUFFLE_PERF_DOCKER_DEPENDENCY_REUSE", raising=False)
    monkeypatch.delenv("SHUFFLE_HANDED_OVER", raising=False)
    monkeypatch.setattr(updater, "build", updates.ContainerUpdates.build.__get__(updater))
    commands = []

    site_relative = f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    identity = {"version": sys.version, "platform": sys.platform, "abi": "fixture", "site": site_relative}
    monkeypatch.setattr(updater, "dependency_identity",
                        lambda kind, *_: identity if kind == "python" else ["node", "platform", "arch", "abi", "npm"],
                        raising=False)

    def site_for(python):
        return Path(python).parent.parent / site_relative

    def run(command, cwd, env):
        commands.append((list(command), Path(cwd)))
        if command[1:3] == ["-m", "venv"]:
            if request.node.name.startswith(("test_signed_second_revision_reuses_all_third_party_dependencies", "test_corrupt_completed_cache", "test_semantically_broken_cache")):
                subprocess.run([*command, "--without-pip"], check=True, env=env,
                               capture_output=True, timeout=20)
            else:
                stub_venv(Path(command[-1]).parent, command[0])
        elif "--require-hashes" in command:
            site = site_for(command[0])
            site.mkdir(parents=True, exist_ok=True)
            (site / "third_party_fixture.py").write_text("VALUE = 'locked dependency'\n")
        elif "./fluid" in command:
            site = site_for(command[0]) / "fluid"
            site.mkdir(parents=True, exist_ok=True)
            (site / "__init__.py").write_text("")
            (site / "fixture_revision.py").write_text((cwd / "fluid/fixture_revision.py").read_text())
        elif command[:2] == ["npm", "ci"]:
            (cwd / "node_modules").mkdir(parents=True, exist_ok=True)
            (cwd / "node_modules/installed.txt").write_text("dependencies")
            package = cwd / "node_modules/dependency-fixture"
            package.mkdir()
            (package / "package.json").write_text('{"main":"index.js"}')
            (package / "index.js").write_text("module.exports = 'locked dependency';")
        elif command == ["npm", "run", "build"] and cwd.name == "frontend":
            (cwd / "build").mkdir(parents=True)
            (cwd / "build/index.html").write_text(env["GITHUB_SHA"])

    monkeypatch.setattr(updater, "run_build", run)

    def stage(revision, sequence, changes=None):
        files = {
            "requirements.lock": "# pinned core\n",
            "requirements-native.lock": "# pinned native\n",
            "requirements-voice.lock": "# pinned voice\n",
            "app/core/voice_dependencies.py": "# signed optional-dependency installer\n",
            "fluid/pyproject.toml": "[project]\nname='fluid'\nversion='0.1.0'\n",
            "fluid/fixture_revision.py": f"REVISION = {revision!r}\n",
        }
        for package in ("frontend", "tools", "browser-mcp"):
            files[f"{package}/package.json"] = json.dumps({"name": package, "private": True})
            files[f"{package}/package-lock.json"] = json.dumps({"lockfileVersion": 3, "name": package})
        files.update(changes or {})
        publish(revision=revision, sequence=sequence,
                entries={f"ai-shuffle-{revision}/{name}": content for name, content in files.items()})
        start = len(commands)
        updater.check(download=True)
        return commands[start:]

    return updater, stage, commands


def installs(commands):
    return [cmd for cmd, _ in commands if "--require-hashes" in cmd or "app/core/voice_dependencies.py" in cmd]


def npm_installs(commands):
    return [cwd.parent.parent.name if cwd.parent.parent.parent.name == "node" else cwd.name
            for cmd, cwd in commands if cmd[:2] == ["npm", "ci"]]


def test_signed_second_revision_reuses_all_third_party_dependencies(build_feed, tmp_path):
    updater, stage, _ = build_feed
    first = stage(B, 2)
    cached = next((updater.root / "dependencies/python").glob("*/*/.complete")).parent
    before_import = updater.dependency_contents(cached, "python")
    old_python = updater.directory(B) / "venv/bin/python"
    probe = "import sys; from fluid.fixture_revision import REVISION; import third_party_fixture; print(REVISION); print(sys.pycache_prefix)"
    imported = subprocess.check_output([str(old_python), "-I", "-c", probe], cwd=tmp_path, text=True, timeout=10)
    assert imported.splitlines() == [B, str(updater.directory(B) / "pycache")]
    assert updater.dependency_contents(cached, "python") == before_import
    updater.launch({}, activate=True)
    updater.activating()
    updater.healthy()
    second = stage(C, 3)
    assert installs(first) and len(npm_installs(first)) == 3
    assert installs(second) == [], "same signed closure must not reinstall third-party Python/voice dependencies"
    assert npm_installs(second) == [], "same Node/package inputs must not run npm ci"
    assert sum("./fluid" in cmd for cmd, _ in second) == 1
    python = updater.directory(C) / "venv/bin/python"
    result = subprocess.check_output(
        [str(python), "-I", "-c", "from fluid.fixture_revision import REVISION; import third_party_fixture; print(REVISION)"],
        cwd=tmp_path, text=True, timeout=10)
    assert result.strip() == C, "reusing deps must never execute the previous Fluid source"
    rollback = subprocess.check_output([str(old_python), "-I", "-c", probe], cwd=tmp_path, text=True, timeout=10)
    assert rollback.splitlines() == [B, str(updater.directory(B) / "pycache")]
    assert updater.dependency_contents(cached, "python") == before_import


@pytest.mark.parametrize("lock", ["requirements.lock", "requirements-native.lock", "requirements-voice.lock"])
def test_each_python_lock_invalidates_only_python(build_feed, lock):
    _, stage, _ = build_feed
    stage(B, 2)
    second = stage(C, 3, {lock: "# changed closure\n"})
    assert len(installs(second)) == 2
    assert npm_installs(second) == []


def test_one_node_lock_invalidates_only_that_package(build_feed):
    _, stage, _ = build_feed
    stage(B, 2)
    second = stage(C, 3, {"tools/package-lock.json": '{"lockfileVersion":3,"name":"tools","changed":true}'})
    assert installs(second) == []
    assert npm_installs(second) == ["tools"]


def test_kill_switch_restores_uncached_builds(build_feed, monkeypatch):
    _, stage, _ = build_feed
    stage(B, 2)
    monkeypatch.setenv("AI_SHUFFLE_PERF_DOCKER_DEPENDENCY_REUSE", "0")
    second = stage(C, 3)
    assert len(installs(second)) == 2
    assert len(npm_installs(second)) == 3



def test_optional_failure_never_publishes_a_reusable_python_cache(build_feed, monkeypatch):
    updater, stage, _ = build_feed
    real = updater.run_build
    fail = [True]

    def run(command, cwd, env):
        if fail[0] and "app/core/voice_dependencies.py" in command:
            raise updates.UpdateError("optional voice unavailable")
        real(command, cwd, env)

    monkeypatch.setattr(updater, "run_build", run)
    stage(B, 2)
    assert not list((updater.root / "dependencies/python").glob("*/*/.complete"))
    updater.launch({}, activate=True)
    updater.activating()
    updater.healthy()
    fail[0] = False
    second = stage(C, 3)
    assert len(installs(second)) == 2
    assert npm_installs(second) == []
    # The first revision's private, incomplete environment is still valid.
    assert len(list((updater.root / "dependencies/python").glob("*/*/venv/pyvenv.cfg"))) == 2


def test_interrupted_node_install_is_not_reused(build_feed, monkeypatch):
    updater, stage, _ = build_feed
    real = updater.run_build
    fail = [True]

    def run(command, cwd, env):
        real(command, cwd, env)
        if fail[0] and command[:2] == ["npm", "ci"]:
            raise updates.UpdateError("interrupted after writing node_modules")

    monkeypatch.setattr(updater, "run_build", run)
    with pytest.raises(updates.UpdateError, match="interrupted"):
        stage(B, 2)
    assert not list((updater.root / "dependencies/node").glob("*/*/*/node_modules"))
    fail[0] = False
    second = stage(C, 3)
    assert installs(second) == []
    assert npm_installs(second) == ["frontend", "tools", "browser-mcp"]


@pytest.mark.parametrize("group", ["python", "node/tools"])
@pytest.mark.parametrize("damage", ["missing", "same-size", "extra-bytecode", "symlink-loop"])
def test_corrupt_completed_cache_builds_a_new_entry_without_repairing_retained_code(build_feed, tmp_path, group, damage):
    updater, stage, _ = build_feed
    stage(B, 2)
    updater.launch({}, activate=True)
    updater.activating()
    updater.healthy()
    root = updater.root / "dependencies" / group
    old = next(root.glob("*/*/.complete")).parent
    marker = (old / ".complete").read_bytes()
    if group == "python":
        payload = next((old / "venv/lib").glob("python*/site-packages/third_party_fixture.py"))
    else:
        payload = old / "node_modules/dependency-fixture/index.js"
    original = payload.read_bytes()
    original_stat = payload.stat()
    if damage == "missing":
        payload.unlink()
    elif damage == "same-size":
        payload.write_bytes(b"!" * len(original))
        os.utime(payload, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        changed = payload.stat()
        assert (changed.st_size, changed.st_mtime_ns, changed.st_ino) == (
            original_stat.st_size, original_stat.st_mtime_ns, original_stat.st_ino)
    elif damage == "symlink-loop":
        payload.unlink()
        payload.symlink_to(payload.name)
    else:
        payload = payload.parent / "__pycache__/unexpected.pyc"
        payload.parent.mkdir(exist_ok=True)
        payload.write_bytes(b"damaged bytecode")
    second = stage(C, 3)
    if group == "python":
        assert len(installs(second)) == 2
        assert npm_installs(second) == []
    else:
        assert installs(second) == []
        assert npm_installs(second) == ["tools"]
    entries = {item.parent for item in root.glob("*/*/.complete")}
    assert len(entries) == 2 and old in entries, "a retained tree must never be repaired in place"
    assert (old / ".complete").read_bytes() == marker
    if damage == "missing":
        assert not payload.exists()
    elif damage == "symlink-loop":
        assert payload.is_symlink() and os.readlink(payload) == payload.name
    else:
        assert payload.read_bytes() != original
    if group != "python":
        fresh = next(iter(entries - {old})) / "node_modules/dependency-fixture/index.js"
        assert fresh.read_bytes() == original
    python = updater.directory(C) / "venv/bin/python"
    output = subprocess.check_output([str(python), "-I", "-c", "import third_party_fixture; print(third_party_fixture.VALUE)"],
                                     cwd=tmp_path, text=True, timeout=10)
    assert output.strip() == "locked dependency"


def test_semantically_broken_cache_is_rejected_by_the_import_probe(build_feed, monkeypatch, tmp_path):
    updater, stage, _ = build_feed
    monkeypatch.setattr(updates, "PYTHON_DEPENDENCY_PROBES", ("third_party_fixture",))
    stage(B, 2)
    updater.launch({}, activate=True)
    updater.activating()
    updater.healthy()
    old = next((updater.root / "dependencies/python").glob("*/*/.complete")).parent
    module = next((old / "venv/lib").glob("python*/site-packages/third_party_fixture.py"))
    module.write_text("raise RuntimeError('unusable installed package')\n")
    # A content-valid installation can still be semantically unusable.
    updates.atomic_json(old / ".complete", {"format": 2, "key": old.parent.name,
                                            "contents": updater.dependency_contents(old, "python")})
    second = stage(C, 3)
    assert len(installs(second)) == 2
    assert npm_installs(second) == []
    assert module.read_text().startswith("raise RuntimeError"), "never repair the retained tree"
    output = subprocess.check_output([str(updater.directory(C) / "venv/bin/python"), "-I", "-c",
                                      "import third_party_fixture; print(third_party_fixture.VALUE)"],
                                     cwd=tmp_path, text=True, timeout=10)
    assert output.strip() == "locked dependency"


@pytest.mark.parametrize("input_name", ["installer", "recipe", "pip", "ensurepip"])
def test_python_install_recipe_inputs_invalidate_only_python(build_feed, monkeypatch, input_name):
    updater, stage, _ = build_feed
    stage(B, 2)
    changes = None
    if input_name == "installer":
        changes = {"app/core/voice_dependencies.py": "# corrected signed installer\n"}
    elif input_name == "recipe":
        monkeypatch.setattr(updates, "PYTHON_DEPENDENCY_RECIPE", "next-recipe", raising=False)
    else:
        original_probe = updater.dependency_identity
        monkeypatch.setattr(updater, "dependency_identity", lambda kind, *args:
                            {**original_probe(kind, *args), input_name: "changed-version"} if kind == "python"
                            else original_probe(kind, *args))
    second = stage(C, 3, changes)
    assert len(installs(second)) == 2
    assert npm_installs(second) == []


def test_selected_python_probe_records_the_effective_seed_and_base_pip_versions(feed, tmp_path):
    import ensurepip
    import importlib.metadata
    identity = feed[0].dependency_identity("python", sys.executable, tmp_path, {})
    assert identity["ensurepip"] == ensurepip.version()
    assert identity["pip"] == importlib.metadata.version("pip")


def test_runtime_and_package_manifest_are_dependency_inputs(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies":{"example":"1"}}')
    (tmp_path / "package-lock.json").write_text("locked")
    files = ["package.json", "package-lock.json"]
    key = updates.ContainerUpdates.dependency_key
    baseline = key(tmp_path, files, ["node-version", "platform", "arch", "abi", "npm"])
    for index in range(5):
        identity = ["node-version", "platform", "arch", "abi", "npm"]
        identity[index] += "-changed"
        assert key(tmp_path, files, identity) != baseline
    (tmp_path / "package.json").write_text('{"dependencies":{"example":"2"}}')
    assert key(tmp_path, files, ["node-version", "platform", "arch", "abi", "npm"]) != baseline
    assert key(tmp_path, files, None) is None


def test_cache_requires_complete_marker_matching_key_and_payload(feed):
    updater = feed[0]
    key = "a" * 64
    first, complete = updater.cached_dependencies("node/tools", key)
    assert not complete
    (first / "node_modules").mkdir()
    second, complete = updater.cached_dependencies("node/tools", key)
    assert not complete and second != first
    updates.atomic_json(first / ".complete", "wrong-key")
    third, complete = updater.cached_dependencies("node/tools", key)
    assert not complete and third not in (first, second)
    updates.atomic_json(first / ".complete", key)
    assert updater.cached_dependencies("node/tools", key)[1] is False, "legacy existence-only markers are not proof"
    updater.complete_dependencies(first, "node/tools", key)
    broken_link = first.parent / "loop"
    broken_link.symlink_to(broken_link.name, target_is_directory=True)
    assert updater.cached_dependencies("node/tools", key) == (first, True)


def test_retention_protects_references_and_bounds_unused_complete_and_partial(feed):
    updater = feed[0]
    root = updater.root / "dependencies"
    entries = []
    for index in range(7):
        entry = root / "node/tools" / (str(index) * 64) / "build-fixture"
        entry.mkdir(parents=True)
        if index < 6:
            updates.atomic_json(entry / ".complete", entry.parent.name)
        entries.append(entry)
    version = updater.directory(B)
    version.mkdir(parents=True)
    updates.atomic_json(version / "dependency-cache.json", [str(entries[0].relative_to(root))])
    updater.prune_dependencies()
    assert entries[0].exists()
    assert sum(entry.exists() for entry in entries) == 3  # referenced + two warm
    assert not entries[-1].exists()
