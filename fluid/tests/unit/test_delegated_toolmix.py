"""The delegated leg's per-turn tool mix: shell file work against dedicated tools."""

from __future__ import annotations

import logging

import pytest

from fluid.engine.delegated_proto import StreamJsonMapper
from fluid.engine.delegated_toolmix import ToolMix, classify_shell
from fluid.events.model import TurnComplete

CD = 'cd "D:/Projects/ai-shuffle/.fluid/worktrees/fix-ci" && '


@pytest.mark.parametrize(("command", "expected"), [
    # reads
    ("sed -n 1,80p .github/workflows/ci.yml", "read"),
    ("cat scripts/ci/policy.py; grep -rl x tests", "read"),
    ("head -30 docs/ci/a.md | head -80", "read"),
    ("tail -1 runs.tsv", "read"),
    ("wc -l app/core/usage.py", "read"),
    ('cat "C:/Users/me/notes.md" 2>/dev/null | head -40', "read"),
    # searches
    ('grep -rn "ci:full" docs/', "search"),
    ("rg -n usage app/", "search"),
    ("find . -name '*.yml'", "search"),
    ("ls docs/ci && head -30 docs/ci/*", "search"),
    # edits
    ("sed -i 's/a/b/' .github/workflows/ci.yml", "edit"),
    ("sed -Ei 's/a/b/' x.yml", "edit"),
    ("python - <<'EOF'\nfrom pathlib import Path\nPath('a.yml').write_text('x')\nEOF", "edit"),
    ("python3 -c \"open('a.txt', 'w').write('x')\"", "edit"),
    ("cat > tests/core/test_new.py <<'EOF'\nimport x\nEOF", "edit"),
    ("cat >> notes.md <<'EOF'\nline\nEOF", "edit"),
    # scratch writes and plain scripts are not project file work
    ("cat > /tmp/probe.py <<'EOF'\nprint(1)\nEOF", "other"),
    ("python - <<'EOF'\nimport csv\nprint(sum(1 for _ in csv.reader(open('/tmp/r.tsv'))))\nEOF",
     "other"),
    # only the head of the first segment decides
    ("git status -sb | head -3 && ls .github/workflows", "other"),
    ("for f in a b; do sed -n 1,60p $f.yml; done", "other"),
    ("echo hi > notes.md", "other"),
    ("", "other"),
    # PowerShell
    ("Get-Content app/core/usage.py -TotalCount 40", "read"),
    ("Select-String -Path *.py -Pattern usage", "search"),
    ("Get-ChildItem -Recurse -Filter *.yml", "search"),
    ("Set-Content -Path notes.md -Value 'x'", "edit"),
    ("'x' | Out-File notes.md", "edit"),
])
def test_classify_shell(command: str, expected: str) -> None:
    assert classify_shell(command) == expected
    # The working-directory prefix Claude Code adds never changes the class.
    if command:
        assert classify_shell(CD + command) == expected


def test_heredoc_bodies_do_not_decide_the_head() -> None:
    """A grep inside a heredoc body is data the script writes, not a search."""
    command = "cat > /tmp/x.sh <<'EOF'\ngrep -rn usage app/\nEOF\nbash /tmp/x.sh"
    assert classify_shell(command) == "other"


def test_non_string_command_is_other() -> None:
    assert classify_shell(None) == "other"
    assert classify_shell({"command": "ls"}) == "other"


def test_tool_mix_counts_each_call_once() -> None:
    mix = ToolMix()
    mix.record("t1", "Bash", {"command": "cat a.py"})
    mix.record("t1", "Bash", {"command": "cat a.py"})  # the same call again
    mix.record("t2", "Bash", {"command": "git status"})
    mix.record("t3", "Read", {"file_path": "a.py"})
    mix.record("t4", "mcp__fluid_bundled__Grep", {"pattern": "x"})
    mix.record("t5", "WebSearch", {"query": "x"})
    assert (mix.bash_file_ops, mix.dedicated_file_ops) == (1, 2)
    assert not mix.bash_heavy


def _tool_frame(tid: str, name: str, tool_input: dict, n: int) -> dict:
    return {"type": "assistant", "message": {
        "id": f"msg_{n}", "model": "claude-opus-5-5", "role": "assistant",
        "content": [{"type": "tool_use", "id": tid, "name": name, "input": tool_input}],
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }}


