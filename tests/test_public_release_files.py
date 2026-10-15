"""Public release metadata and contributor entry points stay consistent."""
import hashlib
import json
from pathlib import Path
import re
import tomllib

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("desktop", "frontend", "mobile", "browser-mcp", "tools")


def test_license_is_verbatim_gnu_agpl_3():
    # https://www.gnu.org/licenses/agpl-3.0.txt, retrieved 2026-10-11.
    # Normalize checkout line endings, not the license's text or whitespace.
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert hashlib.sha256(text.encode()).hexdigest() == (
        "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"
    )
    assert "Copyright (c) 2026 HobbyCoders." in (ROOT / "NOTICE").read_text()


@pytest.mark.parametrize("package", PACKAGES)
def test_published_package_license(package):
    manifest = json.loads((ROOT / package / "package.json").read_text())
    assert manifest["license"] == "AGPL-3.0-only"
    lock = ROOT / package / "package-lock.json"
    if lock.exists():
        assert json.loads(lock.read_text())["packages"][""]["license"] == manifest["license"]


def test_fluid_license():
    project = tomllib.loads((ROOT / "fluid/pyproject.toml").read_text())["project"]
    assert project["license"] == "AGPL-3.0-only"


def test_native_project_license_notices():
    android = (ROOT / "android/app/build.gradle.kts").read_text()
    assert android.startswith("// SPDX-License-Identifier: AGPL-3.0-only\n")
    assert 'extra["license"] = "AGPL-3.0-only"' in android
    ios = (ROOT / "ios/project.yml").read_text()
    assert ios.startswith("# SPDX-License-Identifier: AGPL-3.0-only\n")
    properties = yaml.safe_load(ios)["targets"]["AIShuffle"]["info"]["properties"]
    assert "AGPL-3.0-only" in properties["NSHumanReadableCopyright"]


def test_public_readme_order_and_install():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.findall(r"^## (.+)$", text, re.M) == [
        "What it is", "Fluid: our own agent harness", "What you can do with it", "Install",
        "Free and Cloud", "Contributing", "Security", "License", "Not affiliated",
    ]
    assert text.index("[Watch the tour]") < text.index("## What it is")
    block = re.search(r"```sh\n(.*?)```", text, re.S).group(1)
    commands = [line.strip() for line in block.splitlines() if line.strip()]
    assert len(commands) == 3
    assert commands[-1] == "docker compose up -d"
    assert "ghcr.io/hobbycoders/ai-shuffle-server" in text
    # Installers live on the site's downloads page, which serves the current
    # release; a GitHub "latest release" link is empty between releases.
    assert "https://hobbycoders.com/ai-shuffle/downloads" in text
    assert "releases/latest" not in text
    images = re.findall(r"!\[([^]]*)\]\(([^)]+)\)", text)
    assert images == [
        ("Chat transcript with a folded Worked for work log", "docs/images/readme/folded-work-log.png"),
        ("Phone worker list in the mobile app", "docs/images/readme/phone-workers.png"),
        ("Automations page with scheduled agent tasks", "docs/images/readme/automations.png"),
    ]
    for alt, path in images:
        assert "placeholder" not in f"{alt} {path}".lower(), "No placeholder screenshots"
        image = ROOT / path
        assert image.is_file(), path
        assert image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"), path
        assert image.stat().st_size < 500_000, path
        assert text.index("[Watch the tour]") < text.index(f"![{alt}]") < text.index("## Install")


@pytest.mark.parametrize("name", [
    "README.md", "CONTRIBUTING.md", "SECURITY.md", "SUPPORT.md", "TRADEMARKS.md",
])
def test_public_relative_links_exist(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    for link in re.findall(r"\[[^]]*\]\(([^)]+)\)", text):
        if "://" not in link and not link.startswith(("mailto:", "#")):
            assert (ROOT / link.split("#", 1)[0]).is_file(), (name, link)


def test_bug_report_requests_support_id_and_channel():
    template = yaml.safe_load((ROOT / ".github/ISSUE_TEMPLATE/bug_report.yml").read_text())
    fields = {field.get("id"): field for field in template["body"]}
    assert fields["support-report"]["validations"]["required"]
    assert fields["version"]["validations"]["required"]
    assert "channel" in fields["version"]["attributes"]["label"].lower()


def test_pull_request_template_requires_dco_and_local_checks():
    text = (ROOT / ".github/PULL_REQUEST_TEMPLATE.md").read_text()
    for required in ("Signed-off-by:", "git commit -s", "draft", "python scripts/local_ci.py"):
        assert required in text


# Per-checkout agent files and team working notes stay out of the public tree.
PRIVATE_PATH = re.compile(
    r"^(\.fluid/|AGENTS\.md$|CLAUDE\.md$|AGENTS\.local\.md$|CLAUDE\.local\.md$"
    r"|docs/(audits|design|mockups|perf|settings-redesign)/"
    r"|docs/internal/(AUDIT_|HANDOFF_|PERFORMANCE_|backlog-triage|repair-lifecycle))"
)


def _tracked_files():
    import shutil
    import subprocess
    if not (ROOT / ".git").exists() or shutil.which("git") is None:
        pytest.skip("not a git checkout")
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True)
    return [p for p in out.stdout.decode("utf-8").split("\0") if p]


def test_private_agent_files_and_working_notes_are_not_tracked():
    tracked = [p for p in _tracked_files() if PRIVATE_PATH.match(p)]
    assert tracked == [], f"keep these out of the public repo: {tracked}"


def test_owner_machine_names_stay_out_of_tracked_files():
    import subprocess
    _tracked_files()
    hits = subprocess.run(
        ["git", "-C", str(ROOT), "grep", "-lE", r"\b(HomeLab|GamingPC)\b", "--", "."],
        capture_output=True, text=True,
    ).stdout.split()
    assert [h for h in hits if h != "tests/test_public_release_files.py"] == []
