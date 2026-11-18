"""The Python compatibility check mirrors the request each Node media tool
really builds. Both sides are pinned to tools/tests/fixtures/
generation-tool-requests.json (Node: tools/tests/generation-tool-requests.test.mjs),
so a routing change in one cannot drift silently from the other."""

import json
from pathlib import Path

import pytest

from app.core import generation_operations as ops
from app.services.fal_schema import ModelSchema, ParamField

FIXTURE = Path(__file__).resolve().parents[2] / "tools" / "tests" / "fixtures" / "generation-tool-requests.json"
CASES = json.loads(FIXTURE.read_text())["cases"]


def _schema(case: dict) -> ModelSchema:
    def prop(name):
        return {"type": "array", "items": {"type": "string"}} if name.endswith("_urls") else {"type": "string"}
    fields = [ParamField(name, type="array" if name.endswith("_urls") else "string", required=name in case["required"],
                         is_primary_text=name == "prompt") for name in case["declared"]]
    return ModelSchema(case["model"], fields, "prompt" if "prompt" in case["declared"] else None,
                       input_schema={"type": "object", "properties": {n: prop(n) for n in case["declared"]},
                                     "required": case["required"]})


@pytest.mark.parametrize("case", CASES, ids=[f"{c['tool']}:{c['model']}" for c in CASES])
def test_tool_request_matches_the_node_tool(case):
    op = ops.OPERATIONS[case["operation"]]
    assert case["tool"] in op.tools
    route = ops.tool_request(op, case["model"], set(case["declared"]))
    if case.get("refused"):
        assert isinstance(route, str)
        return
    assert route.endpoint == case["endpoint"]
    assert sorted(route.sends) == sorted(case["sends"])


@pytest.mark.parametrize("case", CASES, ids=[f"{c['tool']}:{c['model']}" for c in CASES])
def test_compatible_only_when_the_real_request_fits(case):
    problem = ops.input_problem(ops.OPERATIONS[case["operation"]], _schema(case))
    assert (problem is None) is case["compatible"], problem


def test_kontext_multi_is_refused_for_edit_and_plain_kontext_accepted():
    edit = ops.OPERATIONS["edit-image"]
    multi = next(c for c in CASES if c["model"] == "fal-ai/flux-pro/kontext/max/multi" and c["tool"] == "editImage")
    plain = next(c for c in CASES if c["model"] == "fal-ai/flux-pro/kontext")
    problem = ops.input_problem(edit, _schema(multi))
    assert problem == ("editImage sends its image in image_url, which this model does not take (it takes image_urls).")
    assert ops.input_problem(edit, _schema(plain)) is None


def test_every_media_tool_has_a_pinned_case():
    tools = {tool for op in ops.OPERATIONS.values() if not op.native for tool in op.tools}
    assert tools <= {c["tool"] for c in CASES}


def test_a_saved_option_fills_a_required_plain_field_but_never_a_file_input():
    edit = ops.OPERATIONS["edit-image"]
    case = {"model": "fal-ai/example/edit", "declared": ["prompt", "image_url", "style", "mask_url"],
            "required": ["prompt", "image_url", "style"]}
    assert ops.input_problem(edit, _schema(case)) == "This model needs style, which editImage does not send."
    assert ops.input_problem(edit, _schema(case), {"style": "anime"}) is None
    masked = {**case, "required": ["prompt", "image_url", "mask_url"]}
    assert "mask_url" in ops.input_problem(edit, _schema(masked), {"mask_url": "https://example.com/m.png"})
