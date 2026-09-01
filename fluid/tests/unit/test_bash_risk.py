"""Bash read-only classifier + permission denial messages.

Covers the ``bash_risk`` table (safe vs write), the engine-level behavior
(read-only Bash commands run under ``on_write`` without a prompt), and the
three denial-text variants: denied at the prompt, denied by a configured rule,
and auto-denied because the run cannot prompt.
"""

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from fluid.cli.repl import auto_deny
from fluid.engine.native import NativeLoop
from fluid.events import (
    PermissionRequest,
    RespondPermission,
    ToolResult,
    TurnComplete,
    UserTurn,
)
from fluid.permissions.engine import PermissionEngine, Verdict
from fluid.permissions.rules import Rule
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore
from fluid.tools.builtins import register_builtins
from fluid.tools.builtins.bash import SPEC as BASH_SPEC
from fluid.tools.builtins.bash_risk import classify, is_read_only
from fluid.tools.registry import ToolCtx, ToolRegistry

# ─── classifier table ────────────────────────────────────────────────────────

SAFE_COMMANDS = [
    "ls",
    "ls -la",
    "dir",
    "pwd",
    "cat file.txt",
    'cat "a b.txt"',
    r"cat C:\Users\dev\notes.txt",  # Windows path as an argument
    r'"C:\Program Files\Git\bin\git.exe" status',  # quoted Windows program path
    "/usr/bin/grep -r pattern src",
    "git status --short",
    "git status",
    "git log --oneline -10",
    "git -C subdir log --oneline -5",
    "git --no-pager diff",
    "git diff HEAD~1",
    "git show abc123",
    "git rev-parse HEAD",
    "git ls-files",
    "git blame file.py",
    "git branch",
    "git branch -a",
    "git branch --list",
    "git remote -v",
    "git stash list",
    "git tag -l",
    "git describe",
    "git config --get user.name",
    "git config --list",
    "cat a | grep b",
    "cat a | grep b | wc -l",
    "grep -rn 'permission denied' fluid | head -20",
    "sed -n 1,5p file.txt",
    "sed -n '1,5p' file.txt",
    "awk '{print $1}' data.txt",
    "find . -name '*.py'",
    "find . -name x -type f",
    "wc -l file.txt",
    "head -n 20 file.txt",
    "tail -5 log.txt",
    "sort file.txt | uniq",
    "cut -d, -f1 data.csv | tr a-z A-Z",
    "diff a.txt b.txt",
    "stat file.txt",
    "du -sh .",
    "df -h",
    "echo hello world",
    "printf '%s\\n' hi",
    "which git",
    "where git",
    "whoami && uname -a",
    "date",
    "printenv PATH",
    "env",
    "jq '.name' package.json",
    "tree -L 2",
    "basename /a/b/c.txt",
    "realpath .",
    "readlink some-link",
    "file archive.zip",
    "true",
    "uv --version",
    "uv pip list",
    "pip list",
    "npm list",
    "npm outdated",
    "cargo tree",
    "go version",
    "node --version",
    "ls 2>&1",
    "ls 2>&1 | head -5",
    "LANG=C grep foo bar",  # locale assignments cannot change what grep does
    "LC_ALL=C sort file.txt",
    "NO_COLOR=1 ls",
    "sed -n '/error/p' file.txt",  # an e inside an address regex is not the e command
    "sed -n 's/one/two/p' file.txt",
    "grep 'foo.*bar' file.txt",  # quoted regex, not a glob
    "rg -n 'a.*' src",
    "grep foo *.py",  # glob that cannot reach a credential file
    "ls *.log",
]

