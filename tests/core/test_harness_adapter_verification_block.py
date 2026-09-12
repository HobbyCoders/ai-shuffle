"""The verification doctrine reaches a code session's compiled prompt.

Six sessions in one day shipped user-facing changes checked only against their
own fixtures, mocks or public docs; the user rejected four of them. The prompt
said nothing about what "verified" has to mean, so this block does — next to
GITHUB_BLOCK, in the cached prefix, on the code lane only.
"""

from __future__ import annotations

import pytest

from app.core.provider.harness_adapter import (
    VERIFICATION_BLOCK,
    HarnessAdapter,
)
from app.core.provider.types import SessionOptions


def _compile(tmp_path, **extra):
    options = SessionOptions(
        model="fake-model", cwd=str(tmp_path), extra=dict(extra) or None
    )
    compiled = HarnessAdapter._compile_prompt_sections(
        "AGENT PROMPT", None, options, native=True
    )
    assert compiled is not None
    return compiled


def test_a_code_session_gets_the_block(tmp_path):
    compiled = _compile(tmp_path)
    assert VERIFICATION_BLOCK in compiled.text
    assert [s for s in compiled.sections if s.text == VERIFICATION_BLOCK]


@pytest.mark.parametrize(
    "clause",
    [
        # 015c1d77 F1, c6576abc F2, 4ce681e2 F1: observed in the running app,
        # or reported as not observed.
        "watched it work in the running app",
        "instead of calling it verified",
        "A test count is not a verification.",
        # 04e64a2d F1: a fixture proves the consumer, not the producer.
        "seeds the field your change reads",
        "drive the producing path too",
        # 015c1d77 F1: capabilities come from a probe, not docs or the
        # serializer that already encodes the assumption.
        "live probe of the configured provider",
        "Public docs describe someone else's account",
        # 4453db07 F8: a shared rule has more than one surface.
        "verify every surface it applies to",
        # e8ece32a F5: scratch fixtures broke the project's own npm run check.
        "OUT of the typechecked and tested tree",
    ],
)
def test_every_clause_the_audit_asked_for_is_in_it(clause):
    assert clause in VERIFICATION_BLOCK


def test_the_chat_lane_does_not_get_it(tmp_path):
    """A chat session cannot change anything the user sees, and the block is
    prefix that would be paid for on every turn."""
    compiled = _compile(tmp_path, session_mode="chat")
    assert VERIFICATION_BLOCK not in (compiled.text or "")


def test_a_read_only_surface_does_not_get_it(tmp_path):
    """Ruling T9, applied to doctrine: an agent that cannot edit is never the
    one reporting a change as shipped."""
    options = SessionOptions(model="fake-model", cwd=str(tmp_path))
    options.tool_allowlist = ["Read", "Grep", "Glob"]
    compiled = HarnessAdapter._compile_prompt_sections(
        "AGENT PROMPT", None, options, native=True
    )
    assert compiled is not None
    assert VERIFICATION_BLOCK not in (compiled.text or "")


def test_the_block_is_fixed_text(tmp_path):
    """Cached prefix: two compilations of the same session are byte-identical,
    and nothing in the block interpolates per-session state."""
    first = _compile(tmp_path)
    second = _compile(tmp_path)
    assert first.text == second.text
    assert "{" not in VERIFICATION_BLOCK


def test_the_block_stays_short(tmp_path):
    """It is prefix on every code session — a page of rules, not an essay.
    Round 9 added the browser budget, how to reach the UI and the ops
    measurement rule (~600 chars); the cap moves with them, once."""
    assert len(VERIFICATION_BLOCK) <= 2600
