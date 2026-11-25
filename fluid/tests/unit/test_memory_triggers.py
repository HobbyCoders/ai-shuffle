"""Memory v3 phase 0: deterministic triggers derived from when-lines.

Every fixture is invented. The matchers are precision first: a fact must not
fire on one shared topic word, a negated instruction, text a command only
carries, or a place the session is not in.
"""
import time

from fluid.codemode.triggers import (
    TriggerFact,
    command_text,
    derive_triggers,
    is_candidate,
    match_tool,
    match_turn,
    normalize_tool,
    prepare,
    split_commands,
)


def fact(when, fact_id="f1", text="", key=None):
    return TriggerFact(fact_id, when, derive_triggers(when, text, key), len(text))


def fired(matches):
    return [(m.fact_id, m.kind) for m in matches]


# --- derivation --------------------------------------------------------------

def test_open_pr_when_line_derives_a_command_and_words():
    t = derive_triggers("When opening a pull request for the example service")
    assert r"re:^gh pr create\b" in t.command
    assert "exampl servic" in t.words
    assert not t.tools and not t.paths


def test_backticked_command_and_identifier_become_command_triggers():
    t = derive_triggers("When CI fails on test_widget_cache_expires or after `make lint` errors")
    assert "test_widget_cache_expires" in t.command
    assert "make lint" in t.command


def test_known_tool_and_paths_are_recognised():
    t = derive_triggers("When starting a review Task or changing files under docs/guides/")
    assert t.tools == ("Task",)
    assert t.paths == ("**/docs/guides/**",)
    assert t.edit_paths


def test_backticked_mixed_case_path_matches_absolute_paths():
    f = [fact("When editing `Docs/Guide.md` or `./Tools/build.sh`")]
    assert fired(match_tool("Edit", {"file_path": "/abs/repo/Docs/Guide.md"}, f)) == [("f1", "path")]
    assert fired(match_tool("Write", {"file_path": "/abs/repo/Tools/build.sh"}, f)) == [("f1", "path")]
    assert match_tool("Edit", {"file_path": "/abs/repo/Docs/Other.md"}, f) == []


def test_prose_slash_words_are_not_paths():
    assert derive_triggers("When installing/updating the widget tool").paths == ()


def test_generic_when_line_falls_back_to_the_text():
    t = derive_triggers("When working with the owner", text="Widget exports need the gizmo flag set.")
    assert "widget export" in t.words


def test_written_when_line_ignores_the_text():
    t = derive_triggers("When exporting widgets", text="Unrelated gizmo calibration notes.")
    assert not any("gizmo" in w for w in t.words)


def test_named_place_keeps_only_words_and_requires_the_name():
    t = derive_triggers("When pushing branches from a Windows worker")
    assert t.command == () and t.tools == ()
    assert "+window" in t.words
    f = [fact("When pushing branches from a Windows worker")]
    assert match_turn("Push the widget branches now and push the gizmo branches too.", f) == []
    assert fired(match_turn("From the Windows worker, push branches to the mirror.", f)) == [("f1", "words")]


def test_question_when_line_implies_no_command():
    assert derive_triggers("When the owner asks why a pull request has not merged").command == ()


def test_normalize_tool_strips_mcp_prefixes():
    assert normalize_tool("mcp__agents__Task") == "Task"
    assert normalize_tool("Bash") == "Bash"


# --- candidates --------------------------------------------------------------

def test_always_identity_rules_superseded_conflicted_and_pair_answers_are_not_candidates():
    base = {"when": "When exporting widgets", "text": "Use the gizmo flag."}
    entries = [
        dict(base, id="keep"),
        dict(base, id="always", always=True, protected=True),
        dict(base, id="identity", key="profile.name"),
        dict(base, id="rule", key="behavior.all.example"),
        dict(base, id="pair", key="memory.coexist.v2.a.b"),
        dict(base, id="old", superseded_by="keep"),
        dict(base, id="disputed", conflicted=True),
    ]
    assert [e["id"] for e in entries if is_candidate(e)] == ["keep"]
    assert [f.id for f in prepare(entries)] == ["keep"]


