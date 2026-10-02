"""Recognized-secret export filtering is opt-in and non-mutating."""
import copy
import json
from urllib.parse import quote

import pytest

from app.core.export_redaction import (
    redact_external_export, redact_export_jsonl, redact_export_text,
)

# Synthetic, non-minted fixture: no installation key or real token involved.
PREVIEW = "v1.c3ludGhldGlj.1790942400." + "a" * 64


@pytest.mark.parametrize("encode", [
    lambda s: s,
    lambda s: quote(s, safe="").replace(".", "%2E"),
    lambda s: quote(quote(s, safe="").replace(".", "%2E"), safe=""),
    lambda s: "".join("%%%02X" % ord(c) for c in s),
    lambda s: "".join("\\u%04x" % ord(c) for c in s),
    lambda s: "".join("&#%d;" % ord(c) for c in s),
    lambda s: s.replace(".", "&period;"),
])
def test_preview_token_in_url_and_standalone_encoded_text(encode):
    token = encode(PREVIEW)
    original = "keep%20this https://worker/api/v1/auth/preview-access?token=" + token + "&next=/ " + token
    assert redact_export_text(original) == (
        "keep%20this https://worker/api/v1/auth/preview-access?token=***&next=/ ***"
    )


@pytest.mark.parametrize("text,expected", [
    ("postgres://user:fixture-pw@db:5432/app", "postgres://***@db:5432/app"),
    ("https://fixture-token@host/repo", "https://***@host/repo"),
    ("https://u:pw@host/?token=" + PREVIEW, "https://***@host/?token=***"),
    ("https%3A%2F%2Fu%3Aencoded-pw%40host%2Frepo", "https://***@host%2Frepo"),
    ("Authorization: Bearer fixture-auth", "Authorization: Bearer ***"),
    ("Authorization: Basic Zml4dHVyZTpwdw==", "Authorization: Basic ***"),
    ('API_KEY="fixture key with spaces"', 'API_KEY="***"'),
    ('{"password":"fixture-pw","ok":1}', '{"password":"***","ok":1}'),
    ("sk-proj-" + "s" * 25, "***"),
    ("ssh://u:fixture-pw@host/repo", "ssh://***@host/repo"),
    ("https://host?access_token=fixture-token&next=/", "https://host?access_token=***&next=/"),
    (r'password="fixture\"secret" keep', 'password="***" keep'),
    ("-----BEGIN PRIVATE KEY-----\nfixture-not-a-key\n-----END PRIVATE KEY-----", "***"),
])
def test_narrow_recognized_text_credentials(text, expected):
    assert redact_export_text(text) == expected


def test_structure_and_useful_content_survive_without_mutation():
    data = {
        "content": "notes " + PREVIEW,
        "nested": [{"apiKey": "fixture-key", "Authorization": "opaque-auth",
                    "cookie": "fixture-cookie", "password": "fixture-password"}],
        "tool_error": {"url": "https://u:fixture-pw@host/path"},
        "total_tokens_in": 42, "token_count": 7, "tokenizer": "example",
        "key " + PREVIEW: "value",
    }
    before = copy.deepcopy(data)
    filtered = redact_external_export(data)
    assert data == before
    assert filtered["nested"] == [{"apiKey": "***", "Authorization": "***",
                                   "cookie": "***", "password": "***"}]
    assert filtered["total_tokens_in"] == 42 and filtered["token_count"] == 7
    assert filtered["tokenizer"] == "example" and filtered["key ***"] == "value"
    assert PREVIEW not in json.dumps(filtered)


@pytest.mark.parametrize("text", [
    "git@github.com:HobbyCoders/ai-shuffle.git",
    "ssh://git@github.com/HobbyCoders/ai-shuffle.git",
    "http://localhost:8080/api version v1.2.3",
    "Budget token count 42; arbitrary fixture prose is not a recognized credential",
    "keep%20this \\u002e &#46;",
    "a" * 100_000,
])
def test_unrecognized_content_is_unchanged(text):
    assert redact_export_text(text) == text


@pytest.mark.parametrize("bad", ['{broken', '{}\nnot-json', '[]', 'null', '{"x": NaN}', '{"x": Infinity}'])
def test_jsonl_fails_closed(bad):
    with pytest.raises(ValueError):
        redact_export_jsonl(bad)


def test_jsonl_parses_then_redacts_structured_fields_and_escaped_text():
    raw = json.dumps({"message": {"content": PREVIEW, "api_key": "fixture-secret"},
                      "metadata": {"ok": True}}) + "\n\n"
    safe = redact_export_jsonl(raw)
    assert json.loads(safe) == {"message": {"content": "***", "api_key": "***"},
                                "metadata": {"ok": True}}
    assert "fixture-secret" in raw
