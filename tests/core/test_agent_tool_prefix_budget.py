"""Bound the requested tool-contract growth without making deferred tools eager."""
import json
from pathlib import Path

import pytest

from scripts.measure_prompt_catalog import compile_fixture, measure

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures" / "agent_tool_prefix_budget.json").read_text())


@pytest.mark.parametrize("native", [True, False], ids=["native", "delegated"])
def test_agent_tool_prefix_size_and_deferred_session_view(native):
    result = measure(native=native)
    mode = "native" if native else "delegated"
    for field, baseline in FIXTURE[mode].items():
        assert result["totals"][field] <= baseline * (100 + FIXTURE["allowance_percent"]) // 100
    name = "SessionView" if native else "mcp__builtins__SessionView"
    assert result["tools"][name] <= FIXTURE["SessionView"] * 101 // 100
    compiled, catalog, specs = compile_fixture(native=native)
    spec = next(spec for spec in specs if spec.name == name)
    # The new paging instructions belong to the deferred tool, not every call.
    assert "text_offset" not in compiled.text
    assert "text_offset" in spec.input_schema["properties"]
    paragraphs = [paragraph.strip() for paragraph in spec.description.split("\n\n") if paragraph.strip()]
    assert len(paragraphs) == len(set(paragraphs))
    assert spec.description.count("Text previews") == 1
    assert sum(tool_name == name for _, tool_name in catalog) == 1
