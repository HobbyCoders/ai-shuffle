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
        "Why AI Shuffle", "Install", "What you can do", "Fluid: our own agent harness",
        "Free and Cloud", "Contributing, security and license",
    ]
    assert text.index("[Watch the tour]") < text.index("## Why AI Shuffle")
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
        assert text.index("[Watch the tour]") < text.index(f"![{alt}]") < text.index("## Fluid")


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


# Hash the original values, not substitutes: regressions must still catch a
# real leak without publishing the sensitive strings in the guard itself.
OWNER_DETAIL_HASHES = frozenset({
    "8be0b5445fca6eb16d14c6f4eaff1cf1d1a3362ef660bfb2b66e74bf309b7e61",  # machine-one
    "6baff1894abb437cfa6086c13670ed2dd5ecfb8178a36f1b8b42fc7c6f588912",  # machine-two
    "8e08b724734fcbc0af03e2469bbe556b37c8330296e4a4a7736a0572bf39522e",  # private-address
    "167ed9eb3a83f4f32378c4b195f745ee5c16116f74d7cc8afe5a09a1d77f219f",  # tailnet-address
    "660bd4b3315cfd8a19cf6050f83741778bd1ee5b9fb25e9c7907c92cad322986",  # home-folder
    "4be591bebbdaca82fab8546a25738a9c9d4e3a6b02fe55bfe2d3636ef455da3b",  # tailnet-name-1
    "b3abbaeb6353ff23d26dbe1dd39f47933d15f686d8d82ca5fe84c9ef0b99220a",  # tailnet-name-2
    "c23fbc9537dd5bb88eef798b887c4948c17c349f729583146d9883fdfd21f634",  # research-artifact-1
    "87274b4f9f72acc805c05b3ced451499c0aa1d795c399112f2c0cbac55b43bb9",  # research-branch-1
    "a68c1f4657a0416721a8148a20443e0a70c383344b91bd4772a96b49d3d51f21",  # research-worktree-1
    "61e1affb401d8062e7732e625e1ca1c8c5937a383681b4ce3baa576bd0eac126",  # research-memory-path-1
    "ed79ee2278242f7851224d74a17df3b97d6dcab6e1db5bd87289a41ea8d800c6",  # research-artifact-2
    "95119a49a78b052307fa0511a5386deb6409bbcb4850571268243a1e298c00b9",  # research-checkout-1
    "9038006871154a43afa40dafaaac8edb4d0f919a2e4a6c57b88d0e2c7f8d4a10",  # research-branch-2
    "003dcea77b63d29547399dac003a401a27fe26235d87a6c405205b47016cc42e",  # research-worktree-2
    "83da9489f41a8387e4504781d8948da4356fc07b6eb9b931100192e882d877d3",  # credential-location-command-1
    "ecb1bdcd5b485a522660951ad0fee754e45566f74e996046bbff0ff8e3a0f199",  # research-checkout-2
    "6a5a14cb2b2845c24b94719b2b91b8940be56590c40a719bcdee59629f14603c",  # credential-location-command-2
})
OWNER_DETAIL_PATTERNS = (
    r"\b\w+\b",  # worker words, also inside URLs and hyphenated names
    r"\b(?:\d{1,3}\\?\.){3}\d{1,3}\b",  # IPv4, including regex literals
    r"\b(?=([\w-]+(?:\.[\w-]+)*\.ts\.net)\b)",  # overlapping hostname suffixes
    r"/home/[\w.-]+",  # the home directory prefix, including in a longer path
    r"(?:https?://|[A-Z]:[\\/]|~[/\\]|\.claude/)[^\s`<>\"()]+",  # URLs and local paths
    r"\b[\w.-]+(?:/[\w.-]+)+",  # branch and worktree path tokens
    r"`([^`\n]+)`",  # code-quoted setup commands, including embedded spaces
)


def _has_owner_detail(text):
    return any(
        hashlib.sha256(token.replace(r"\.", ".").rstrip(".,;:").casefold().encode()).hexdigest()
        in OWNER_DETAIL_HASHES
        for pattern in OWNER_DETAIL_PATTERNS
        for token in re.findall(pattern, text, re.I)
    )