WRITE_COMMANDS = [
    "",
    "   ",
    "rm -rf x",
    "git push",
    "git push origin main",
    "git commit -m 'x'",
    "git checkout -b feature",
    "git branch -D old",  # branch beyond listing flags
    "git stash pop",
    "git stash",
    "git config user.name x",  # config write form
    "git config --unset user.name",
    "git -c core.pager=evil log",  # -c can point git at external programs
    "git log --output=out.txt",  # --output writes a file
    "ls > out.txt",
    "ls >> out.txt",
    "echo hi > file",
    "cat a 2> err.txt",
    "cat <(ls)",  # process substitution
    "cat a | xargs rm",
    "cat a | xargs -n1 echo",  # xargs is never safe
    r"find . -name x -exec rm {} \;",
    "find . -name x -execdir rm {} +",
    "find . -name '*.tmp' -delete",
    "find . -ok rm {} +",
    "sed -i s/a/b/ file",
    "sed --in-place s/a/b/ file",
    "sed s/a/b/ file",  # without -n sed prints/could do more; conservative
    "sed -n 'w out.txt' file",  # sed w command writes a file
    "awk 'BEGIN{system(\"rm x\")}'",
    "awk '{print $1 | \"sort\"}' f",  # awk piping to a command
    "echo `ls`",  # backticks
    "echo $(ls)",  # command substitution
    "sudo ls",
    "python -c 'print(1)'",
    "python3 script.py",
    "node app.js",
    "npm install",
    "npm run build",
    "cargo build",
    "uv sync",
    "pip install requests",
    "go build ./...",
    "touch marker.txt",
    "mkdir newdir",
    "cp a b",
    "mv a b",
    "curl https://example.com",
    "env rm x",  # env with arguments runs a command
    "sort -o out.txt file",  # sort -o writes
    "uniq input output",  # second positional is an output file
    "date -s '2020-01-01'",  # sets the clock
    "rg --pre cmd pattern",  # rg --pre runs a command
    "ls &",  # background job
    "(ls)",  # subshell
    "ls; rm x",  # one bad simple command poisons the whole line
    "true && rm x",
    "cat a || rm b",
    "ls\nrm -rf x",  # newline as a command separator
    'ls "unterminated',  # unlexable → conservative write
    "FOO=1",  # bare assignment is not a read
    "unknowntool --help",
    # Environment variables that make a read-only program run something else.
    "FOO=bar ls",  # not on the inert allowlist
    "GIT_EXTERNAL_DIFF=/tmp/evil.sh git diff",
    "LD_PRELOAD=/tmp/e.so ls",
    "GIT_PAGER='sh -c \"rm -rf x\"' git log",
    "LESSOPEN='|rm %s' less foo",
    "BASH_ENV=/tmp/x ls",
    "less foo",  # LESSOPEN/LESSCLOSE run commands, so less is off the table
    # GNU sed e runs a shell command; -f/awk -f load a script we never see.
    "sed -n '1e rm -rf x' f",
    "sed -n 's/a/b/e' f",
    "sed -n 's/a/b/ge' f",
    "sed -n -e '1e rm' f",
    "sed -n --expression='1e rm' f",
    "sed -n -f script.sed f",
    "sed -nf script.sed f",
    "sed -n --file=script.sed f",
    "awk -f prog.awk f",
    "awk --file=prog.awk f",
    "gawk -l ext f",  # loads an extension library
    "gawk -i inc.awk f",
]


@pytest.mark.parametrize("command", SAFE_COMMANDS)
def test_read_only_commands_are_safe(command: str) -> None:
    assert is_read_only(command), command
    assert classify({"command": command}) == "safe"


@pytest.mark.parametrize("command", WRITE_COMMANDS)
def test_everything_else_is_write(command: str) -> None:
    assert not is_read_only(command), command
    assert classify({"command": command}) == "write"


@pytest.mark.parametrize(
    "command",
    [
        "cat .env*",
        "cat .env?",
        "cat ?env",
        "cat .en[v]",
        "cat .{env,bashrc}",
        "cat config/.env*",
        "cat ~/.ssh/*",
        "head /root/.aws/*",
        "cat *",  # could expand to anything, .env included
        "cat *.pem",
        "cat '.en'*",  # the unquoted tail is still a glob
        "cat {.env",  # unbalanced brace: unresolvable, so not safe
    ],
)
def test_globs_that_could_reach_a_credential_file_are_write(command: str) -> None:
    assert is_read_only(command), command  # read-only program, but ...
    assert classify({"command": command}) == "write"


def test_classify_without_command_is_write() -> None:
    assert classify({}) == "write"
    assert classify({"command": 42}) == "write"


