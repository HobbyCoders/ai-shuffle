"""AI Shuffle's editable chat instructions (stored as ``security_instructions``).

The default text tells agents what the chat renders and how to link files so
the file API serves them; these tests pin the rules that keep links working.
"""
import re
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from app.api import settings as settings_api
from app.core.environment import DEFAULT_SECURITY_INSTRUCTIONS, get_security_instructions

#: Size of the default before the 2026-10 rewrite. The text ships with every
#: session, so it must not grow past this without a deliberate decision.
PRE_REWRITE_CHARS = 3448

SECTIONS = (
    "## Chat display",
    "### What renders",
    "### Generated media",
    "### Files on disk",
    "### HTML previews",
)


@pytest.mark.parametrize("custom", [None, ""])
def test_default_chat_instructions_are_used_without_a_custom_value(custom):
    with patch("app.core.environment.database.get_system_setting", return_value=custom) as read:
        instructions = get_security_instructions()
    read.assert_called_once_with("security_instructions")
    assert instructions == DEFAULT_SECURITY_INSTRUCTIONS
    for heading in SECTIONS:
        assert instructions.count(heading) == 1, heading


@pytest.mark.parametrize("custom", [
    "Keep replies short and link generated files.",
    DEFAULT_SECURITY_INSTRUCTIONS,
    "### HTML previews\nUse my custom HTML guidance.",
])
def test_custom_chat_instructions_are_used_without_hidden_append(custom):
    with patch("app.core.environment.database.get_system_setting", return_value=custom):
        assert get_security_instructions() == custom


@pytest.mark.asyncio
async def test_settings_save_and_revert_control_the_model_instructions():
    # Exercise the same API used by the chat-instructions editor, including the
    # producing path (save), rather than seeding only the value the prompt reads.
    initial = await settings_api.get_security_instructions(token="test-admin")
    assert initial.current == initial.default == DEFAULT_SECURITY_INSTRUCTIONS
    assert not initial.is_customized

    custom = "Use only this custom chat guidance."
    await settings_api.update_security_instructions(
        settings_api.SecurityInstructionsUpdateRequest(instructions=custom),
        token="test-admin",
    )
    saved = await settings_api.get_security_instructions(token="test-admin")
    assert saved.current == get_security_instructions() == custom
    assert saved.is_customized
    assert saved.default == DEFAULT_SECURITY_INSTRUCTIONS

    await settings_api.revert_security_instructions(token="test-admin")
    reverted = await settings_api.get_security_instructions(token="test-admin")
    assert reverted.current == get_security_instructions() == initial.default
    assert not reverted.is_customized


def test_default_stays_within_its_size_budget():
    assert len(DEFAULT_SECURITY_INSTRUCTIONS) <= PRE_REWRITE_CHARS
    # Measured at 819 chars after tightening; leave 13 chars of headroom.
    assert len(_files_on_disk(DEFAULT_SECURITY_INSTRUCTIONS)) <= 832


def _files_on_disk(text):
    heading = "### Files on disk"
    return heading + text.split(heading, 1)[1].split("### HTML previews", 1)[0]


def _assert_disk_sharing_rules(text):
    section = _files_on_disk(text)
    for rule in (
        "`/api/files/by-path?path=URL_ENCODED_FULL_PATH`",
        "`![alt](URL)` for images, `[label](URL)` for links and inline video/audio",
        "Share only existing files in a served root the viewer can access",
        "the worker's workspace or a registered project/worktree",
        "Never share scratch/temp paths",
        "For scratch/temp or unserved files, copy only the intended deliverable to a served root",
        "Verify the copy exists and is accessible; link or embed it, not the original",
        "If access is unverified, say it's unavailable",
        "Never copy or share secrets or credentials",
        "Don't commit copies",
    ):
        assert rule in section, rule


def test_default_states_where_links_and_embeds_work_and_what_to_do_otherwise():
    _assert_disk_sharing_rules(DEFAULT_SECURITY_INSTRUCTIONS)
    assert "never a bare filesystem path" in DEFAULT_SECURITY_INSTRUCTIONS


