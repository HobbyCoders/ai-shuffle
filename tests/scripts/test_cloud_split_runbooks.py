"""Operational instructions must follow the private service relocation."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLOUD = "https://github.com/HobbyCoders/hobbycoders-cloud/blob/main/"


def test_private_runbooks_are_one_line_relocation_pointers():
    for name in ("docs/network/hub-legacy-purge.md", "docs/release/billing-and-accounts.md"):
        text = (ROOT / name).read_text()
        assert len(text.splitlines()) == 1
        assert CLOUD + name in text


def test_current_operational_docs_do_not_reference_deleted_service_paths():
    # Keep historical audit evidence and the original migration plan unchanged.
    for directory in ("docs/ci", "docs/internal", "docs/install", "docs/network"):
        for path in (ROOT / directory).rglob("*.md"):
            text = path.read_text()
            for service in ("hub", "license", "site", "uptime"):
                assert f"android/{service}" not in text, path


def test_forward_fix_requires_dispatch_and_publication_evidence():
    text = (ROOT / "docs/ci/release-channels.md").read_text()
    section = text.split("### 2. Ship the forward fix", 1)[1].split("### 3.", 1)[0]
    assert "Pushing the tag does not publish" in section
    assert "gh workflow run release.yml --repo HobbyCoders/ai-shuffle --ref v0.9.3 -f version=v0.9.3" in section
    assert "before\n   declaring it live" in section
