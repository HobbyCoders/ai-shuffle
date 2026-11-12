"""Batched ChatHttpRequest: how a call is read, saved rules, and the bridged schema.

The broker path and the owner gate are covered in test_chat_services.py and
test_own_consent_tools.py.
"""

import json

import pytest

from app.core import chat_http_batch
from app.core.chat_files import AccessDenied
from app.core.permission_handler import PermissionDecision, PermissionRule

TOOL = "mcp__chat-access__ChatHttpRequest"
GET = {"grant_id": "g", "method": "GET", "path": "/zones"}
POST = {"grant_id": "g", "method": "POST", "path": "/zones/1/purge", "body": "{}"}


@pytest.mark.parametrize("args", [
    {**GET},
    {**GET, "requests": None},
    {**GET, "requests": ""},
    {**GET, "requests": "  "},
    {**GET, "requests": []},
    {**GET, "requests": [{"grant_id": "", "method": "", "path": "", "body": None}]},
    {"requests": None},
])
def test_single_form(args):
    assert chat_http_batch.items(args) is None


def test_batch_form_ignores_blank_top_level_fields_and_reads_json_text():
    blank = {"grant_id": "", "method": " ", "path": "", "body": "", "content_type": None}
    assert chat_http_batch.items({**blank, "requests": [GET, POST]}) == [GET, POST]
    assert chat_http_batch.items({"requests": json.dumps([GET])}) == [GET]


@pytest.mark.parametrize("args,needle", [
    ({"requests": []}, "1 to 8"),
    ({"requests": "[not json"}, "1 to 8"),
    ({"requests": "{}"}, "1 to 8"),
    ({"requests": 3}, "1 to 8"),
    ({"requests": [GET] * 9}, "1 to 8"),
    ({"requests": [GET, None]}, "1 to 8"),
    ({"method": "GET", "requests": [POST]}, "method"),
    ({"grant_id": "g", "path": "/x", "requests": [GET]}, "grant_id, path"),
])
def test_malformed_or_mixed_batches_are_refused(args, needle):
    with pytest.raises(AccessDenied, match=needle):
        chat_http_batch.items(args)


def test_eight_items_is_the_limit():
    assert len(chat_http_batch.items({"requests": [GET] * chat_http_batch.LIMIT})) == 8


def _rule(decision, pattern):
    return PermissionRule(id="r", session_id=None, profile_id=None, tool_name=TOOL,
                          tool_pattern=pattern, decision=decision)


def test_a_saved_deny_pattern_cannot_be_dodged_by_batching():
    deny = _rule(PermissionDecision.DENY, "*/purge")
    assert deny.matches(TOOL, POST)  # the single request, as before
    assert deny.matches(TOOL, {"requests": [GET, POST]})
    assert deny.matches(TOOL, {"requests": json.dumps([GET, POST])})
    assert not deny.matches(TOOL, {"requests": [GET, GET]})
    # A malformed batch runs nothing; only a Deny claims it.
    assert deny.matches(TOOL, {"method": "GET", "requests": [POST]})


def test_a_saved_allow_pattern_must_cover_every_request():
    allow = _rule(PermissionDecision.ALLOW, "/zones*")
    assert allow.matches(TOOL, GET)
    assert allow.matches(TOOL, {"requests": [GET, {**GET, "path": "/zones/2"}]})
    assert not allow.matches(TOOL, {"requests": [GET, {**GET, "path": "/accounts"}]})
    assert not allow.matches(TOOL, {"method": "GET", "requests": [GET]})
    # A pattern-free rule still covers every call of the tool.
    assert _rule(PermissionDecision.ALLOW, None).matches(TOOL, {"requests": [POST]})


def test_the_schema_survives_the_fluid_bridge_and_string_lists_are_coerced():
    from fluid.tools.hardening import coerce_args
    from fluid.tools.registry import ToolRegistry

    from app.core.provider.fluid_tool_bridge import (
        register_sdk_tools,
        sdk_tool_to_inproc,
    )
    from app.core.provider.sdk_tool_records import recorded_sdk_server_tools
    from app.mcp.chat_access_sdk import create_chat_access_server

    tools = {t.name: t for t in recorded_sdk_server_tools(create_chat_access_server("chat"))}
    schema = sdk_tool_to_inproc(tools["ChatHttpRequest"]).input_schema
    assert schema["type"] == "object" and "required" not in schema
    batch = schema["properties"]["requests"]
    assert batch["type"] == "array" and batch["maxItems"] == chat_http_batch.LIMIT
    assert set(batch["items"]["properties"]) == set(chat_http_batch.FIELDS)
    assert set(schema["properties"]) == {*chat_http_batch.FIELDS, "requests"}
    # Providers that send the list as JSON text get a list before the handler.
    assert coerce_args({"requests": json.dumps([GET])}, schema)["requests"] == [GET]
    registry = ToolRegistry()
    register_sdk_tools(registry, "chat-access", list(tools.values()))
    registered = registry.get(TOOL)
    assert registered is not None and registered.input_schema["properties"]["requests"] == batch