def test_owner_setup_details_stay_out_of_tracked_files():
    hits = [
        path for path in _tracked_files()
        if _has_owner_detail((ROOT / path).read_bytes().decode("utf-8", errors="ignore"))
    ]
    # No exemption for this test file, and no private contents in failure output.
    assert hits == [], f"remove owner-specific details from tracked files: {hits}"


@pytest.mark.parametrize("detail, blocked", [
    pytest.param("ServerWorker", True, id="machine-one"),
    pytest.param("DesktopWorker", True, id="machine-two"),
    pytest.param("desktopworker", True, id="case-insensitive-worker"),
    pytest.param("https://ServerWorker.local:8000", True, id="worker-hostname"),
    pytest.param("ServerWorker-backup", True, id="hyphenated-worker"),
    pytest.param("192.0.2.10", True, id="private-address"),
    pytest.param("http://192.0.2.10:8000/path", True, id="address-in-url"),
    pytest.param(r"192\.0\.2\.10", True, id="address-in-regex"),
    pytest.param("example-tailnet.ts.net.attacker.example", True, id="spoofed-tailnet"),
    pytest.param("node.example-tailnet.ts.net", True, id="tailnet-subdomain"),
    pytest.param("/home/owner/Documents/project", True, id="home-directory"),
    pytest.param("192.0.2.11", False, id="documentation-address"),
    pytest.param("ExampleWorker", False, id="generic-worker"),
    pytest.param("NotServerWorker", False, id="worker-word-boundary"),
    pytest.param("/home/owner-example", False, id="different-home-directory"),
    pytest.param("other-tailnet.ts.net", False, id="different-tailnet"),
    pytest.param("https://example.com/artifact/research-review", True, id="research-artifact"),
    pytest.param("See https://example.com/artifact/research-review.", True, id="artifact-sentence"),
    pytest.param("[report](https://example.com/artifact/research-review)", True, id="artifact-link"),
    pytest.param("https://example.com/artifact/public-example", False, id="different-artifact"),
    pytest.param("review/research-harness", True, id="research-branch"),
    pytest.param(".claude/worktrees/research-review", True, id="research-worktree"),
    pytest.param(r"C:\Projects\harness", True, id="research-checkout"),
    pytest.param("~/.claude/projects/example/memory/review.md", True, id="research-memory-path"),
    pytest.param(
        '`[Environment]::GetEnvironmentVariable("EXAMPLE_API_KEY", "User")`',
        True, id="credential-location-command",
    ),
    pytest.param("review/public-example", False, id="different-branch"),
    pytest.param(r"C:\Projects\public-example", False, id="different-checkout"),
])
def test_owner_detail_guard_checks_actual_tracked_content(tmp_path, monkeypatch, detail, blocked):
    import shutil
    import subprocess
    if shutil.which("git") is None:
        pytest.skip("git unavailable")
    # Neutral fixtures exercise the same matcher. The original forbidden-value
    # digests above remain installed; the fixture digests only extend them.
    fixture_values = (
        "ServerWorker", "DesktopWorker", "192.0.2.10", "example-tailnet.ts.net", "/home/owner",
        "https://example.com/artifact/research-review", "review/research-harness",
        ".claude/worktrees/research-review", r"C:\Projects\harness",
        "~/.claude/projects/example/memory/review.md",
        '[Environment]::GetEnvironmentVariable("EXAMPLE_API_KEY", "User")',
    )
    monkeypatch.setitem(globals(), "OWNER_DETAIL_HASHES", OWNER_DETAIL_HASHES | {
        hashlib.sha256(value.casefold().encode()).hexdigest() for value in fixture_values
    })
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "fixture.txt").write_text(detail, encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "fixture.txt"], check=True, capture_output=True)
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    if blocked:
        with pytest.raises(AssertionError):
            test_owner_setup_details_stay_out_of_tracked_files()
    else:
        test_owner_setup_details_stay_out_of_tracked_files()
