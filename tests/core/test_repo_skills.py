"""Repo-level prose skills under ``.fluid/skills`` (round-9 audit, item 3).

fluid discovers ``<dir>/.fluid/skills/<name>/SKILL.md`` walking up from the
session cwd, so a skill committed at the checkout root is visible from every
worktree the app provisions under it (the container keeps worktrees at
``<checkout>/.fluid/worktrees/<name>``) and on every worker that checks the
project out. The two skills authored this round must parse and be findable.
"""
from __future__ import annotations

from pathlib import Path

from fluid.context.instructions import discover_skills, parse_frontmatter

REPO = Path(__file__).resolve().parents[2]
SKILLS = REPO / ".fluid" / "skills"


def _names(cwd):
    return {s.name for s in discover_skills(cwd, include_global=False, include_plugins=False)}


def test_the_round_nine_skills_are_committed_with_valid_frontmatter():
    for name in ("preview-branch", "play-internal-release"):
        path = SKILLS / name / "SKILL.md"
        assert path.is_file(), f"{path} missing"
        fields, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert fields["name"] == name
        assert len(fields["description"]) > 40
        assert len(body.strip()) > 500


def test_preview_skill_carries_the_real_commands():
    text = (SKILLS / "preview-branch" / "SKILL.md").read_text(encoding="utf-8")
    for needle in ("python -m app.preview stamp", "AI_SHUFFLE_PREVIEW=1", "0.0.0.0",
                   "preview-access", "scope_ref", "browser_screenshot", ":8000"):
        assert needle in text, needle


def test_play_skill_names_the_workflow_and_its_gates():
    text = (SKILLS / "play-internal-release" / "SKILL.md").read_text(encoding="utf-8")
    for needle in ("android-play.yml", "publish_to_play", "PLAY_SIGNING_READY",
                   "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", "ai-shuffle-play-bundle",
                   "com.hobbycoders.aishuffle", "android-play-enrollment.yml"):
        assert needle in text, needle


def test_skills_are_discovered_from_the_checkout_and_from_a_nested_worktree(tmp_path):
    """Mirror the two layouts the workers use: the checkout root itself, and a
    worktree provisioned INSIDE it (``.fluid/worktrees/<name>`` on the
    container, ``.claude/worktrees/<name>`` on the desktop)."""
    checkout = tmp_path / "ai-shuffle"
    skill = checkout / ".fluid" / "skills" / "preview-branch" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text((SKILLS / "preview-branch" / "SKILL.md").read_text(encoding="utf-8"), encoding="utf-8")
    assert "preview-branch" in _names(checkout)
    for nested in (".fluid/worktrees/feat-x", ".claude/worktrees/feat-y"):
        wt = checkout / nested
        wt.mkdir(parents=True)
        assert "preview-branch" in _names(wt), nested


def test_the_real_checkout_exposes_them_to_fluid():
    assert {"preview-branch", "play-internal-release"} <= _names(REPO)