def test_bash_spec_wires_the_classifier() -> None:
    """The static risk stays write; the per-input hook lowers it for reads."""
    assert BASH_SPEC.risk == "write"
    assert BASH_SPEC.effective_risk({"command": "git status --short"}) == "safe"
    assert BASH_SPEC.effective_risk({"command": "rm -rf x"}) == "write"


# ─── engine decisions ────────────────────────────────────────────────────────


def test_on_write_policy_uses_effective_risk() -> None:
    engine = PermissionEngine("on_write")
    assert engine.evaluate(BASH_SPEC, {"command": "git status"}) == "allow"
    assert engine.evaluate(BASH_SPEC, {"command": "ls -la | wc -l"}) == "allow"
    assert engine.evaluate(BASH_SPEC, {"command": "rm -rf x"}) == "ask"
    # always_ask still asks even for read-only commands.
    assert PermissionEngine("always_ask").evaluate(BASH_SPEC, {"command": "ls"}) == "ask"


def test_decide_names_the_rule_that_denied() -> None:
    engine = PermissionEngine(
        "never_ask", rules=[Rule(tool="Bash", decision="deny", input_globs={"command": "rm *"})]
    )
    verdict = engine.decide(BASH_SPEC, {"command": "rm -rf x"})
    assert isinstance(verdict, Verdict)
    assert verdict.decision == "deny" and verdict.source == "rule"
    assert "'Bash'" in verdict.deny_message()
    assert verdict.deny_message().startswith("permission denied by a configured rule")


# ─── engine-level: the native loop ───────────────────────────────────────────


class ScriptedProvider:
    name = "anthropic"
    caps = ProviderCaps()

    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        self.scripts = scripts
        self.calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        script = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        for chunk in script:
            yield chunk


def _text(text: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": text}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


def _bash(tuid: str, command: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {}}),
        StreamChunk("block_start",
                    {"index": 0, "block_type": "tool_use", "tool_name": "Bash", "tool_use_id": tuid}),
        StreamChunk("tool_args_delta", {"index": 0, "partial_json": json.dumps({"command": command})}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {}, "stop_reason": "tool_use"}),
        StreamChunk("message_stop", {}),
    ]


def _loop(provider, tmp_path, *, permissions, store=None, sid=None) -> NativeLoop:
    registry = ToolRegistry()
    register_builtins(registry)
    return NativeLoop(
        provider=provider, store=store or MemoryStore(), model="m", session_id=sid,
        registry=registry, ctx=ToolCtx(cwd=str(tmp_path)), permissions=permissions,
    )


async def _watch_first(loop: NativeLoop, sid: str, kind: type):
    holder: dict = {}
    got = asyncio.Event()

    async def watch() -> None:
        async for e in loop.bus.subscribe(sid):
            if isinstance(e, kind):
                holder["e"] = e
                got.set()
                return

    return holder, got, asyncio.create_task(watch())


async def test_git_status_runs_without_prompt_under_on_write(tmp_path) -> None:
    provider = ScriptedProvider([_bash("t1", "git status --short"), _text("done")])
    loop = _loop(provider, tmp_path, permissions=PermissionEngine("on_write"))
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=15)

    events = loop.bus.log(loop.session_id)
    assert not any(isinstance(e, PermissionRequest) for e in events)
    assert any(isinstance(e, ToolResult) for e in events)  # the command ran
    assert any(isinstance(e, TurnComplete) for e in events)


