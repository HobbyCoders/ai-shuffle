"""Clock policy costs one small fixed sentence; no volatile fact in the prefix."""
import json
from pathlib import Path

from fluid.context.time_awareness import RELATIVE_TIME_RULE
from fluid.context.environment import environment_block
from scripts.measure_prompt_catalog import compile_fixture
from scripts.measure_time_awareness_prefix import duplicate_paragraphs, render


def test_owner_code_prefix_budget():
    fixture = json.loads((Path(__file__).parents[1] / "fixtures/time_prefix_budget.json").read_text())
    for leg, result in render().items():
        budget = fixture[leg]
        assert result["compiled_eager"]["chars"] <= budget["compiled_eager_chars"] + budget["allowance_chars"]


def test_no_duplicate_policy_paragraph_or_clock_in_fixed_prefix():
    env = environment_block("/fixture", include_volatile=False)
    compiled, _, _ = compile_fixture(environment_text=env)
    assert compiled.text.count(RELATIVE_TIME_RULE) == 1
    assert "Current time:" not in compiled.text and "Owner's previous message:" not in compiled.text
    assert duplicate_paragraphs(env) == []
    assert duplicate_paragraphs(compiled.text) == []
