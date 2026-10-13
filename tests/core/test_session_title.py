"""Tests for session title derivation (leading @attachment token strip)."""

import pytest

from app.core.session_title import derive_session_title, strip_leading_attachment_tokens


LONG_REQUEST = (
    "Remove computer use section from the settings page, and also take out the "
    "global toggle wiring so the chat Stop button stops it again"
)


class TestStripLeadingAttachmentTokens:
    def test_message_without_token_is_unchanged(self):
        text = "Remove computer use section from settings"
        assert strip_leading_attachment_tokens(text) == text

    def test_message_without_token_is_unchanged_even_with_known_paths(self):
        text = "Remove computer use section from settings"
        assert strip_leading_attachment_tokens(text, ["/ws/a.png"]) == text

    def test_at_mention_mid_message_is_untouched(self):
        text = "email me @user about this"
        assert strip_leading_attachment_tokens(text) == text

    def test_reported_bug_shape_with_known_path(self):
        # The actual shape from the field report: path with spaces, exact
        # staged upload path available.
        token = "@/workspace/ai-shuffle/uploads/Screenshot 2026-08-30 233548.png"
        text = f"{token} {LONG_REQUEST}"
        assert strip_leading_attachment_tokens(
            text, [token[1:]]
        ) == LONG_REQUEST

    def test_single_segment_path(self):
        text = "@src/lib/api.ts fix the import"
        assert strip_leading_attachment_tokens(text) == "fix the import"

    def test_space_bearing_path_with_extension_segments(self):
        text = "@/workspace/repo/notes/meeting notes.md please summarise this file"
        assert (
            strip_leading_attachment_tokens(text) == "please summarise this file"
        )

    def test_user_mention_leading_is_stripped_but_prose_is_not_eaten(self):
        # Prose after a non-path token stops the consumption at the first word.
        text = "@user gave me notes.md to fix"
        assert strip_leading_attachment_tokens(text) == "gave me notes.md to fix"

    def test_multiple_tokens(self):
        text = "@/ws/a.png @/ws/b.png Fix the login bug"
        known = ["/ws/a.png", "/ws/b.png"]
        assert strip_leading_attachment_tokens(text, known) == "Fix the login bug"

    def test_multiple_tokens_via_heuristic(self):
        text = "@/ws/a.png @/ws/b.png Fix the login bug"
        assert strip_leading_attachment_tokens(text) == "Fix the login bug"

    def test_leading_whitespace_before_token(self):
        text = "\n\n  @/ws/a.png Fix the thing"
        assert strip_leading_attachment_tokens(text, ["/ws/a.png"]) == "Fix the thing"

    def test_attachment_only_message_is_not_emptied(self):
        text = "@/ws/a.png @/ws/b.png"
        assert strip_leading_attachment_tokens(text, ["/ws/a.png", "/ws/b.png"]) == text

    def test_attachment_only_message_without_known_paths(self):
        # The heuristic consumes the space-bearing name; nothing survives, so
        # the never-empty rule returns the original text.
        text = "@/ws/shot 1.png"
        assert strip_leading_attachment_tokens(text) == text

    def test_bare_at_sign_is_left_alone(self):
        text = "@"
        assert strip_leading_attachment_tokens(text) == text

    def test_known_path_that_does_not_match_falls_back_to_heuristic(self):
        text = "@/ws/a.png Fix the thing"
        assert strip_leading_attachment_tokens(text, ["/other/path.png"]) == "Fix the thing"

    def test_absolute_path_consumes_until_extension_segment(self):
        # The reported shape without staged paths: the space-bearing upload
        # name is unwrapped up to its extension-bearing segment.
        text = (
            "@/workspace/ai-shuffle/uploads/Screenshot 2026-08-30 233548.png "
            "Remove computer use section"
        )
        assert (
            strip_leading_attachment_tokens(text) == "Remove computer use section"
        )


class TestDeriveSessionTitle:
    def test_strips_token_then_truncates_on_word_boundary(self):
        token = "/workspace/ai-shuffle/uploads/Screenshot 2026-08-30 233548.png"
        text = f"@{token} {LONG_REQUEST}"
        title = derive_session_title(text, [token])
        assert title.startswith("Remove computer use section")
        assert title.endswith("...")
        assert len(title) <= 103

    def test_unchanged_message_truncates_on_word_boundary(self):
        title = derive_session_title("word " * 40)
        assert title.endswith("...")
        assert len(title) <= 103

    def test_legacy_50_char_shape_without_word_boundary(self):
        body = "word " * 20
        title = derive_session_title(body, max_chars=50, word_boundary=False)
        # The 50-char sites always stripped the cut before appending.
        assert title == body[:50].strip() + "..."

    def test_short_message_is_not_truncated(self):
        assert derive_session_title("Fix the login bug") == "Fix the login bug"

    def test_empty_message(self):
        assert derive_session_title("") == ""

    def test_whitespace_only_message(self):
        assert derive_session_title("   \n  ") == ""


@pytest.mark.parametrize(
    "text,expected",
    [
        ("plain request", "plain request"),
        ("@src/a.ts fix it", "fix it"),
        ("@/ws/shot 1.png request here", "request here"),
        ("@mid sentence", "sentence"),
    ],
)
def test_strip_matrix(text, expected):
    assert strip_leading_attachment_tokens(text) == expected