async def test_rm_rf_prompts_under_on_write(tmp_path) -> None:
    provider = ScriptedProvider([_bash("t1", "rm -rf x"), _text("done")])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _loop(provider, tmp_path, permissions=PermissionEngine("on_write"),
                 store=store, sid=sid)
    holder, got, watcher = await _watch_first(loop, sid, PermissionRequest)
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(got.wait(), timeout=5)
    req: PermissionRequest = holder["e"]
    assert req.name == "Bash" and req.risk == "write"
    await loop.submit(RespondPermission(request_id=req.request_id, decision="deny"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    await watcher

    result = next(e for e in loop.bus.log(sid) if isinstance(e, ToolResult))
    assert result.is_error
    # Variant 1: denied by the user at the prompt (responder gave no reason).
    assert "permission denied by the user at the prompt" in result.output


async def test_rule_denial_names_the_rule(tmp_path) -> None:
    provider = ScriptedProvider([_bash("t1", "rm -rf x"), _text("done")])
    permissions = PermissionEngine("never_ask", rules=[Rule(tool="Bash", decision="deny")])
    loop = _loop(provider, tmp_path, permissions=permissions)
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)

    result = next(e for e in loop.bus.log(loop.session_id) if isinstance(e, ToolResult))
    assert result.is_error
    # Variant 2: denied by a configured rule, naming its tool pattern.
    assert "configured rule" in result.output and "'Bash'" in result.output


async def test_non_interactive_auto_deny_explains_itself(tmp_path) -> None:
    request = PermissionRequest(
        session_id="s", turn_id="t", request_id="perm_1",
        tool_use_id="tu1", name="Bash", input={"command": "rm -rf x"}, risk="write",
    )
    response = await auto_deny(request)
    # Variant 3: the run cannot prompt; the message says how to unblock.
    assert isinstance(response, RespondPermission)
    assert response.decision == "deny" and response.request_id == "perm_1"
    assert response.message is not None
    assert response.message.startswith("permission denied: this non-interactive run cannot prompt")
    assert "--approval-policy never_ask" in response.message
    assert "Bash" in response.message and ".fluid/config.toml" in response.message
    assert "Do not retry the same action through another tool" in response.message


async def test_doom_loop_forced_prompt_carries_reason(tmp_path) -> None:
    same = _bash("t", "echo hi")
    provider = ScriptedProvider([same, same, same, _text("done")])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _loop(provider, tmp_path, permissions=PermissionEngine("on_write"),
                 store=store, sid=sid)
    holder, got, watcher = await _watch_first(loop, sid, PermissionRequest)
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(got.wait(), timeout=15)
    req: PermissionRequest = holder["e"]
    assert req.reason is not None and "doom-loop guard" in req.reason
    await loop.submit(RespondPermission(request_id=req.request_id, decision="deny"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    await watcher

    results = [e for e in loop.bus.log(sid) if isinstance(e, ToolResult)]
    # The denial text tells the model both what happened and why it was asked.
    assert results[-1].is_error
    assert "permission denied by the user at the prompt" in results[-1].output
    assert "doom-loop guard" in results[-1].output


# ─── allow_always is scoped to the programs a Bash command runs ──────────────


def test_program_names_collects_every_program() -> None:
    from fluid.tools.builtins.bash_risk import program_names

    assert program_names("cat a | grep b && ls") == frozenset({"cat", "grep", "ls"})
    assert program_names('"C:\\Program Files\\Git\bin\\git.exe" status') == frozenset({"git"})
    assert program_names("FOO=1 git log") == frozenset({"git"})
    assert program_names("echo $(rm x)") is None
    assert program_names("") is None


def test_allow_always_on_bash_covers_same_programs_only() -> None:
    from fluid.permissions.engine import PermissionEngine
    from fluid.tools.builtins.bash import SPEC as bash_spec

    engine = PermissionEngine("on_write")
    push = {"command": "git push origin main"}
    assert engine.decide(bash_spec, push).decision == "ask"
    engine.allow_always(bash_spec.name, bash_spec.scope_of(push))
    assert engine.decide(bash_spec, push).source == "session"
    assert engine.decide(bash_spec, {"command": "git commit -m x"}).source == "session"
    assert engine.decide(bash_spec, {"command": "rm -rf build"}).decision == "ask"
    assert engine.decide(bash_spec, {"command": "git push | tee log"}).decision == "ask"
    # A later unscoped allow widens to every Bash call.
    engine.allow_always(bash_spec.name, None)
    assert engine.decide(bash_spec, {"command": "rm -rf build"}).source == "session"


def test_allow_always_without_scope_hook_covers_every_call() -> None:
    from fluid.permissions.engine import PermissionEngine
    from fluid.tools.registry import ToolSpec

    spec = ToolSpec(name="Write", description="", input_schema={}, risk="write")
    engine = PermissionEngine("on_write")
    engine.allow_always("Write", spec.scope_of({"file_path": "a"}))
    assert engine.decide(spec, {"file_path": "b"}).source == "session"
