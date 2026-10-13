"""AI Shuffle's editable chat instructions (stored as ``security_instructions``).

The default text tells agents what the chat renders and how to link files so
the file API serves them; these tests pin the rules that keep links working.
"""
import re
from unittest.mock import patch
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


def test_default_states_where_links_work_and_what_to_do_otherwise():
    text = DEFAULT_SECURITY_INSTRUCTIONS
    # The served roots (app.api.common.require_file_access).
    assert "inside your working directory, the workspace or a registered project directory" in text
    assert "copy it into your working directory and link the copy, never the original" in text
    assert "Write the file before linking it" in text
    assert "never secrets or credential files" in text
    assert "never a bare filesystem path" in text


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
    for tool in ("generateImage", "editImage", "generateWithReference", "generateVideo",
                 "imageToVideo", "extendVideo", "generateAudio", "audioToAudio"):
        assert tool in text
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
    assert "outside code fences" in text
    assert "embed CSS and JavaScript" in text
    assert "2 MiB" in text
    assert "network requests" in text
    assert "HTML code block" in text
    assert "screenshot" in text


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


def test_a_top_level_chat_gets_the_chat_instructions(db):
    from app.core.query_options import WORKER_REPORT_INSTRUCTIONS

    _go_live("human-1", "user")
    text = _system_text("human-1")
    assert DEFAULT_SECURITY_INSTRUCTIONS.strip() in text
    assert WORKER_REPORT_INSTRUCTIONS not in text


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
