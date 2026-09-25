"""Resource-limit tests for the jq shim: runaway programs and deeply nested
input must fail fast with a clear error instead of hanging or crashing."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.core.jq_shim import evaluator, jq
from app.core.jq_shim.evaluator import (
    EvaluationError,
    LimitError,
    UnsupportedError,
    evaluate,
)
from app.core.jq_shim.parser import (
    MAX_AST_DEPTH,
    MAX_PARSE_DEPTH,
    ASTNode,
    NodeType,
    ParseError,
    parse_jq_expression,
)

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent


def nested_list(depth: int) -> list:
    root: list = []
    current = root
    for _ in range(depth):
        child: list = []
        current.append(child)
        current = child
    return root


class TestStepAndTimeLimits:
    def test_unbounded_recurse_hits_default_limits(self):
        # Whichever default limit trips first (step count, or wall time under
        # a slow tracer such as coverage), it must be a clean LimitError.
        start = time.monotonic()
        with pytest.raises(LimitError, match="step limit|time limit"):
            jq("[recurse(.)]", 1)
        assert time.monotonic() - start < 60

    def test_step_limit_is_configurable(self, monkeypatch):
        monkeypatch.setattr(evaluator, "MAX_STEPS", 1_000)
        with pytest.raises(LimitError, match=r"step limit \(1000\)"):
            jq("[.[] | tostring]", list(range(10_000)))
        # Small programs stay well under it
        assert jq("[.[] | tostring]", [1, 2]) == ["1", "2"]

    def test_time_limit(self, monkeypatch):
        monkeypatch.setattr(evaluator, "MAX_STEPS", 10**12)
        monkeypatch.setattr(evaluator, "MAX_SECONDS", 0.2)
        start = time.monotonic()
        with pytest.raises(LimitError, match="time limit"):
            jq("[recurse(.)]", 1)
        assert time.monotonic() - start < 10

    def test_counters_reset_per_evaluation(self, monkeypatch):
        monkeypatch.setattr(evaluator, "MAX_STEPS", 50)
        for _ in range(5):
            assert jq("[.[] | . == 1]", [1, 2, 3]) == [True, False, False]

    def test_optional_does_not_swallow_limit(self, monkeypatch):
        monkeypatch.setattr(evaluator, "MAX_STEPS", 1_000)
        with pytest.raises(LimitError):
            jq("[recurse(.)]?", 1)
        with pytest.raises(LimitError):
            jq("[.[] | [recurse(.)]?]", [1])

    def test_alternative_does_not_swallow_limit(self, monkeypatch):
        monkeypatch.setattr(evaluator, "MAX_STEPS", 1_000)
        with pytest.raises(LimitError):
            jq("[recurse(.)] // 0", 1)

    def test_limit_error_is_an_evaluation_error(self):
        # Callers that catch EvaluationError keep working
        assert issubclass(LimitError, EvaluationError)

    def test_long_arrays_stay_fast(self):
        big = list(range(200_000))
        start = time.monotonic()
        assert jq("[.[] | select(. > 5)] | length", big) == 199_994
        # unique was O(n^2) before; it now sorts
        assert jq("unique | length", big + big) == 200_000
        assert jq("group_by(. > 100) | map(length)", big) == [101, 199_899]
        assert jq("sort | first", big[::-1]) == 0
        assert time.monotonic() - start < 30


class TestPathologicalProgramsAreRejected:
    """Unbounded generators are not implemented, so they fail at compile time."""

    @pytest.mark.parametrize(
        "program",
        [
            "[range(1e7)]",
            "[range(0; 10000000)]",
            "until(false; .)",
            "[repeat(.)]",
            "[limit(10; repeat(1))]",
            "[while(true; .)]",
            "[.[] | range(.)]",
            "input",
            "[inputs]",
        ],
    )
    def test_unsupported_generators(self, program):
        start = time.monotonic()
        with pytest.raises(UnsupportedError, match="is not defined"):
            jq(program, [1])
        assert time.monotonic() - start < 1

    @pytest.mark.parametrize(
        "program",
        ["def f: f; f", "reduce range(1e9) as $x (0; .)", "[.[] as $x | $x]"],
    )
    def test_unsupported_syntax(self, program):
        with pytest.raises(ParseError, match="unsupported jq syntax"):
            parse_jq_expression(program)


class TestNestingLimits:
    def test_deep_parentheses(self):
        program = "(" * 5000 + "." + ")" * 5000
        with pytest.raises(ParseError, match="nested too deeply"):
            parse_jq_expression(program)

    def test_deep_array_construction(self):
        depth = MAX_PARSE_DEPTH + 5
        with pytest.raises(ParseError, match="nested too deeply"):
            parse_jq_expression("[" * depth + "]" * depth)

    def test_nesting_just_under_limit_parses(self):
        depth = MAX_PARSE_DEPTH - 2
        assert jq("[" * depth + "1" + "]" * depth, None) == json.loads(
            "[" * depth + "1" + "]" * depth
        )

    def test_deep_prefix_operators(self):
        with pytest.raises(ParseError, match="nested too deeply"):
            parse_jq_expression("not " * 5000 + ".")
        with pytest.raises(ParseError, match="nested too deeply"):
            parse_jq_expression("- " * 5000 + "1")

    def test_long_path_chain(self):
        with pytest.raises(ParseError, match="AST depth"):
            parse_jq_expression(".a" * (MAX_AST_DEPTH + 10))
        assert jq(".a" * 50, {}) is None

    @pytest.mark.parametrize(
        "program", ["walk(.)", ". == .", "sort", "flatten", "[.] | unique"]
    )
    def test_deeply_nested_input(self, program):
        with pytest.raises(LimitError, match="nested too deeply"):
            jq(program, nested_list(5000))

    def test_iterative_builtins_handle_deep_input(self):
        # .. and recurse are iterative, so depth alone is not an error
        assert jq("[..] | length", nested_list(5000)) == 5001

    def test_eval_depth_backstop_for_hand_built_ast(self, monkeypatch):
        monkeypatch.setattr(evaluator, "MAX_EVAL_DEPTH", 20)
        node = ASTNode(NodeType.IDENTITY)
        for _ in range(50):
            node = ASTNode(NodeType.PIPE, children=[node, ASTNode(NodeType.IDENTITY)])
        with pytest.raises(LimitError, match="nested too deeply"):
            evaluate(node, 1)


def _run_cli(*args, input_data=""):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["JQ_SHIM_NO_FALLBACK"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "app.core.jq_shim", *args],
        input=input_data,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env=env,
        timeout=120,
    )


class TestCliLimits:
    def test_deeply_nested_input_is_a_clean_error(self):
        result = _run_cli(".", input_data="[" * 100_000 + "]" * 100_000)
        assert result.returncode == 2
        assert "nested too deeply" in result.stderr
        assert "Traceback" not in result.stderr

    def test_runaway_program_is_a_clean_error(self):
        result = _run_cli("-n", "1 | [recurse(.)]")
        assert result.returncode == 5
        assert "step limit" in result.stderr
        assert "Traceback" not in result.stderr

    def test_unsupported_generator_without_fallback(self):
        result = _run_cli("-n", "[range(1e7)]")
        assert result.returncode == 3
        assert "range/1 is not defined" in result.stderr
