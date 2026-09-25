"""Shared pieces of the jsonl_parser parse-cache tests.

Import explicitly. ``setup_function``/``teardown_function`` are pytest module
hooks: importing them into a test module clears the parse cache around every
test function in it.
"""
from app.core import jsonl_parser as jp


def setup_function(_func):
    jp.invalidate_parse_cache()


def teardown_function(_func):
    jp.invalidate_parse_cache()


def sdk_entries(pairs):
    """Build minimal SDK user/assistant entries from (user_text, asst_text) pairs."""
    out = []
    for i, (u, a) in enumerate(pairs):
        out.append(
            {
                "type": "user",
                "message": {"role": "user", "content": u},
                "uuid": f"u{i}",
                "timestamp": f"2024-01-01T00:00:{i * 2:02d}Z",
            }
        )
        out.append(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": a}],
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                    "model": "claude-x",
                },
                "uuid": f"a{i}",
                "timestamp": f"2024-01-01T00:00:{i * 2 + 1:02d}Z",
            }
        )
    return out
