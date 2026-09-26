"""Built-in subagent seeding behavior (moved from test_subagents.py when the
/subagents API was hard-renamed away in P2b — the seeding logic lives on in
app.core.builtin_subagents over the unified agents table)."""

from unittest.mock import patch


_CHS_DEF = {
    "id": "chat-history-summarizer",
    "name": "Chat History Summarizer",
    "description": "Summarizes chat history",
    "prompt": "Summarizer prompt",
    "tools": ["Read", "Grep", "Glob"],
    "model": "sonnet",
}


class TestBuiltinSubagentSeeding:
    """Test the seed_builtin_subagents function behavior."""

    def test_seed_creates_builtin_when_not_exists(self):
        """Seeding creates built-in subagent when it doesn't exist."""
        from app.core.builtin_subagents import seed_builtin_subagents

        with patch("app.core.builtin_subagents.database") as mock_db, \
             patch("app.core.builtin_subagents.BUILTIN_SUBAGENTS", [_CHS_DEF]):
            mock_db.get_subagent.return_value = None
            mock_db.create_subagent.return_value = {
                "id": "chat-history-summarizer",
                "is_builtin": True
            }

            results = seed_builtin_subagents()

            assert results.get("chat-history-summarizer") == "created"
            mock_db.create_subagent.assert_called_once()
            # Verify is_builtin=True was passed
            call_kwargs = mock_db.create_subagent.call_args[1]
            assert call_kwargs["is_builtin"] is True
            assert call_kwargs["store_defaults"] is True

    def test_seed_promotes_non_builtin_to_builtin(self):
        """Seeding promotes subagent to built-in if it exists with is_builtin=False."""
        from app.core.builtin_subagents import seed_builtin_subagents

        # Subagent exists but is_builtin is False (the bug condition)
        existing_subagent = {
            "id": "chat-history-summarizer",
            "name": "Old Name",
            "is_builtin": False,  # Bug: should be True
            "default_prompt": None
        }

        with patch("app.core.builtin_subagents.database") as mock_db, \
             patch("app.core.builtin_subagents.BUILTIN_SUBAGENTS", [_CHS_DEF]):
            mock_db.get_subagent.return_value = existing_subagent

            results = seed_builtin_subagents()

            assert results.get("chat-history-summarizer") == "promoted"
            # Verify set_subagent_builtin was called to fix the flag
            mock_db.set_subagent_builtin.assert_called_once_with("chat-history-summarizer", True)

    def test_seed_existing_builtin_without_defaults_is_left_alone(self):
        """v71: defaults live in code — a builtin row missing the old
        default_prompt column needs no 'repair' pass any more."""
        from app.core.builtin_subagents import seed_builtin_subagents

        existing_subagent = {
            "id": "chat-history-summarizer",
            "name": "Chat History Summarizer",
            "is_builtin": True,
        }

        with patch("app.core.builtin_subagents.database") as mock_db, \
             patch("app.core.builtin_subagents.BUILTIN_SUBAGENTS", [_CHS_DEF]):
            mock_db.get_subagent.return_value = existing_subagent

            results = seed_builtin_subagents()

            assert results.get("chat-history-summarizer") == "exists"
            mock_db.create_subagent.assert_not_called()

    def test_seed_skips_existing_builtin_with_defaults(self):
        """Seeding skips properly configured built-in subagents."""
        from app.core.builtin_subagents import seed_builtin_subagents

        # Properly configured built-in — defaults match the code definition,
        # so seeding should detect no change and report "exists".
        existing_subagent = {
            "id": "chat-history-summarizer",
            "name": "Chat History Summarizer",
            "is_builtin": True,
            "default_prompt": _CHS_DEF["prompt"],
            "default_model": _CHS_DEF["model"],
        }

        with patch("app.core.builtin_subagents.database") as mock_db, \
             patch("app.core.builtin_subagents.BUILTIN_SUBAGENTS", [_CHS_DEF]):
            mock_db.get_subagent.return_value = existing_subagent

            results = seed_builtin_subagents()

            assert results.get("chat-history-summarizer") == "exists"
            # No modifications should be made
            mock_db.set_subagent_builtin.assert_not_called()
            mock_db.create_subagent.assert_not_called()
