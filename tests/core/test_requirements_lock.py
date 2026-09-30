"""requirements.lock must stay the complete, hash-pinned closure of the inputs."""
from pathlib import Path
import re
import tomllib

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]


def direct_requirements():
    lines = []
    for name in ("requirements.txt", "requirements-build.txt"):
        for line in (ROOT / name).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                lines.append(line)
    fluid = tomllib.loads((ROOT / "fluid/pyproject.toml").read_text())
    lines += fluid["project"]["dependencies"]
    return [Requirement(line) for line in lines]


def locked(name="requirements.lock"):
    text = (ROOT / name).read_text()
    entries = {}
    for block in re.split(r"\n(?=[A-Za-z0-9])", text):
        head = block.split("\\", 1)[0].strip()
        match = re.match(r"([A-Za-z0-9_.\[\],-]+)==([^\s;\\]+)", head)
        if not match:
            continue
        name = canonicalize_name(match.group(1).split("[", 1)[0])
        hashes = re.findall(r"--hash=sha256:[0-9a-f]{64}", block)
        entries.setdefault(name, []).append((Version(match.group(2)), hashes))
    return entries


def test_every_locked_package_is_pinned_with_hashes():
    entries = locked()
    assert len(entries) > 50
    for name, versions in entries.items():
        for _version, hashes in versions:
            assert hashes, f"{name} has no --hash in requirements.lock"


def test_lock_covers_every_direct_requirement_within_its_range():
    entries = locked()
    for requirement in direct_requirements():
        name = canonicalize_name(requirement.name)
        assert name in entries, f"{name} is missing from requirements.lock; run scripts/lock_requirements.sh"
        assert any(requirement.specifier.contains(version, prereleases=True) for version, _ in entries[name]), (
            f"requirements.lock pins {name} outside {requirement.specifier}; run scripts/lock_requirements.sh")


def test_headless_image_conditioning_dependency_is_in_main_lock():
    # Docker and the container updater install Fluid with --no-deps after ONLY
    # the main lock. A desktop/dev-only Pillow silently disables conditioning.
    fluid = tomllib.loads((ROOT / "fluid/pyproject.toml").read_text())
    required = [Requirement(dep) for dep in fluid["project"]["dependencies"]]
    [pillow] = [dep for dep in required if canonicalize_name(dep.name) == "pillow"]
    main = locked()
    assert "pillow" in main, "Headless workers must install Pillow from requirements.lock"
    assert all(pillow.specifier.contains(version) and hashes for version, hashes in main["pillow"])
    assert main["pillow"] == locked("requirements-native.lock")["pillow"]


def test_image_and_updater_install_only_from_the_lock():
    image = (ROOT / "Dockerfile").read_text()
    assert "pip install --no-cache-dir --require-hashes -r requirements.lock" in image
    assert "pip install --no-cache-dir --no-deps --no-build-isolation /opt/fluid" in image
    assert "-r requirements.txt" not in image
    updater = (ROOT / "docker/application_updates.py").read_text()
    assert '"--require-hashes", "-r", "requirements.lock"' in updater
    assert '"--no-deps", "--no-build-isolation", "./fluid"' in updater
    assert '"requirements.txt"' not in updater


def native_requirements():
    lines = [line.split("#", 1)[0].strip() for line in (ROOT / "requirements-native.txt").read_text().splitlines()]
    return [Requirement(line) for line in lines if line]


def test_native_lock_is_hash_pinned_and_covers_requirements_native():
    entries = locked("requirements-native.lock")
    assert entries
    for name, versions in entries.items():
        for _version, hashes in versions:
            assert hashes, f"{name} has no --hash in requirements-native.lock"
    for requirement in native_requirements():
        name = canonicalize_name(requirement.name)
        assert name in entries, f"{name} is missing from requirements-native.lock; run scripts/lock_requirements.sh"
        assert any(requirement.specifier.contains(version, prereleases=True) for version, _ in entries[name]), (
            f"requirements-native.lock pins {name} outside {requirement.specifier}; run scripts/lock_requirements.sh")


def test_packages_in_both_locks_are_pinned_identically():
    # The native lock is compiled with -c requirements.lock; the desktop
    # installs them one after the other and they must never disagree.
    main, native = locked(), locked("requirements-native.lock")
    for name in main.keys() & native.keys():
        assert sorted(main[name]) == sorted(native[name]), f"{name} differs between the two locks"


def test_desktop_installs_only_from_the_locks():
    build = (ROOT / "desktop/src/main/source-build.ts").read_text()
    assert "'--require-hashes', '-r', 'requirements.lock'" in build
    assert "'--require-hashes', '-r', 'requirements-native.lock'" in build
    assert "'--no-deps', '--no-build-isolation', './fluid'" in build
    assert "'requirements.txt'" not in build and "'requirements-native.txt'" not in build
    runtime = (ROOT / "desktop/scripts/build-worker.mjs").read_text()
    assert "'--require-hashes', '-r', path.join(repoDir, 'requirements.lock')" in runtime
    assert "'--require-hashes', '-r', path.join(repoDir, 'requirements-native.lock')" in runtime
    assert "'--no-deps', '--no-build-isolation', path.join(repoDir, 'fluid')" in runtime
    # The bundled runtime's fingerprint and the source build's must hash the same inputs.
    contract = (ROOT / "desktop/scripts/runtime-contract.mjs").read_text()
    assert "['requirements.lock', 'requirements-native.lock', 'fluid/pyproject.toml']" in contract
    assert "['requirements.lock', 'requirements-native.lock', 'fluid/pyproject.toml']" in build