def _feed_turn(uses: list[tuple[str, str, dict]]) -> tuple[StreamJsonMapper, TurnComplete]:
    mapper = StreamJsonMapper(
        session_id="s", turn_id="t", model="opus", user_node_id="n",
    )
    for n, (tid, name, tool_input) in enumerate(uses, start=1):
        mapper.feed(_tool_frame(tid, name, tool_input, n))
    events = mapper.feed({
        "type": "result", "subtype": "success", "num_turns": len(uses),
        "total_cost_usd": 0.1, "usage": {"input_tokens": 10, "output_tokens": 2},
    }).events
    (done,) = [e for e in events if isinstance(e, TurnComplete)]
    return mapper, done


# The file-tool calls of production session 42595afe (2026-09-29, Opus 5.5 in
# bypass mode on a worker without CLAUDE_CODE_THRIFTY_SONIC=0), in order: 37
# Bash calls (sed -i edits, python heredocs rewriting YAML) against Read 2,
# Edit 2, Write 2 and Grep 1. Each command keeps the head, the worktree
# prefix and the redirects the CLI sent; long bodies and arguments are cut
# down and personal paths replaced.
_WT = 'cd "D:/Projects/ai-shuffle/.fluid/worktrees/fix-stabilize-production-workflows" && '
SESSION_42595AFE = [
    ("Bash", _WT + "git status -sb | head -3 && ls .github/workflows && wc -l .github/workflows/*.yml | tail -1"),
    ("Bash", _WT + 'cat "C:/Users/me/.claude/projects/repo/memory/ci-cost-cuts.md" 2>/dev/null | head -60'),
    ("Bash", _WT + "repo=$(gh repo view --json nameWithOwner -q .nameWithOwner) && echo $repo"),
    ("Bash", "cd /tmp && head -1 runs.tsv && tail -1 runs.tsv && cut -f1 runs.tsv | head"),
    ("Bash", "cd /tmp && python - <<'EOF'\nimport csv,collections\nprint(1)\nEOF"),
    ("Bash", _WT + 'grep -L "cancel-in-progress" .github/workflows/*.yml; echo ---'),
    ("Bash", "cd /tmp && python - <<'EOF'\nimport csv,math\nprint(2)\nEOF"),
    ("Bash", _WT + "git fetch -q origin && git log --oneline -1 origin/main"),
    ("Bash", _WT + "git status --porcelain | head && git reset -q --hard origin/main"),
    ("Bash", _WT + 'grep -rlE "android\\.yml|desktop\\.yml" .'),
    ("Bash", _WT + "cat scripts/ci/test-cost-policy.py; grep -rlE workflows tests scripts | head -20"),
    ("Bash", _WT + "cat scripts/ci/require-full-ci.py; git log --oneline -6"),
    ("Bash", _WT + "sed -n 1,60p .github/workflows/ci-backend.yml; echo ======"),
    ("Bash", _WT + 'grep -nE "canary|schedule|needs:" .github/workflows/ci-backend.yml'),
    ("Bash", _WT + 'for f in android desktop; do echo "=== $f"; sed -n 1,60p .github/workflows/$f.yml; done'),
    ("Bash", _WT + 'grep -n "ci:full" -B2 -A3 desktop/README.md'),
    ("Bash", _WT + "ls docs/ci && head -30 docs/ci/* | head -80"),
    ("Read", {"file_path": ".github/workflows/ci-backend.yml"}),
    ("Bash", _WT + "python - <<'EOF'\nimport re\nfrom pathlib import Path\np=Path('.github/workflows/android.yml')\np.write_text(p.read_text().replace('a','b'))\nEOF"),
    ("Bash", _WT + "sed -i 's/- name: Require successful full checks/- name: Require successful checks/' .github/workflows/ci.yml"),
    ("Bash", _WT + "python - <<'EOF'\nfrom pathlib import Path\nW=Path('.github/workflows')\n(W/'a.yml').write_text('x')\nEOF"),
    ("Bash", _WT + "python - <<'EOF'\nfrom pathlib import Path\nW=Path('.github/workflows')\n(W/'b.yml').write_text('x')\nEOF"),
    ("Edit", {"file_path": ".github/workflows/ci-backend.yml"}),
    ("Bash", _WT + 'grep -n "GH_TOKEN\\|gh api" .github/workflows/ci-backend.yml'),
    ("Bash", _WT + "python - <<'EOF'\nfrom pathlib import Path\np=Path('.github/workflows/container-postgres.yml')\np.write_text('x')\nEOF"),
    ("Bash", _WT + 'grep -n "verify_buyer" .github/workflows/container-postgres.yml; sed -n 183,192p x'),
    ("Grep", {"pattern": "container-postgres"}),
    ("Bash", _WT + 'grep -n "container-postgres\\|schedule" tests/scripts/test_ci.py'),
    ("Bash", _WT + "sed -n 385,400p tests/scripts/test_edge_publication_gate.py"),
    ("Bash", _WT + "sed -i '/^      - \"Container PostgreSQL\"$/d' .github/branch-rules.yml"),
    ("Write", {"file_path": "docs/ci/policy.md"}),
    ("Bash", _WT + "python - <<'EOF'\nfrom pathlib import Path\np=Path('scripts/ci/test-cost-policy.py'); t=p.read_text()\np.write_text(t)\nEOF"),
    ("Read", {"file_path": "scripts/ci/test-cost-policy.py"}),
    ("Bash", _WT + "python scripts/ci/test-artifact-policy.py 2>&1 | tail -4"),
    ("Bash", _WT + "(command -v actionlint || ls ~/go/bin/actionlint* 2>/dev/null)"),
    ("Bash", "cd /tmp && curl -sSL -o al.tgz https://example.invalid/actionlint.tgz"),
    ("Bash", _WT + "printf 'self-hosted-runner:\\n' > /tmp/al.yaml && /tmp/actionlint -config-file /tmp/al.yaml"),
    ("Edit", {"file_path": "docs/ci/policy.md"}),
    ("Bash", _WT + "git add -A && git status --short"),
    ("Bash", _WT + "git diff --stat HEAD~1 | tail -1 && gh pr create --base main --title x"),
    ("Write", {"file_path": "docs/ci/runbook.md"}),
    ("Bash", _WT + 'gh run view 36648690903 --log-failed 2>&1 | grep -E "FAILED" | head -30'),
    ("Bash", _WT + "venv/Scripts/python.exe -m pytest -q tests/core/test_install_files.py 2>&1 | tail -3"),
    ("Bash", "rm -f /tmp/actionlint.exe /tmp/al.tgz /tmp/al.yaml /tmp/runs.tsv; echo ok"),
]


