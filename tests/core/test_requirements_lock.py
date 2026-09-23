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


def locked():
    text = (ROOT / "requirements.lock").read_text()
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


def test_image_and_updater_install_only_from_the_lock():
    image = (ROOT / "Dockerfile").read_text()
    assert "pip install --no-cache-dir --require-hashes -r requirements.lock" in image
    assert "pip install --no-cache-dir --no-deps --no-build-isolation /opt/fluid" in image
    assert "-r requirements.txt" not in image
    updater = (ROOT / "docker/application_updates.py").read_text()
    assert '"--require-hashes", "-r", "requirements.lock"' in updater
    assert '"--no-deps", "--no-build-isolation", "./fluid"' in updater
    assert '"requirements.txt"' not in updater
