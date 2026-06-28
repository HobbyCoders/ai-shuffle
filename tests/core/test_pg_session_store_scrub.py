"""
Unit tests for ``_scrub_pg_illegal`` in :mod:`app.core.pg_session_store`.

Postgres ``text``/``json``/``jsonb`` cannot store U+0000 (NUL) or lone UTF-16
surrogates (U+D800–U+DFFF). The SDK occasionally hands the mirror an ``append``
whose transcript entry carries such a code point (e.g. a tool result that
echoed a binary or UTF-16-encoded file). Before the scrub, ``psycopg2.Json``
serialized it verbatim and the INSERT failed with
``unsupported Unicode escape sequence: <0x00> cannot be converted to text``,
which the SDK surfaced to the chat as a ``mirror_error`` system message.

These are pure-function tests — no Postgres, no SDK subprocess, no DB pool.
"""

from __future__ import annotations

from app.core.pg_session_store import _scrub_pg_illegal


def test_clean_string_passes_through_identically():
    s = "ordinary text — emoji 🚀, accents café, CJK 日本語"
    out = _scrub_pg_illegal(s)
    assert out == s
    # Hot path returns the SAME object (no needless copy) when nothing matched.
    assert out is s


def test_nul_is_replaced_with_fffd():
    out = _scrub_pg_illegal("before\x00after")
    assert "\x00" not in out
    assert out == "before�after"


def test_multiple_nuls_all_replaced():
    out = _scrub_pg_illegal("\x00a\x00b\x00")
    assert "\x00" not in out
    assert out == "�a�b�"


def test_lone_surrogate_is_replaced():
    # A lone high surrogate — what surrogateescape decoding of invalid UTF-8
    # bytes produces. jsonb rejects it the same way it rejects NUL.
    out = _scrub_pg_illegal("x\ud83dx")
    assert out == "x�x"


def test_nested_dict_and_list_are_scrubbed_recursively():
    entry = {
        "uuid": "11111111-2222-3333-4444-555555555555",
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": "tail ===\x00binary\x00junk"},
                {"type": "text", "text": "clean"},
            ],
        },
        "tags": ["ok", "bad\x00tag"],
    }
    out = _scrub_pg_illegal(entry)

    # uuid (and any id field) never contains illegal points -> untouched, so the
    # drift verifier that compares entry->>'uuid' stays green.
    assert out["uuid"] == entry["uuid"]
    assert out["message"]["content"][0]["text"] == "tail ===�binary�junk"
    assert out["message"]["content"][1]["text"] == "clean"
    assert out["tags"] == ["ok", "bad�tag"]
    # No NUL survives anywhere in the serialized structure.
    import json

    assert "\x00" not in json.dumps(out)


def test_nul_in_dict_key_is_scrubbed():
    out = _scrub_pg_illegal({"good": 1, "ba\x00d": 2})
    assert set(out.keys()) == {"good", "ba�d"}
    assert out["ba�d"] == 2


def test_non_string_scalars_pass_through():
    assert _scrub_pg_illegal(42) == 42
    assert _scrub_pg_illegal(3.14) == 3.14
    assert _scrub_pg_illegal(True) is True
    assert _scrub_pg_illegal(None) is None


def test_real_world_utf16_log_tail_shape():
    # Mirrors the exact failure that surfaced in the UI: a tool result whose
    # text echoed a UTF-16 (BOM + NUL-laden) log file. � already present
    # (the decoded BOM) is valid for jsonb and must be left as-is; only the raw
    # NUL needs replacing.
    text = "=== _e2e_run.log tail ===\n��p\x00\x00o\x00r\x00t"
    out = _scrub_pg_illegal({"text": text})
    assert "\x00" not in out["text"]
    # Pre-existing replacement chars are preserved (not double-mangled).
    assert out["text"].startswith("=== _e2e_run.log tail ===\n��p�")