def test_default_has_no_repeated_paragraphs():
    paragraphs = re.split(r"\n\s*\n", DEFAULT_SECURITY_INSTRUCTIONS.strip())
    assert len(paragraphs) == len(set(paragraphs))


def test_default_encoding_examples_are_correct_on_windows_and_posix():
    examples = re.findall(r"`([^`]+)` becomes `(/api/files/by-path\?path=[^`]+)`", DEFAULT_SECURITY_INSTRUCTIONS)
    raw_paths = [raw for raw, _ in examples]
    assert any(re.match(r"^[A-Z]:\\", raw) for raw in raw_paths), "needs a Windows example"
    assert any(raw.startswith("/") for raw in raw_paths), "needs a POSIX example"
    for raw, link in examples:
        # Spaces, parentheses, backslashes and the drive colon are what break a
        # Markdown link in the chat renderer; each example must encode them.
        assert not re.search(r"[ ()\\:]", link.split("?", 1)[1]), link
        assert link == "/api/files/by-path?path=" + quote(raw, safe="/")
        assert parse_qs(urlsplit(link).query)["path"] == [raw]
    assert 'urllib.parse.quote(path, safe="/")' in DEFAULT_SECURITY_INSTRUCTIONS


def test_default_keeps_the_media_embed_rules():
    text = DEFAULT_SECURITY_INSTRUCTIONS
    # This rule covers all sources and tools, not a hard-coded provider/tool
    # list that becomes stale when another generation tool is registered.
    assert "Embed all media" in text
    assert "`![short description](IMAGE_URL)` from `image_url`" in text
    assert "`[short description](VIDEO_URL)` from `video_url`" in text
    assert "`[short description](AUDIO_URL)` from `audio_url`" in text
    assert "embed every image, each on its own line" in text
    assert "copying the URL verbatim" in text
    assert "never paste base64" in text


def test_default_describes_what_the_chat_renders():
    text = DEFAULT_SECURITY_INSTRUCTIONS
    assert "Tables, task lists, strikethrough" in text
    assert "A single newline is a line break" in text
    assert "Math, Mermaid and footnotes are not rendered" in text


def test_default_chat_instructions_describe_html_creation_and_sandbox_limits():
    text = DEFAULT_SECURITY_INSTRUCTIONS
    assert "/api/files/by-path?path=URL_ENCODED_FULL_PATH" in text
    for rule in (
        "one self-contained .html under 2 MiB",
        "link above outside code fences, once per file",
        "Inline frame fits content",
        "no outer cards, page backgrounds, vh or 100% heights",
        "Prefer app theme CSS vars with standalone fallbacks",
        "var(--foreground, #111)",
        "--background, --muted-foreground, --border, --primary, --chart-1 to --chart-5",
        "Revisions: same path, relink",
        "No screenshot/HTML code block substitutes",
        "Sandboxed: embed CSS and JavaScript",
        "inline SVG/data URLs, system fonts, client-side data",
        "No external resources, sibling files, network requests, forms, popups or storage",
        "If dev server/backend needed, say so; give run steps",
    ):
        assert rule in text, rule


# ---------------------------------------------------------------------------
# Who gets the chat instructions. They describe how the chat renders a reply;
# a Task/Swarm worker's reply goes to the agent that started it instead.
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    from app.core import provider_agent

    from tests.pg_shim import new_sqlite_connection, route_db_to

    conn = new_sqlite_connection()
    provider_agent.reset_worker_sessions()
    with route_db_to(conn):
        yield conn
    provider_agent.reset_worker_sessions()
    conn.close()


def _system_text(session_id):
    import app.core.query_options as qo

    opts, _ = qo.build_options_from_profile(
        profile={"id": "t", "config": {"model": "opus", "tools_preset": "claude_code"}},
        session_id=session_id,
    )
    prompt = opts.system_prompt
    return prompt.get("append", "") if isinstance(prompt, dict) else str(prompt or "")