# --- turn matching -----------------------------------------------------------

WIDGET = "When exporting widget archives to the gizmo store"


def test_one_shared_word_does_not_fire():
    assert match_turn("The widget looks wrong on the settings page.", [fact(WIDGET)]) == []


def test_close_phrase_and_words_fire():
    matches = match_turn("Please export the widget archives to the gizmo store tonight.", [fact(WIDGET)])
    assert fired(matches) == [("f1", "words")]
    assert matches[0].score >= 3


def test_words_spread_across_a_long_brief_do_not_fire():
    filler = " ".join(f"topic{i} detail{i} remark{i}." for i in range(20))
    text = f"The widget exporter needs a log line. {filler} Separately, the gizmo store is slow."
    assert match_turn(text, [fact(WIDGET)]) == []


def test_negated_clause_is_not_the_situation():
    text = "Review only. Do not export widget archives to the gizmo store."
    assert match_turn(text, [fact(WIDGET)]) == []


def test_weak_words_alone_do_not_fire():
    f = [fact("When merging a PR after CI checks fail")]
    assert match_turn("Fix the PR so CI checks pass, then check the build.", f) == []


def test_turn_matches_are_ranked_best_first():
    facts = [fact("When calibrating the gizmo sensor", "weak"),
             fact("When calibrating the gizmo sensor array on the bench rig", "strong")]
    matches = match_turn("Calibrate the gizmo sensor array on the bench rig.", facts)
    assert [m.fact_id for m in matches][0] == "strong"


# --- tool matching -----------------------------------------------------------

def test_command_trigger_fires_on_the_command_only():
    f = [fact("When opening a pull request for the example service")]
    assert fired(match_tool("Bash", {"command": "gh pr create --title x"}, f)) == [("f1", "command")]
    assert match_tool("Bash", {"command": "gh pr view 12"}, f) == []


def test_first_push_needs_an_upstream_flag_and_push_ignores_branch_deletes():
    first = fact("When preparing a branch for its first push", "first")
    every = fact("Before every push", "every")
    assert fired(match_tool("Bash", {"command": "git push -u origin topic"}, [first])) == [("first", "command")]
    assert match_tool("Bash", {"command": "git push origin topic"}, [first]) == []
    assert fired(match_tool("Bash", {"command": "git push origin topic"}, [every])) == [("every", "command")]
    assert match_tool("Bash", {"command": "git push origin --delete topic"}, [every]) == []


def test_text_a_command_carries_is_not_searched():
    every = [fact("Before every push")]
    heredoc = "cat > notes.md <<'EOF'\nRemember to git push later\nEOF\nwc -l notes.md"
    assert match_tool("Bash", {"command": heredoc}, every) == []
    message = "git commit -m '" + "Explain why git push was skipped here. " * 5 + "'"
    assert match_tool("Bash", {"command": message}, every) == []


def test_heredoc_bodies_end_only_at_the_exact_delimiter_line():
    every = [fact("Before every push")]
    empty = "cat > notes.md <<EOF\nEOF\ngit push origin topic"
    assert fired(match_tool("Bash", {"command": empty}, every)) == [("f1", "command")]
    leaky = "cat > notes.md <<EOF\nEOF is not the delimiter here\ngit push origin topic\nEOF\nwc -l notes.md"
    assert match_tool("Bash", {"command": leaky}, every) == []
    tabbed = "cat <<-'END' > notes.md\n\tgit push origin topic\n\tEND\ngit push origin topic"
    assert command_text({"command": tabbed}) == "cat > notes.md ; git push origin topic"
    quoted = 'cat <<"EOF" > a.md && echo done\ngit push here\nEOF'
    assert command_text({"command": quoted}) == "cat > a.md ; echo done"


