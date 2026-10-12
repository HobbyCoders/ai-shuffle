"""Publication notices cover the checked-in dependency declarations."""
import json
import re
from pathlib import Path

from scripts import generate_third_party_notices as notices

ROOT = Path(__file__).resolve().parents[2]


def test_all_npm_lockfile_rows_are_noticed():
    text = (ROOT / "THIRD_PARTY_NOTICES.md").read_text()
    for project, include_dev in notices.NPM_PROJECTS:
        section = text.split(f"## npm packages: {project}/", 1)[1].split("\n## ", 1)[0]
        for row in notices.npm_packages(project, include_dev):
            assert notices._table([row]).splitlines()[-1] in section


def test_mobile_dependencies_use_the_frontend_lock():
    mobile = json.loads((ROOT / "mobile/package.json").read_text())
    frontend = json.loads((ROOT / "frontend/package-lock.json").read_text())["packages"]
    for name in {**mobile["dependencies"], **mobile["devDependencies"]}:
        assert f"node_modules/{name}" in frontend


def test_all_android_declarations_are_noticed():
    text = (ROOT / "THIRD_PARTY_NOTICES.md").read_text()
    gradle = (ROOT / "android/app/build.gradle.kts").read_text()
    declared = re.findall(r'(?:implementation|testImplementation|androidTestImplementation)\("([^":]+):([^":]+):([^":]+)"\)', gradle)
    rows = notices.android_packages()
    assert len(rows) == len(declared) > 0
    for row in rows:
        assert notices._table([row]).splitlines()[-1] in text


def test_python_locks_are_noticed_for_every_platform():
    text = (ROOT / "THIRD_PARTY_NOTICES.md").read_text().split("## Python packages", 1)[1].split("## npm", 1)[0]
    rows = {(notices.canonicalize_name(cells[1].strip()), cells[2].strip())
            for line in text.splitlines() if line.startswith("| ")
            for cells in [line.split("|")]}
    for filename in ("requirements.lock", "requirements-native.lock"):
        pins = re.findall(r"^([A-Za-z0-9_.-]+)==([^ ;\\\n]+)",
                          (ROOT / filename).read_text(), flags=re.MULTILINE)
        assert pins, filename
        for name, version in pins:
            assert (notices.canonicalize_name(name), version) in rows, (filename, name, version)

def test_generator_keeps_all_lock_versions_regardless_of_host_platform(tmp_path, monkeypatch):
    from email.message import Message
    from types import SimpleNamespace

    monkeypatch.setattr(notices, "ROOT", tmp_path)
    (tmp_path / "requirements.lock").write_text(
        'example==1.0 ; sys_platform == "win32" \\\n    --hash=sha256:abc\n'
    )
    (tmp_path / "requirements-native.lock").write_text('example==2.0 ; sys_platform == "darwin"\n')
    meta = Message()
    meta["Name"] = "example"
    meta["License-Expression"] = "MIT"
    meta["Home-page"] = "https://example.com"

    def distribution(name):
        if name != "example":
            raise notices.metadata.PackageNotFoundError(name)
        return SimpleNamespace(metadata=meta, version="99.0", requires=[])

    monkeypatch.setattr(notices.metadata, "distribution", distribution)
    rows, missing = notices.python_packages()
    assert [(row[0], row[1]) for row in rows] == [("example", "1.0"), ("example", "2.0")]
    assert missing == ["fluid"]
