"""P1.5 (ruling A11): the profile's `memory` config field is injected into the
system prompt on every shape — preset (append field, so the delegated CLI sees
it) and custom/string (folded into the string the omni compiler treats as
base+blocks)."""

from unittest.mock import patch

import app.core.query_options as qo


def _profile(config=None):
    cfg = {"model": "opus", "tools_preset": "claude_code"}
    if config:
        cfg.update(config)
    return {"id": "t", "config": cfg}


def _build(config):
    with patch.object(qo, "get_security_instructions", return_value="SEC"):
        opts, _ = qo.build_options_from_profile(profile=_profile(config))
    return opts.system_prompt


def test_memory_injected_into_preset_append():
    sp = _build({
        "system_prompt": {"type": "preset", "preset": "claude_code"},
        "memory": "User prefers terse answers.",
    })
    assert isinstance(sp, dict)
    assert "# Agent memory" in sp["append"]
    assert "User prefers terse answers." in sp["append"]


def test_memory_injected_into_custom_prompt():
    sp = _build({
        "system_prompt": {"type": "custom", "content": "I am an agent."},
        "memory": "Project X ships Friday.",
    })
    assert isinstance(sp, str)
    assert "I am an agent." in sp
    assert "# Agent memory" in sp
    assert "Project X ships Friday." in sp


def test_no_memory_no_block():
    sp = _build({"system_prompt": {"type": "preset", "preset": "claude_code"}})
    assert "# Agent memory" not in sp["append"]
    sp2 = _build({
        "system_prompt": {"type": "custom", "content": "Agent."},
        "memory": "   ",
    })
    assert "# Agent memory" not in sp2


def test_memory_mentions_write_path():
    sp = _build({
        "system_prompt": {"type": "custom", "content": "Agent."},
        "memory": "fact",
    })
    assert "Memory tool" in sp
