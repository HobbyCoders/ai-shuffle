"""M6 — CLAUDE.md / AGENTS.md discovery, merge rule, skills, commands.

Discovery walks up to the git root, so tests drop a ``.git`` marker at the
project root to bound the walk. Global discovery reads ``~`` / XDG, so tests
that exercise it isolate HOME + XDG_CONFIG_HOME to tmp dirs.
"""

from pathlib import Path

from fluid.context.instructions import (
    build_instruction_stack,
    discover_commands,
    discover_instruction_files,
    discover_skills,
    expand_imports,
    nested_instructions,
    parse_frontmatter,
)


def _mk_project(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".git").mkdir()
    return tmp_path


def _set_home(monkeypatch, home) -> None:
    """Point ``Path.home()`` at ``home`` on both POSIX and Windows.

    ``expanduser`` consults HOME on POSIX but USERPROFILE on Windows, so a test
    that only sets HOME silently reads the developer's real ~/.claude.
    """
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(Path(home) / ".config"))


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
    _set_home(monkeypatch, home)

    proj = _mk_project(tmp_path / "proj")
    (proj / "CLAUDE.md").write_text("project rules", encoding="utf-8")

    stack = build_instruction_stack(proj)
    assert "global user rules" in stack
    assert "project rules" in stack
    assert stack.index("global user rules") < stack.index("project rules")


# ─── @-imports (CE2) ─────────────────────────────────────────────────────────


def test_import_relative_to_containing_file(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "style.md").write_text("be terse", encoding="utf-8")
    (proj / "CLAUDE.md").write_text("Rules:\n@style.md\n", encoding="utf-8")

    stack = build_instruction_stack(proj, include_global=False)
    assert "be terse" in stack
    assert "@style.md" not in stack


