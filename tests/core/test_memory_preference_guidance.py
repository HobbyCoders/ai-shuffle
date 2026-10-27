"""The memory-correction and when-line rule rides each leg exactly once.

Native: Remember's eager description carries it. Delegated: Remember's docs
are deferred, so a line beside the memory block carries it instead. Neither
leg pays for both copies.
"""
from __future__ import annotations

import pytest

import app.core.provider.harness_adapter as ha
from scripts.measure_prompt_catalog import compile_fixture

PHRASE = "not a new dated rule"
WHEN_EXAMPLE = "When choosing a model for a subagent"


@pytest.fixture(autouse=True)
def _emulator_tools_listed(monkeypatch):
    monkeypatch.setattr("app.mcp.emulator_tools.emulator_tools_supported", lambda: True)


def test_native_leg_carries_the_rule_once_in_remember():
    compiled, catalog, _ = compile_fixture(native=True)
    system = "\n\n".join(s.text for s in compiled.sections if s.kind != "tools")
    remember = next(text for text, name in catalog if name == "Remember")
    assert ha.DELEGATED_PREFERENCE_RULE not in system
    assert "pref.<topic>" not in system and "pref.<topic>" not in remember
    assert remember.count(PHRASE) == 1 and remember.count(WHEN_EXAMPLE) == 1
    assert "replaces" in remember and "always" in remember


def test_delegated_leg_carries_the_rule_once_beside_the_memory_block():
    compiled, _, _ = compile_fixture(native=False)
    text = compiled.text
    assert text.count(ha.DELEGATED_PREFERENCE_RULE) == 1
    assert text.index(ha._MEMORY_BLOCK_CORE) < text.index(ha.DELEGATED_PREFERENCE_RULE)
    assert text.count(WHEN_EXAMPLE) == 1 and "pref.<topic>" not in text
    assert "always" in ha.DELEGATED_PREFERENCE_RULE
