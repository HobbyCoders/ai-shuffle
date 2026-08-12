"""M6 — CLAUDE.md / AGENTS.md discovery, merge rule, skills, commands.

Discovery walks up to the git root, so tests drop a ``.git`` marker at the
project root to bound the walk. Global discovery reads ``~`` / XDG, so tests
that exercise it isolate HOME + XDG_CONFIG_HOME to tmp dirs.
"""

from omni.context.instructions import (
    build_instruction_stack,
    discover_commands,
    discover_instruction_files,
    discover_skills,
    nested_instructions,
    parse_frontmatter,
)


def _mk_project(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_both_claude_and_agents_in_same_scope_are_concatenated(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "CLAUDE.md").write_text("claude rules here", encoding="utf-8")
    (proj / "AGENTS.md").write_text("agents rules here", encoding="utf-8")

    files = discover_instruction_files(proj, include_global=False)
    kinds = {f.kind for f in files}
    assert kinds == {"claude", "agents"}  # neither dropped

    stack = build_instruction_stack(proj, include_global=False)
    assert "claude rules here" in stack
    assert "agents rules here" in stack
    assert f"# From: {proj / 'CLAUDE.md'}" in stack
    assert f"# From: {proj / 'AGENTS.md'}" in stack


def test_byte_identical_sibling_gets_dedup_note(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    body = "identical guidance"
    (proj / "CLAUDE.md").write_text(body, encoding="utf-8")
    (proj / "AGENTS.md").write_text(body, encoding="utf-8")

    stack = build_instruction_stack(proj, include_global=False)
    # Present once, and the sibling is noted rather than silently dropped.
    assert stack.count(body) == 1
    assert "byte-identical" in stack
    assert f"# From: {proj / 'AGENTS.md'}" in stack


def test_walk_up_orders_outermost_then_cwd(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "CLAUDE.md").write_text("root guidance", encoding="utf-8")
    sub = proj / "pkg" / "mod"
    sub.mkdir(parents=True)
    (sub / "AGENTS.md").write_text("sub guidance", encoding="utf-8")

    stack = build_instruction_stack(sub, include_global=False)
    assert stack.index("root guidance") < stack.index("sub guidance")


def test_dir_config_variants_discovered(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / ".claude").mkdir()
    (proj / ".claude" / "CLAUDE.md").write_text("dot claude", encoding="utf-8")
    (proj / ".agents").mkdir()
    (proj / ".agents" / "AGENTS.md").write_text("dot agents", encoding="utf-8")
    (proj / "AGENT.md").write_text("singular agent", encoding="utf-8")

    paths = {f.path for f in discover_instruction_files(proj, include_global=False)}
    assert str(proj / ".claude" / "CLAUDE.md") in paths
    assert str(proj / ".agents" / "AGENTS.md") in paths
    assert str(proj / "AGENT.md") in paths


def test_nested_instructions_for_subdir_file(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    sub = proj / "service"
    sub.mkdir()
    (sub / "CLAUDE.md").write_text("service-local rules", encoding="utf-8")
    (sub / "handler.py").write_text("# code", encoding="utf-8")

    nested = nested_instructions(sub / "handler.py")
    assert len(nested) == 1
    assert nested[0].scope == "nested"
    assert "service-local rules" in nested[0].content


def test_global_before_project(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("global user rules", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))

    proj = _mk_project(tmp_path / "proj")
    (proj / "CLAUDE.md").write_text("project rules", encoding="utf-8")

    stack = build_instruction_stack(proj)
    assert "global user rules" in stack
    assert "project rules" in stack
    assert stack.index("global user rules") < stack.index("project rules")


def test_parse_frontmatter() -> None:
    text = "---\nname: My Skill\ndescription: Does a thing\n---\nbody line 1\nbody line 2\n"
    fields, body = parse_frontmatter(text)
    assert fields["name"] == "My Skill"
    assert fields["description"] == "Does a thing"
    assert body.strip() == "body line 1\nbody line 2"
    # No frontmatter → passthrough.
    assert parse_frontmatter("plain body") == ({}, "plain body")


def test_discover_skills_parses_name_and_description(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    skill_dir = proj / ".claude" / "skills" / "pdf"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: pdf\ndescription: Work with PDF files\n---\nlong body not loaded yet",
        encoding="utf-8",
    )
    agents_skill = proj / ".agents" / "skills" / "xlsx"
    agents_skill.mkdir(parents=True)
    (agents_skill / "SKILL.md").write_text(
        "---\nname: xlsx\ndescription: Spreadsheets\n---\nbody", encoding="utf-8"
    )

    skills = discover_skills(proj, include_global=False)
    by_name = {s.name: s for s in skills}
    assert by_name["pdf"].description == "Work with PDF files"
    assert by_name["pdf"].body_loaded is False  # progressive disclosure
    assert by_name["xlsx"].description == "Spreadsheets"


def test_discover_commands(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    cmd_dir = proj / ".claude" / "commands"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "review.md").write_text("review prompt", encoding="utf-8")
    a_dir = proj / ".agents" / "commands"
    a_dir.mkdir(parents=True)
    (a_dir / "deploy.md").write_text("deploy prompt", encoding="utf-8")

    names = {c.name for c in discover_commands(proj, include_global=False)}
    assert names == {"review", "deploy"}