def _as_uses(calls: list) -> list[tuple[str, str, dict]]:
    return [
        (f"toolu_{i}", name, {"command": arg} if isinstance(arg, str) else arg)
        for i, (name, arg) in enumerate(calls, start=1)
    ]


def test_a_bash_only_production_turn_is_flagged(caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluid.engine.delegated_proto")
    mapper, done = _feed_turn(_as_uses(SESSION_42595AFE))
    assert 20 <= done.usage["bash_file_ops"] <= 30
    assert done.usage["dedicated_file_ops"] == 7
    assert mapper.tool_mix.bash_heavy
    infos = [r for r in caplog.records if "delegated turn tool mix" in r.getMessage()]
    assert len(infos) == 1 and infos[0].levelno == logging.INFO
    assert "model=claude-opus-5-5" in infos[0].getMessage()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "file work through the shell" in warnings[0].getMessage()


def test_a_turn_using_the_dedicated_tools_does_not_warn(caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluid.engine.delegated_proto")
    uses = [
        ("t1", "Read", {"file_path": "a.py"}),
        ("t2", "Grep", {"pattern": "x"}),
        ("t3", "Edit", {"file_path": "a.py"}),
        ("t4", "Glob", {"pattern": "*.py"}),
        ("t5", "Bash", {"command": "pytest -q"}),
        ("t6", "Bash", {"command": "git status"}),
    ]
    _, done = _feed_turn(uses)
    assert (done.usage["bash_file_ops"], done.usage["dedicated_file_ops"]) == (0, 4)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