def _go_live(session_id, source):
    from app.db import runs as runs_db
    from app.db import turns as turns_db

    turn = turns_db.enqueue_turn(session_id, source, prompt="x")
    run = runs_db.claim_run(session_id, "runner-b", 60)
    turns_db.start_turn(turn["id"], run["id"], "runner-b")


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["native", "delegated", "antigravity"])
async def test_a_top_level_chat_gets_the_shipped_prompt(db, tmp_path, monkeypatch, leg):
    from app.core import antigravity
    from app.core.provider import harness_adapter as ha
    from app.core.query_options import WORKER_REPORT_INSTRUCTIONS
    from app.core.sdk_options import build_session_options
    from app.db import database, mobile_tool_identity
    from fluid.engine.acp.loop import AcpAgentSpec

    # An isolated checkout boundary keeps instruction discovery out of the
    # machine's installation and other projects. No external provider is run.
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    model = {"native": "gpt-5", "delegated": "opus", "antigravity": "gemini-3.1-pro"}[leg]
    kind = {"native": "openai_responses", "delegated": "delegated", "antigravity": ha._ANTIGRAVITY_KIND}[leg]
    monkeypatch.setattr(ha, "_decide_provider_kind", lambda _: kind)
    if leg == "native":
        monkeypatch.setattr(ha, "_build_provider_client", AsyncMock(return_value=(object(), None)))
    elif leg == "delegated":
        monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: ["unused-claude"])
    else:
        monkeypatch.setattr(database, "resolve_model_provider", lambda *_, **__: {"id": "test-acp"})
        monkeypatch.setattr(antigravity, "find_binary", lambda: "unused-agy")
        monkeypatch.setattr(antigravity, "build_spec", lambda *_, **__: AcpAgentSpec(argv=["unused-agy"]))
        monkeypatch.setattr(antigravity, "register_agent", lambda *_: None)

    mobile_tool_identity.init_schema()  # A separate migration, not in the shim's base schema.
    profile = database.create_profile("t", "Test agent", None, {
        "model": model, "tools_preset": "claude_code",
        "system_prompt": {"type": "preset", "preset": "claude_code"},
        "cwd": str(tmp_path),
    })
    database.create_session("human-1", profile["id"], derive_provider_from_profile=False)
    _go_live("human-1", "user")
    options, _ = build_session_options(profile=profile, session_id="human-1")
    if leg == "antigravity":
        options.aih_provider_id = "test-acp"
    adapter = ha.HarnessAdapter()
    await adapter.connect_with_sdk_options(options)
    try:
        engine = adapter._engine
        if leg == "native":
            assert engine.system_sections is not None  # Not the fallback string.
            text = engine.system
        elif leg == "delegated":
            # The compiled prompt replaces Claude Code's (--system-prompt).
            assert engine.append_system_prompt is None
            text = engine.system
        else:
            text = engine.system_append
        _assert_disk_sharing_rules(text)
        assert text.count(DEFAULT_SECURITY_INSTRUCTIONS.strip()) == 1
        assert WORKER_REPORT_INSTRUCTIONS not in text
    finally:
        await adapter.disconnect()


def test_a_worker_gets_the_report_note_instead(db):
    from app.core import provider_agent
    from app.core.query_options import WORKER_REPORT_INSTRUCTIONS

    provider_agent.register_worker_session("worker-1")
    text = _system_text("worker-1")
    assert "## Chat display" not in text
    assert WORKER_REPORT_INSTRUCTIONS in text
    assert len(WORKER_REPORT_INSTRUCTIONS) < len(DEFAULT_SECURITY_INSTRUCTIONS) // 10


def test_a_worker_turn_claimed_by_another_runner_gets_the_report_note(db):
    from app.core import provider_agent

    _go_live("worker-x", provider_agent.WORKER_TURN_SOURCE)
    assert provider_agent.is_worker_session("worker-x") is False
    assert "## Chat display" not in _system_text("worker-x")


def test_a_worker_chat_a_human_types_into_gets_the_chat_instructions(db):
    # After a restart the in-process worker set is empty; the live turn is the
    # owner's, so the reply is shown in the chat.
    _go_live("worker-y", "user")
    assert "## Chat display" in _system_text("worker-y")