def test_import_resolves_relative_to_the_file_not_the_cwd(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    sub = proj / "pkg"
    sub.mkdir()
    (sub / "style.md").write_text("sub-local style", encoding="utf-8")
    (sub / "CLAUDE.md").write_text("@style.md", encoding="utf-8")

    # cwd is the project root; the import still resolves next to pkg/CLAUDE.md.
    stack = build_instruction_stack(sub, include_global=False)
    assert "sub-local style" in stack


def test_import_tilde_path(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "communication-style.md").write_text("short answers", encoding="utf-8")
    _set_home(monkeypatch, home)

    proj = _mk_project(tmp_path / "proj")
    (proj / "CLAUDE.md").write_text("@~/.claude/communication-style.md", encoding="utf-8")

    stack = build_instruction_stack(proj, include_global=False)
    assert "short answers" in stack


def test_import_absolute_path(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    external = tmp_path / "external.md"
    external.write_text("external guidance", encoding="utf-8")
    (proj / "CLAUDE.md").write_text(f"@{external}", encoding="utf-8")

    stack = build_instruction_stack(proj, include_global=False)
    assert "external guidance" in stack


def test_imports_nest(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "inner.md").write_text("innermost text", encoding="utf-8")
    (proj / "outer.md").write_text("outer text\n@inner.md", encoding="utf-8")
    (proj / "CLAUDE.md").write_text("@outer.md", encoding="utf-8")

    stack = build_instruction_stack(proj, include_global=False)
    assert "outer text" in stack
    assert "innermost text" in stack


def test_import_depth_capped_at_five_hops(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    # f1 … f7, each importing the next; the root CLAUDE.md imports f1.
    for i in range(1, 8):
        body = f"level{i}"
        if i < 7:
            body += f"\n@f{i + 1}.md"
        (proj / f"f{i}.md").write_text(body, encoding="utf-8")
    (proj / "CLAUDE.md").write_text("@f1.md", encoding="utf-8")

    stack = build_instruction_stack(proj, include_global=False)
    for i in range(1, 6):  # hops 1..5 expand
        assert f"level{i}" in stack
    assert "level6" not in stack  # the 6th hop is refused
    assert "@f6.md" in stack  # …and its token is left literal


def test_import_cycle_is_cut(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "a.md").write_text("a body\n@b.md", encoding="utf-8")
    (proj / "b.md").write_text("b body\n@a.md", encoding="utf-8")
    (proj / "CLAUDE.md").write_text("@a.md", encoding="utf-8")

    stack = build_instruction_stack(proj, include_global=False)
    assert stack.count("a body") == 1
    assert stack.count("b body") == 1
    assert "@a.md" in stack  # cycle edge kept literal


def test_self_import_is_cut(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "CLAUDE.md").write_text("rules\n@CLAUDE.md", encoding="utf-8")

    stack = build_instruction_stack(proj, include_global=False)
    assert stack.count("rules") == 1
    assert "@CLAUDE.md" in stack


def test_import_inside_fenced_block_is_inert(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "style.md").write_text("SHOULD NOT APPEAR", encoding="utf-8")
    (proj / "CLAUDE.md").write_text(
        "How imports work:\n\n```md\n@style.md\n```\n\n~~~\n@style.md\n~~~\n",
        encoding="utf-8",
    )

    stack = build_instruction_stack(proj, include_global=False)
    assert "SHOULD NOT APPEAR" not in stack
    assert stack.count("@style.md") == 2


def test_import_inside_inline_code_span_is_inert(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "style.md").write_text("SHOULD NOT APPEAR", encoding="utf-8")
    (proj / "CLAUDE.md").write_text(
        "Write `@style.md` to import it.\nThen @style.md expands.\n", encoding="utf-8"
    )

    stack = build_instruction_stack(proj, include_global=False)
    assert "`@style.md`" in stack  # the documented token survives
    assert "SHOULD NOT APPEAR" in stack  # the live one expanded


def test_missing_import_keeps_the_literal_token(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "CLAUDE.md").write_text("see @nope.md for details", encoding="utf-8")

    stack = build_instruction_stack(proj, include_global=False)
    assert "@nope.md" in stack


def test_import_keeps_surrounding_line_text(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "style.md").write_text("TERSE", encoding="utf-8")
    (proj / "CLAUDE.md").write_text("before @style.md after", encoding="utf-8")

    assert "before TERSE after" in build_instruction_stack(proj, include_global=False)


def test_expand_imports_ignores_mid_word_at_signs(tmp_path) -> None:
    (tmp_path / "style.md").write_text("SHOULD NOT APPEAR", encoding="utf-8")
    src = tmp_path / "CLAUDE.md"
    text = "mail me at user@style.md please"
    assert expand_imports(text, src) == text


# ─── CLAUDE.local.md / AGENTS.override.md (CE3) ──────────────────────────────


def test_claude_local_loads_after_claude_md(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "CLAUDE.md").write_text("shared rules", encoding="utf-8")
    (proj / "CLAUDE.local.md").write_text("my personal rules", encoding="utf-8")

    files = discover_instruction_files(proj, include_global=False)
    by_path = {f.path: f for f in files}
    local = by_path[str(proj / "CLAUDE.local.md")]
    assert local.kind == "claude"

    stack = build_instruction_stack(proj, include_global=False)
    assert stack.index("shared rules") < stack.index("my personal rules")


def test_agents_override_replaces_agents_md(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "AGENTS.md").write_text("upstream agents rules", encoding="utf-8")
    (proj / "AGENTS.override.md").write_text("overridden rules", encoding="utf-8")

    paths = {f.path for f in discover_instruction_files(proj, include_global=False)}
    assert str(proj / "AGENTS.override.md") in paths
    assert str(proj / "AGENTS.md") not in paths  # replaced, not stacked

    stack = build_instruction_stack(proj, include_global=False)
    assert "overridden rules" in stack
    assert "upstream agents rules" not in stack


def test_agents_override_keeps_agent_md_and_dot_agents(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "AGENTS.md").write_text("replaced", encoding="utf-8")
    (proj / "AGENTS.override.md").write_text("override body", encoding="utf-8")
    (proj / "AGENT.md").write_text("singular body", encoding="utf-8")
    (proj / ".agents").mkdir()
    (proj / ".agents" / "AGENTS.md").write_text("dot agents body", encoding="utf-8")

    stack = build_instruction_stack(proj, include_global=False)
    assert "override body" in stack
    assert "singular body" in stack
    assert "dot agents body" in stack
    assert "replaced" not in stack


def test_global_codex_override_preferred(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "AGENTS.md").write_text("codex base rules", encoding="utf-8")
    (home / ".codex" / "AGENTS.override.md").write_text("codex override rules", encoding="utf-8")
    _set_home(monkeypatch, home)

    proj = _mk_project(tmp_path / "proj")
    stack = build_instruction_stack(proj)
    assert "codex override rules" in stack
    assert "codex base rules" not in stack


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
