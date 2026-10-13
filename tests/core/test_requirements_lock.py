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
    return [Requirement(line) for line in lines if line and not line.startswith("--")]


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


def test_native_linux_x64_replaces_evdev_sdist_with_hash_pinned_wheels():
    text = (ROOT / "requirements-native.lock").read_text()
    entries = locked("requirements-native.lock")
    assert "evdev==2.0.0 ; platform_machine != 'x86_64'" in text
    assert entries["evdev-binary"][0][0] == Version("2.0.0")
    assert "--hash=sha256:4721296f6439fd2a923f9e174c9a4dede44efee6aef3470775154d9cf7b54ba3" in text
    assert "platform_machine == 'x86_64' and sys_platform == 'linux'" in text
    script = (ROOT / "scripts/lock_requirements.sh").read_text()
    assert "--find-links desktop/vendor --only-binary pynput --only-binary evdev-binary" in script
    assert "requirements-native.lock" not in (ROOT / "Dockerfile").read_text()
    local = (ROOT / "run_local.py").read_text()
    assert '"--only-binary=pynput,evdev-binary"' in local
    assert '"--require-hashes", "-r", str(project_root / "requirements-native.lock")' in local


def test_vendored_pynput_hash_and_dependency_closure_are_consistent():
    import base64
    import csv
    import hashlib
    import io
    from email.parser import BytesParser
    import zipfile
    from packaging.markers import default_environment

    wheel = ROOT / "desktop/vendor/pynput-1.8.2+shuffle1-py2.py3-none-any.whl"
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert "--hash=sha256:" + digest in locked("requirements-native.lock")["pynput"][0][1]
    with zipfile.ZipFile(wheel) as archive:
        metadata = BytesParser().parsebytes(archive.read("pynput-1.8.2+shuffle1.dist-info/METADATA"))
        assert metadata["Version"] == "1.8.2+shuffle1"
        assert any(name.endswith("COPYING.LGPL") for name in archive.namelist())
        record = archive.read("pynput-1.8.2+shuffle1.dist-info/RECORD").decode()
        for name, checksum, size in csv.reader(io.StringIO(record)):
            if checksum:
                data = archive.read(name)
                actual = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                assert checksum == "sha256=" + actual and int(size) == len(data)
    requirements = [Requirement(line) for line in metadata.get_all("Requires-Dist")]
    heads = []
    for name in ("requirements.lock", "requirements-native.lock"):
        for line in (ROOT / name).read_text().splitlines():
            if line and line[0].isalnum():
                heads.append(Requirement(line.rstrip("\\").strip()))
    for platform, machine in [("linux", "x86_64"), ("linux", "aarch64"), ("win32", "AMD64"), ("darwin", "arm64")]:
        env = {**default_environment(), "sys_platform": platform, "platform_machine": machine,
               "python_version": "3.12", "python_full_version": "3.12.14", "extra": ""}
        pins = {canonicalize_name(r.name): next(iter(r.specifier)).version for r in heads if not r.marker or r.marker.evaluate(env)}
        for req in requirements:
            if not req.marker or req.marker.evaluate(env):
                assert canonicalize_name(req.name) in pins, f"{platform}/{machine}: missing {req}"
                assert req.specifier.contains(pins[canonicalize_name(req.name)]), f"{platform}/{machine}: incompatible {req}"
        if platform == "linux" and machine == "x86_64":
            assert "evdev" not in pins and "evdev-binary" in pins


def test_packages_in_both_locks_are_pinned_identically():
    # The native lock is compiled with -c requirements.lock; the desktop
    # installs them one after the other and they must never disagree.
    main, native = locked(), locked("requirements-native.lock")
    for name in main.keys() & native.keys():
        assert sorted(main[name]) == sorted(native[name]), f"{name} differs between the two locks"


def test_desktop_installs_only_from_the_locks():
    build = (ROOT / "desktop/src/main/source-build.ts").read_text()
    assert "'--require-hashes', '-r', 'requirements.lock'" in build
    assert "'--find-links', 'desktop/vendor', '--only-binary=pynput,evdev-binary', '--require-hashes', '-r', 'requirements-native.lock'" in build
    assert "'--no-deps', '--no-build-isolation', './fluid'" in build
    assert "'requirements.txt'" not in build and "'requirements-native.txt'" not in build
    runtime = (ROOT / "desktop/scripts/build-worker.mjs").read_text()
    assert "'--require-hashes', '-r', path.join(repoDir, 'requirements.lock')" in runtime
    assert "'--find-links', path.join(repoDir, 'desktop/vendor'), '--only-binary=pynput,evdev-binary', '--require-hashes', '-r', path.join(repoDir, 'requirements-native.lock')" in runtime
    assert "'--no-deps', '--no-build-isolation', path.join(repoDir, 'fluid')" in runtime
    # The bundled runtime's fingerprint and the source build's must hash the same inputs.
    contract = (ROOT / "desktop/scripts/runtime-contract.mjs").read_text()
    assert "['requirements.lock', 'requirements-native.lock', 'fluid/pyproject.toml']" in contract
    assert "['requirements.lock', 'requirements-native.lock', 'fluid/pyproject.toml']" in build