def test_built_in_rules_match_the_program_not_its_arguments():
    every = [fact("Before every push")]
    for carried in ('git commit -m "then git push"', "echo git push", "grep -rn 'git push' docs",
                    "gh pr create --body 'run git push first'"):
        assert match_tool("Bash", {"command": carried}, every) == [], carried
    for runs in ("git -C svc push origin topic", "cd svc && git push", "sudo -E git push",
                 "(cd svc; git push)"):
        assert fired(match_tool("Bash", {"command": runs}, every)) == [("f1", "command")], runs


def test_split_commands_reads_heads_after_launchers():
    heads = [c.head for c in split_commands(
        {"command": "CI=1 uv run pytest -q | tee out.log && timeout 30 python3 -m pytest x # pytest"})]
    assert heads == ["pytest -q", "tee out.log", "pytest x"]


def test_prose_command_triggers_are_literals_never_patterns():
    t = derive_triggers("When a check prints `re:[` or `(a+)+$` in the widget log")
    assert "re:[" in t.command and "(a+)+$" in t.command
    f = [TriggerFact("f1", "x", t)]
    assert match_tool("Bash", {"command": "ls -la"}, f) == []
    assert fired(match_tool("Bash", {"command": "echo re:["}, f)) == [("f1", "command")]
    assert match_tool("Bash", {"command": "echo " + "a" * 40 + "!"}, f) == []


def test_command_scan_is_linear_and_bounded():
    adversarial = ['"' + '\\"' * 4096, "'" * 8192, "<<EOF\n" * 1365, "a\\ " * 2730,
                   '"x' * 4096, "x" * 8192 + "<<", "git push " + '"\\' * 4096]
    for text in adversarial:
        started = time.perf_counter()
        split_commands({"command": text})
        assert time.perf_counter() - started < 0.05, text[:20]
    huge = "echo x; " * 10_000 + "git push origin topic"
    assert match_tool("Bash", {"command": huge}, [fact("Before every push")]) == []  # past MAX_COMMAND_CHARS


def test_program_names_fire_only_where_they_run():
    f = [fact("When running Python tests in the example sandbox")]
    assert fired(match_tool("Bash", {"command": "cd svc && pytest -q tests/unit"}, f)) == [("f1", "command")]
    assert fired(match_tool("Bash", {"command": "cd svc\nCI=1 python -m pytest -q"}, f)) == [("f1", "command")]
    assert match_tool("Bash", {"command": "grep -n pytest requirements.txt"}, f) == []


def test_mutating_curl_needs_a_method_or_body():
    f = [fact("When calling the example API with mutating requests from curl")]
    assert match_tool("Bash", {"command": "curl -s http://host/api/items"}, f) == []
    assert fired(match_tool("Bash", {"command": "curl -s -X POST http://host/api/items"}, f)) == [
        ("f1", "command")]


def test_path_trigger_fires_on_edits_when_the_situation_is_changing_files():
    f = [fact("When changing files under docs/guides/")]
    path = {"file_path": "/repo/docs/guides/intro.md"}
    assert fired(match_tool("Edit", path, f)) == [("f1", "path")]
    assert match_tool("Read", path, f) == []
    assert match_tool("Edit", {"file_path": "/repo/src/guides/intro.md"}, f) == []


def test_tool_trigger_needs_the_when_line_words_in_its_input():
    f = [fact("When starting a gizmo calibration Task")]
    brief = {"description": "gizmo calibration", "prompt": "Start the gizmo calibration on the bench."}
    assert fired(match_tool("mcp__agents__Task", brief, f)) == [("f1", "tool")]
    assert match_tool("mcp__agents__Task", {"prompt": "Summarise the release notes."}, f) == []


def test_delegation_briefs_are_matched_like_turns():
    f = [fact(WIDGET)]
    brief = {"prompt": "Export the widget archives to the gizmo store and report the count."}
    assert fired(match_tool("StartSession", brief, f)) == [("f1", "words")]
    assert match_tool("Bash", {"command": "echo export widget archives to the gizmo store"}, f) == []
