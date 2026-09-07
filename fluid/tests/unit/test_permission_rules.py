"""Rule matching: per-command Bash globs, ``prefix:*`` patterns, path spellings."""

from __future__ import annotations

import os

import pytest

from fluid.permissions.rules import Rule, evaluate_rules, path_candidates
from fluid.tools.builtins.bash_risk import simple_commands


# ─── simple_commands ─────────────────────────────────────────────────────────


def test_simple_commands_splits_on_every_separator() -> None:
    assert simple_commands('git commit -m "a b" && rm -rf /') == ['git commit -m "a b"', "rm -rf /"]
    assert simple_commands("ls; rm -rf x") == ["ls", "rm -rf x"]
    assert simple_commands("ls || rm x") == ["ls", "rm x"]
    assert simple_commands("cat a | grep b") == ["cat a", "grep b"]
    assert simple_commands("ls\nrm x") == ["ls", "rm x"]
    assert simple_commands("ls & rm x") == ["ls", "rm x"]


@pytest.mark.parametrize(
    "command",
    ["echo $(rm x)", "echo `rm x`", "(rm x)", "cat <(rm x)", "", "   ", 'ls "unterminated'],
)
def test_simple_commands_is_none_when_it_cannot_see_everything(command: str) -> None:
    assert simple_commands(command) is None


# ─── Bash command rules ──────────────────────────────────────────────────────

_NPM_ALLOW = Rule(tool="Bash", decision="allow", input_globs={"command": "npm run *"})
_RM_DENY = Rule(tool="Bash", decision="deny", input_globs={"command": "rm *"})


def test_bash_allow_rule_needs_every_simple_command_to_match() -> None:
    assert _NPM_ALLOW.matches("Bash", {"command": "npm run build"})
    assert _NPM_ALLOW.matches("Bash", {"command": "npm run build && npm run lint"})
    assert not _NPM_ALLOW.matches("Bash", {"command": "npm run build && rm -rf /"})
    assert not _NPM_ALLOW.matches("Bash", {"command": "npm run build; rm -rf /"})
    assert not _NPM_ALLOW.matches("Bash", {"command": "npm run build | sh"})
    assert not _NPM_ALLOW.matches("Bash", {"command": "npm run build\nrm -rf /"})


def test_bash_allow_rule_never_matches_substitution_or_subshell() -> None:
    assert not _NPM_ALLOW.matches("Bash", {"command": "npm run $(rm -rf /)"})
    assert not _NPM_ALLOW.matches("Bash", {"command": "npm run `rm -rf /`"})
    assert not _NPM_ALLOW.matches("Bash", {"command": "(npm run build)"})


def test_bash_deny_rule_matches_any_simple_command() -> None:
    assert _RM_DENY.matches("Bash", {"command": "ls; rm -rf x"})
    assert _RM_DENY.matches("Bash", {"command": "ls && rm -rf x"})
    assert _RM_DENY.matches("Bash", {"command": "rm -rf x"})
    assert not _RM_DENY.matches("Bash", {"command": "ls"})


def test_rules_are_case_sensitive_on_every_platform() -> None:
    assert not Rule(tool="Bash", decision="deny", input_globs={"command": "RM *"}).matches(
        "Bash", {"command": "rm x"}
    )
    assert not Rule(tool="bash", decision="deny").matches("Bash", {"command": "rm x"})


def test_last_match_still_wins_across_split_commands() -> None:
    rules = [_NPM_ALLOW, _RM_DENY]
    assert evaluate_rules(rules, "Bash", {"command": "npm run build"}) == "allow"
    assert evaluate_rules(rules, "Bash", {"command": "npm run build && rm -rf /"}) == "deny"
    assert evaluate_rules(rules, "Bash", {"command": "npm run build && ls"}) is None


# ─── prefix:* patterns ───────────────────────────────────────────────────────


def test_prefix_pattern_matches_the_word_and_its_arguments_only() -> None:
    curl = Rule(tool="Bash", decision="deny", input_globs={"command": "curl:*"})
    assert curl.matches("Bash", {"command": "curl http://evil"})
    assert curl.matches("Bash", {"command": "curl"})
    assert curl.matches("Bash", {"command": "ls && curl http://evil"})
    assert not curl.matches("Bash", {"command": "curlx"})
    assert not curl.matches("Bash", {"command": "ls"})

    status = Rule(tool="Bash", decision="allow", input_globs={"command": "git status:*"})
    assert status.matches("Bash", {"command": "git status --short"})
    assert status.matches("Bash", {"command": "git status"})
    assert not status.matches("Bash", {"command": "git statusx"})
    assert not status.matches("Bash", {"command": "git status; rm x"})


def test_prefix_form_is_literal_in_path_patterns() -> None:
    # "C:*" on a path field is a drive, not a prefix rule.
    drive = Rule(tool="Read", decision="deny", input_globs={"file_path": "C:*"})
    assert drive.matches("Read", {"file_path": "C:/secrets.txt"})


# ─── path rules ──────────────────────────────────────────────────────────────

_ENV_ASK = Rule(tool="Read", decision="ask", input_globs={"file_path": "*.env"})


@pytest.mark.parametrize("spelling", [".env", ".env/", ".env ", "./x/../.env", ".\\.env", "x\\..\\.env"])
def test_path_rule_sees_through_alternate_spellings(spelling: str) -> None:
    assert _ENV_ASK.matches("Read", {"file_path": spelling})


def test_path_rule_does_not_over_match() -> None:
    assert not _ENV_ASK.matches("Read", {"file_path": "src/main.py"})
    assert not _ENV_ASK.matches("Read", {"file_path": ".env.example/../main.py"})


def test_path_allow_rule_uses_the_normalized_spelling_only() -> None:
    src = Rule(tool="Read", decision="allow", input_globs={"file_path": "./src/**"})
    assert src.matches("Read", {"file_path": "src\\a.py"})
    assert src.matches("Read", {"file_path": "./src/a.py"})
    assert src.matches("Read", {"file_path": "src/a.py"})
    # Raw "src/../.env" matches "src/**" as text; the resolved path does not.
    assert not src.matches("Read", {"file_path": "src/../.env"})


def test_path_rule_matches_absolute_paths_under_cwd(tmp_path) -> None:
    src = Rule(tool="Read", decision="allow", input_globs={"file_path": "./src/**"})
    inside = str(tmp_path / "src" / "a.py")
    outside = str(tmp_path.parent / "src" / "a.py")
    assert src.matches("Read", {"file_path": inside}, cwd=str(tmp_path))
    assert not src.matches("Read", {"file_path": outside}, cwd=str(tmp_path))
    assert not src.matches("Read", {"file_path": inside})  # no cwd: no relative form


def test_path_candidates_follow_symlinks_when_the_file_exists(tmp_path) -> None:
    real = tmp_path / ".env"
    real.write_text("SECRET=1")
    link = tmp_path / "harmless.txt"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    _, normalized = path_candidates("harmless.txt", cwd=str(tmp_path))
    assert any(c.endswith("/.env") for c in normalized)
    assert _ENV_ASK.matches("Read", {"file_path": "harmless.txt"}, cwd=str(tmp_path))
