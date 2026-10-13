"""Fluid-native AGENTS discovery, imports, skills, and commands.

Discovery walks up to the git root, so tests add a ``.git`` marker at the
project root. Global tests isolate HOME from the developer's real settings.
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
    render_command,
    split_command_args,
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


def test_claude_instructions_are_inert_until_imported(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / ".claude").mkdir()
    (proj / ".claude" / "CLAUDE.md").write_text("legacy rules", encoding="utf-8")
    (proj / "AGENTS.md").write_text("native rules", encoding="utf-8")

    files = discover_instruction_files(proj, include_global=False)
    assert {f.kind for f in files} == {"agents"}

    stack = build_instruction_stack(proj, include_global=False)
    assert "legacy rules" not in stack
    assert "native rules" in stack
    assert f"# From: {proj / 'AGENTS.md'}" in stack


def test_byte_identical_sibling_gets_dedup_note(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    body = "identical guidance"
    (proj / "AGENTS.md").write_text(body, encoding="utf-8")
    (proj / "AGENT.md").write_text(body, encoding="utf-8")

    stack = build_instruction_stack(proj, include_global=False)
    # Present once, and the sibling is noted rather than silently dropped.
    assert stack.count(body) == 1
    assert "byte-identical" in stack
    assert f"# From: {proj / 'AGENTS.md'}" in stack


def test_walk_up_orders_outermost_then_cwd(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "AGENTS.md").write_text("root guidance", encoding="utf-8")
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
    assert str(proj / ".claude" / "CLAUDE.md") not in paths
    assert str(proj / ".agents" / "AGENTS.md") in paths
    assert str(proj / "AGENT.md") in paths


def test_nested_instructions_for_subdir_file(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    sub = proj / "service"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("service-local rules", encoding="utf-8")
    (sub / "handler.py").write_text("# code", encoding="utf-8")

    nested = nested_instructions(sub / "handler.py")
    assert len(nested) == 1
    assert nested[0].scope == "nested"
    assert "service-local rules" in nested[0].content


def test_global_before_project(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / ".fluid").mkdir(parents=True)
    (home / ".fluid" / "AGENTS.md").write_text("global user rules", encoding="utf-8")
    _set_home(monkeypatch, home)

    proj = _mk_project(tmp_path / "proj")
    (proj / "AGENTS.md").write_text("project rules", encoding="utf-8")

    stack = build_instruction_stack(proj)
    assert "global user rules" in stack
    assert "project rules" in stack
    assert stack.index("global user rules") < stack.index("project rules")


# ─── @-imports (CE2) ─────────────────────────────────────────────────────────


def test_import_relative_to_containing_file(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "style.md").write_text("be terse", encoding="utf-8")
    (proj / "AGENTS.md").write_text("Rules:\n@style.md\n", encoding="utf-8")

    stack = build_instruction_stack(proj, include_global=False)
    assert "be terse" in stack
    assert "@style.md" not in stack


def test_import_resolves_relative_to_the_file_not_the_cwd(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    sub = proj / "pkg"
    sub.mkdir()
    (sub / "style.md").write_text("sub-local style", encoding="utf-8")
    (sub / "AGENTS.md").write_text("@style.md", encoding="utf-8")

    # cwd is the project root; the import still resolves next to pkg/CLAUDE.md.
    stack = build_instruction_stack(sub, include_global=False)
    assert "sub-local style" in stack


def test_import_tilde_path(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / ".fluid").mkdir(parents=True)
    (home / ".fluid" / "communication-style.md").write_text("short answers", encoding="utf-8")
    _set_home(monkeypatch, home)

    proj = _mk_project(tmp_path / "proj")
    (proj / "AGENTS.md").write_text("@~/.fluid/communication-style.md", encoding="utf-8")

    stack = build_instruction_stack(proj, include_global=False)
    assert "short answers" in stack


def test_import_absolute_path(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    external = tmp_path / "external.md"
    external.write_text("external guidance", encoding="utf-8")
    (proj / "AGENTS.md").write_text(f"@{external}", encoding="utf-8")

    stack = build_instruction_stack(proj, include_global=False)
    assert "external guidance" in stack


def test_imports_nest(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "inner.md").write_text("innermost text", encoding="utf-8")
    (proj / "outer.md").write_text("outer text\n@inner.md", encoding="utf-8")
    (proj / "AGENTS.md").write_text("@outer.md", encoding="utf-8")

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
    (proj / "AGENTS.md").write_text("@f1.md", encoding="utf-8")

    stack = build_instruction_stack(proj, include_global=False)
    for i in range(1, 6):  # hops 1..5 expand
        assert f"level{i}" in stack
    assert "level6" not in stack  # the 6th hop is refused
    assert "@f6.md" in stack  # …and its token is left literal


def test_import_cycle_is_cut(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "a.md").write_text("a body\n@b.md", encoding="utf-8")
    (proj / "b.md").write_text("b body\n@a.md", encoding="utf-8")
    (proj / "AGENTS.md").write_text("@a.md", encoding="utf-8")

    stack = build_instruction_stack(proj, include_global=False)
    assert stack.count("a body") == 1
    assert stack.count("b body") == 1
    assert "@a.md" in stack  # cycle edge kept literal


def test_self_import_is_cut(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "AGENTS.md").write_text("rules\n@AGENTS.md", encoding="utf-8")

    stack = build_instruction_stack(proj, include_global=False)
    assert stack.count("rules") == 1
    assert "@AGENTS.md" in stack


def test_import_inside_fenced_block_is_inert(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "style.md").write_text("SHOULD NOT APPEAR", encoding="utf-8")
    (proj / "AGENTS.md").write_text(
        "How imports work:\n\n```md\n@style.md\n```\n\n~~~\n@style.md\n~~~\n",
        encoding="utf-8",
    )

    stack = build_instruction_stack(proj, include_global=False)
    assert "SHOULD NOT APPEAR" not in stack
    assert stack.count("@style.md") == 2


def test_import_inside_inline_code_span_is_inert(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "style.md").write_text("SHOULD NOT APPEAR", encoding="utf-8")
    (proj / "AGENTS.md").write_text(
        "Write `@style.md` to import it.\nThen @style.md expands.\n", encoding="utf-8"
    )

    stack = build_instruction_stack(proj, include_global=False)
    assert "`@style.md`" in stack  # the documented token survives
    assert "SHOULD NOT APPEAR" in stack  # the live one expanded


def test_missing_import_keeps_the_literal_token(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "AGENTS.md").write_text("see @nope.md for details", encoding="utf-8")

    stack = build_instruction_stack(proj, include_global=False)
    assert "@nope.md" in stack


def test_import_keeps_surrounding_line_text(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "style.md").write_text("TERSE", encoding="utf-8")
    (proj / "AGENTS.md").write_text("before @style.md after", encoding="utf-8")

    assert "before TERSE after" in build_instruction_stack(proj, include_global=False)


def test_expand_imports_ignores_mid_word_at_signs(tmp_path) -> None:
    (tmp_path / "style.md").write_text("SHOULD NOT APPEAR", encoding="utf-8")
    src = tmp_path / "CLAUDE.md"
    text = "mail me at user@style.md please"
    assert expand_imports(text, src) == text


# ─── CLAUDE.local.md / AGENTS.override.md (CE3) ──────────────────────────────


def test_claude_local_is_inert_until_imported(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "AGENTS.md").write_text("shared rules", encoding="utf-8")
    (proj / "CLAUDE.local.md").write_text("my personal rules", encoding="utf-8")

    paths = {f.path for f in discover_instruction_files(proj, include_global=False)}
    assert str(proj / "CLAUDE.local.md") not in paths
    stack = build_instruction_stack(proj, include_global=False)
    assert "shared rules" in stack
    assert "my personal rules" not in stack


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


def test_global_codex_files_are_inert_until_imported(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "AGENTS.md").write_text("codex base rules", encoding="utf-8")
    (home / ".codex" / "AGENTS.override.md").write_text("codex override rules", encoding="utf-8")
    _set_home(monkeypatch, home)

    proj = _mk_project(tmp_path / "proj")
    stack = build_instruction_stack(proj)
    assert "codex override rules" not in stack
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
    skill_dir = proj / ".agents" / "skills" / "pdf"
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
    assert names == {"deploy"}


# ─── render_command ──────────────────────────────────────────────────────────


def test_split_command_args_quotes_and_backslashes() -> None:
    assert split_command_args("a b c") == ["a", "b", "c"]
    assert split_command_args('one "two words" three') == ["one", "two words", "three"]
    assert split_command_args("'a b'") == ["a b"]
    # No backslash escaping — Windows paths survive intact.
    assert split_command_args(r"C:\dev\x.py") == [r"C:\dev\x.py"]
    assert split_command_args("") == []


def test_render_command_positional_and_arguments(tmp_path) -> None:
    body = "---\ndescription: d\n---\nFix $1 in $2; all: $ARGUMENTS; missing: [$3]"
    out = render_command(body, 'bug-42 "src dir"', cwd=tmp_path)
    assert out == 'Fix bug-42 in src dir; all: bug-42 "src dir"; missing: []'


def test_render_command_arg_value_not_resubstituted(tmp_path) -> None:
    # A user argument containing $2 must not expand into another argument.
    out = render_command("first: $1 second: $2", "$2 real", cwd=tmp_path)
    assert out == "first: $2 second: real"


def test_render_command_inline_bash_requires_allowed_tools(tmp_path) -> None:
    raw = "Status: !`echo hi`"
    # No allowed-tools frontmatter → literal.
    assert render_command(raw, "", cwd=tmp_path) == "Status: !`echo hi`"
    gated = f"---\nallowed-tools: Bash(echo:*)\n---\n{raw}"
    assert render_command(gated, "", cwd=tmp_path) == "Status: hi"


def test_render_command_bash_skips_fenced_blocks(tmp_path) -> None:
    raw = "---\nallowed-tools: Bash\n---\n```\n!`echo hi`\n```\n!`echo yes`"
    out = render_command(raw, "", cwd=tmp_path)
    assert "!`echo hi`" in out  # fenced example stayed literal
    assert "yes" in out and "!`echo yes`" not in out


def test_render_command_bash_sees_substituted_args(tmp_path) -> None:
    raw = "---\nallowed-tools: Bash\n---\nOut: !`echo $1`"
    assert render_command(raw, "hello", cwd=tmp_path) == "Out: hello"


def test_render_command_file_references(tmp_path) -> None:
    (tmp_path / "notes.md").write_text("the notes body", encoding="utf-8")
    out = render_command("Review @notes.md and @missing.md please", "", cwd=tmp_path)
    # Tokens stay in place; existing file's contents are appended, missing skipped.
    assert out.startswith("Review @notes.md and @missing.md please")
    assert "Contents of notes.md:\nthe notes body" in out
    assert "Contents of missing.md" not in out


def test_render_command_blocks_separates_file_dumps(tmp_path) -> None:
    """Each @file dump is its own block, so an oversize one can be intake-
    stubbed by the compaction view without hiding the instruction text."""
    from fluid.context.instructions import render_command_blocks

    (tmp_path / "a.md").write_text("alpha body", encoding="utf-8")
    (tmp_path / "b.md").write_text("beta body", encoding="utf-8")
    blocks = render_command_blocks("Review @a.md and @b.md", "", cwd=tmp_path)
    assert blocks[0] == "Review @a.md and @b.md"
    assert blocks[1] == "Contents of a.md:\nalpha body"
    assert blocks[2] == "Contents of b.md:\nbeta body"
    # The joined form is exactly what render_command returns.
    assert render_command("Review @a.md and @b.md", "", cwd=tmp_path) == "\n\n".join(blocks)


def test_render_command_file_reference_in_code_span_inert(tmp_path) -> None:
    (tmp_path / "notes.md").write_text("body", encoding="utf-8")
    out = render_command("Docs about `@notes.md` syntax", "", cwd=tmp_path)
    assert "Contents of" not in out


def test_parse_frontmatter_folded_block_joins_lines() -> None:
    text = "---\nname: deploy\ndescription: >-\n  Deploy the app.\n  Use when asked.\n---\nbody"
    fields, body = parse_frontmatter(text)
    assert fields == {"name": "deploy", "description": "Deploy the app. Use when asked."}
    assert body == "body"


def test_parse_frontmatter_folded_chomping_and_paragraphs() -> None:
    clip = "---\nd: >\n  one\n  two\n\n  three\n---\n"
    assert parse_frontmatter(clip)[0]["d"] == "one two\nthree\n"
    keep = "---\nd: >+\n  one\n\n\nnext: x\n---\n"
    assert parse_frontmatter(keep)[0] == {"d": "one\n\n\n", "next": "x"}


def test_parse_frontmatter_literal_block_keeps_newlines() -> None:
    text = "---\nd: |\n  line one\n    indented\n  line three\n---\n"
    assert parse_frontmatter(text)[0]["d"] == "line one\n  indented\nline three\n"
    strip = "---\nd: |-\n  a\n  b\n\n---\n"
    assert parse_frontmatter(strip)[0]["d"] == "a\nb"


def test_parse_frontmatter_plain_continuation_is_not_a_field() -> None:
    text = (
        "---\nname: helper\ndescription: Runs shell commands. Also handles\n"
        "  tools: Bash and friends.\ntools: [Read, Grep]\n---\n"
    )
    fields, _ = parse_frontmatter(text)
    assert fields["description"] == "Runs shell commands. Also handles tools: Bash and friends."
    assert fields["tools"] == ["Read", "Grep"]


def test_parse_frontmatter_single_line_and_list_shapes_unchanged() -> None:
    text = '---\nname: "quoted"\ndesc: a, b, c\ntools: [Read, Grep]\nempty:\n---\nb'
    fields, body = parse_frontmatter(text)
    assert fields == {"name": "quoted", "desc": "a, b, c", "tools": ["Read", "Grep"], "empty": ""}
    assert body == "b"


def test_skills_index_renders_a_folded_description(tmp_path) -> None:
    from fluid.context.compiler import skills_index

    proj = _mk_project(tmp_path)
    skill_dir = proj / ".agents" / "skills" / "live-monitor-flow"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: live-monitor-flow\ndescription: >-\n"
        "  Keep the app running and watch it.\n"
        "  Delegate feature work to sub-agents.\n---\nbody",
        encoding="utf-8",
    )
    index = skills_index(proj, include_global=False)
    assert (
        "- live-monitor-flow: Keep the app running and watch it. "
        "Delegate feature work to sub-agents."
    ) in index
    assert ">-" not in index


# ─── AGENTS.local.md layering, .fluid override, skill/command precedence ─────


def test_agents_local_layers_after_agents_md(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "AGENTS.md").write_text("shared rules", encoding="utf-8")
    (proj / "AGENTS.local.md").write_text("personal rules", encoding="utf-8")
    (proj / ".fluid").mkdir()
    (proj / ".fluid" / "AGENTS.md").write_text("dot shared", encoding="utf-8")
    (proj / ".fluid" / "AGENTS.local.md").write_text("dot personal", encoding="utf-8")

    stack = build_instruction_stack(proj, include_global=False)
    assert (
        stack.index("shared rules")
        < stack.index("personal rules")
        < stack.index("dot shared")
        < stack.index("dot personal")
    )


def test_dot_fluid_override_replaces_dot_fluid_agents_md(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "AGENTS.md").write_text("root rules", encoding="utf-8")
    (proj / ".fluid").mkdir()
    (proj / ".fluid" / "AGENTS.md").write_text("replaced", encoding="utf-8")
    (proj / ".fluid" / "AGENTS.override.md").write_text("dot override", encoding="utf-8")

    paths = {f.path for f in discover_instruction_files(proj, include_global=False)}
    assert str(proj / ".fluid" / "AGENTS.override.md") in paths
    assert str(proj / ".fluid" / "AGENTS.md") not in paths
    stack = build_instruction_stack(proj, include_global=False)
    assert "root rules" in stack and "dot override" in stack
    assert "replaced" not in stack


def test_agents_override_leaves_the_local_file_in_place(tmp_path) -> None:
    proj = _mk_project(tmp_path)
    (proj / "AGENTS.md").write_text("replaced", encoding="utf-8")
    (proj / "AGENTS.override.md").write_text("override body", encoding="utf-8")
    (proj / "AGENTS.local.md").write_text("still personal", encoding="utf-8")

    stack = build_instruction_stack(proj, include_global=False)
    assert stack.index("override body") < stack.index("still personal")
    assert "replaced" not in stack


def _skill(root, name: str, note: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {note}\n---\nbody {note}", encoding="utf-8"
    )


def test_project_skill_shadows_global_and_fluid_beats_agents(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    _set_home(monkeypatch, home)
    _skill(home / ".fluid" / "skills" / "dup", "dup", "global")
    _skill(home / ".fluid" / "skills" / "global-only", "global-only", "home")
    proj = _mk_project(tmp_path / "proj")
    _skill(proj / ".agents" / "skills" / "dup", "dup", "root agents")
    _skill(proj / ".fluid" / "skills" / "dup", "dup", "root fluid")
    child = proj / "child"
    _skill(child / ".agents" / "skills" / "dup", "dup", "nearest")

    at_root = {s.name: s for s in discover_skills(proj)}
    assert at_root["dup"].description == "root fluid"
    assert at_root["dup"].scope == "project"
    assert at_root["global-only"].scope == "global"
    assert [s.name for s in discover_skills(proj)] == ["dup", "global-only"]

    at_child = {s.name: s for s in discover_skills(child)}
    assert at_child["dup"].description == "nearest"
    assert {s.name: s.description for s in discover_skills(child, include_project=False)} == {
        "dup": "global", "global-only": "home",
    }


async def test_skill_builtin_loads_the_project_body_over_the_global_one(tmp_path, monkeypatch) -> None:
    from fluid.tools.builtins.skill import handler
    from fluid.tools.registry import ToolCtx

    home = tmp_path / "home"
    _set_home(monkeypatch, home)
    _skill(home / ".fluid" / "skills" / "dup", "dup", "global")
    proj = _mk_project(tmp_path / "proj")
    _skill(proj / ".fluid" / "skills" / "dup", "dup", "project")

    outcome = await handler({"name": "dup"}, ToolCtx(cwd=str(proj)))
    assert "body project" in outcome.text
    assert "body global" not in outcome.text


def test_command_precedence_mirrors_skills(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    _set_home(monkeypatch, home)
    (home / ".fluid" / "commands").mkdir(parents=True)
    (home / ".fluid" / "commands" / "dup.md").write_text("global", encoding="utf-8")
    (home / ".fluid" / "commands" / "only-global.md").write_text("home", encoding="utf-8")
    proj = _mk_project(tmp_path / "proj")
    for tree, text in ((".agents", "root agents"), (".fluid", "root fluid")):
        (proj / tree / "commands").mkdir(parents=True)
        (proj / tree / "commands" / "dup.md").write_text(text, encoding="utf-8")
    child = proj / "child"
    (child / ".agents" / "commands").mkdir(parents=True)
    (child / ".agents" / "commands" / "dup.md").write_text("nearest", encoding="utf-8")

    by_name = {c.name: c for c in discover_commands(proj)}
    assert Path(by_name["dup"].path).read_text(encoding="utf-8") == "root fluid"
    assert by_name["only-global"].scope == "global"
    assert [c.name for c in discover_commands(proj)] == ["dup", "only-global"]
    nearest = next(c for c in discover_commands(child) if c.name == "dup")
    assert Path(nearest.path).read_text(encoding="utf-8") == "nearest"
