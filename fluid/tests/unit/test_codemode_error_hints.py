"""Failed cells must carry their own correction.

Every case here is a shape taken from real transcripts, where the model's next
move was a wasted turn: an opaque ``SyntaxError`` on an unquoted tool argument,
a blocked import that named the allowlist but not the replacement, a
``NameError`` for a module the kernel could have bound in the first place. The
kernel's answer is the only thing the model sees, so the correction has to be
in it.
"""

import pytest

from fluid.codemode import SubprocessKernel
from fluid.codemode.kernel import HostReply
from fluid.codemode.projection import stub_source
from fluid.tools.builtins.grep import SPEC as GREP_SPEC


async def _no_tools(name: str, kwargs: dict) -> HostReply:
    return HostReply(error=f"no tools available in this kernel: {name}")


@pytest.fixture
async def kernel():
    k = SubprocessKernel()
    await k.start()
    await k.set_vfs({"Grep": stub_source(GREP_SPEC, "Grep")})
    try:
        yield k
    finally:
        await k.stop()


async def _err(kernel, code: str) -> str:
    result = await kernel.exec(code, timeout=20, on_host_call=_no_tools)
    assert result.error, f"expected a failure for {code!r}"
    return result.error


# ── SyntaxError: the two shapes that dominate ────────────────────────────


async def test_unquoted_tool_argument_names_the_fix(kernel) -> None:
    err = await _err(kernel, 'from tools.Grep import Grep\nGrep(pattern=def foo|bar)')
    assert "SyntaxError" in err
    assert "quote every string" in err
    assert 'pattern=r"def foo|bar"' in err


async def test_unterminated_string_points_at_the_heredoc(kernel) -> None:
    code = 'cmd = "gh pr create --body \\"$(cat <<\'EOF\'\nbody\n'
    err = await _err(kernel, code)
    assert "unterminated" in err
    assert "heredoc" in err
    assert "triple-quoted" in err


async def test_unterminated_fstring_gets_the_same_hint(kernel) -> None:
    err = await _err(kernel, 'x = f"gh pr create --body \'long body')
    assert "unterminated f-string" in err
    assert "heredoc" in err


async def test_bad_tool_import_spelling_is_corrected(kernel) -> None:
    err = await _err(kernel, "from tools.Grep as G")
    assert "from tools.Grep import Grep" in err


async def test_await_hint_survives_the_rewrite(kernel) -> None:
    err = await _err(kernel, "await Grep(pattern='x')")
    assert "without await" in err


# ── blocked capabilities name their replacement ──────────────────────────


@pytest.mark.parametrize(
    ("module", "expected"),
    [
        ("os", "Bash tool"),
        ("subprocess", "Bash tool"),
        ("tempfile", "kernel variables"),
        ("requests", "WebFetch"),
    ],
)
async def test_blocked_import_names_the_replacement(kernel, module, expected) -> None:
    err = await _err(kernel, f"import {module}")
    assert "is blocked in the code-mode kernel" in err
    assert expected in err
    # The allowlist still rides along, after the hint.
    assert "Allowed:" in err


async def test_blocked_open_names_read_or_write(kernel) -> None:
    reading = await _err(kernel, 'open("x.txt").read()')
    assert "from tools.Read import Read" in reading
    assert "'x.txt'" in reading

    writing = await _err(kernel, 'open("x.txt", "w").write("hi")')
    assert "from tools.Write import Write" in writing


# ── the namespace the model expects to already exist ─────────────────────


async def test_common_modules_are_prebound(kernel) -> None:
    result = await kernel.exec(
        'print(json.dumps({"a": 1}), re.match(r"a", "ab") is not None, Path("/tmp").name)',
        timeout=20,
        on_host_call=_no_tools,
    )
    assert result.error is None
    assert result.stdout.strip() == '{"a": 1} True tmp'


async def test_prebound_names_can_be_shadowed(kernel) -> None:
    result = await kernel.exec("json = 5\njson + 1", timeout=20, on_host_call=_no_tools)
    assert result.error is None
    assert result.value_repr == "6"


# ── wrong assumptions about a tool's shape ───────────────────────────────


async def test_bad_kwarg_returns_the_real_signature(kernel) -> None:
    err = await _err(kernel, 'from tools.Grep import Grep\nGrep(pattern="x", ignore_case=True)')
    assert "the signature is Grep(pattern: str" in err
    assert "read_tool_docs" not in err  # the answer is inline, not another call


async def test_dict_access_on_a_tool_result_is_explained(kernel) -> None:
    err = await _err(kernel, 'x = "text"\nx.get("k")')
    assert "returns text" in err
    assert "json.loads" in err
