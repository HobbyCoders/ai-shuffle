"""Tests for the jq expression evaluator."""

import pytest
from app.core.jq_shim.parser import parse_jq_expression
from app.core.jq_shim.evaluator import evaluate, EvaluationError, EMPTY


class TestBasicEvaluation:
    """Test basic evaluation functionality."""

    def test_identity(self):
        """Identity should return input unchanged."""
        ast = parse_jq_expression(".")
        assert evaluate(ast, {"a": 1}) == {"a": 1}

    def test_identity_with_list(self):
        """Identity should work with lists."""
        ast = parse_jq_expression(".")
        assert evaluate(ast, [1, 2, 3]) == [1, 2, 3]

    def test_identity_with_null(self):
        """Identity should work with null."""
        ast = parse_jq_expression(".")
        assert evaluate(ast, None) is None


class TestFieldAccess:
    """Test field access evaluation."""

    def test_field_access(self):
        """Should access object fields."""
        ast = parse_jq_expression(".field")
        assert evaluate(ast, {"field": "value"}) == "value"

    def test_nested_field(self):
        """Should access nested fields."""
        ast = parse_jq_expression(".a.b.c")
        data = {"a": {"b": {"c": 42}}}
        assert evaluate(ast, data) == 42

    def test_missing_field_returns_none(self):
        """Missing field should return None."""
        ast = parse_jq_expression(".missing")
        assert evaluate(ast, {"other": 1}) is None

    def test_field_on_null_returns_none(self):
        """Field access on null should return None."""
        ast = parse_jq_expression(".field")
        assert evaluate(ast, None) is None


class TestArrayIndexing:
    """Test array indexing evaluation."""

    def test_array_index(self):
        """Should index arrays."""
        ast = parse_jq_expression(".[1]")
        assert evaluate(ast, [1, 2, 3]) == 2

    def test_negative_index(self):
        """Should handle negative indices."""
        ast = parse_jq_expression(".[-1]")
        assert evaluate(ast, [1, 2, 3]) == 3

    def test_out_of_bounds_returns_none(self):
        """Out of bounds index should return None."""
        ast = parse_jq_expression(".[10]")
        assert evaluate(ast, [1, 2, 3]) is None


class TestIteration:
    """Test array/object iteration."""

    def test_array_iterate(self):
        """Should iterate over arrays."""
        ast = parse_jq_expression(".[]")
        assert evaluate(ast, [1, 2, 3]) == [1, 2, 3]

    def test_object_iterate(self):
        """Should iterate over object values."""
        ast = parse_jq_expression(".[]")
        result = evaluate(ast, {"a": 1, "b": 2})
        assert set(result) == {1, 2}

    def test_iterate_null(self):
        """Iterate on null should return empty list."""
        ast = parse_jq_expression(".[]")
        assert evaluate(ast, None) == []


class TestPipes:
    """Test pipe evaluation."""

    def test_simple_pipe(self):
        """Should pipe through expressions."""
        ast = parse_jq_expression(".a | .b")
        data = {"a": {"b": "value"}}
        assert evaluate(ast, data) == "value"

    def test_pipe_with_iterate(self):
        """Should pipe through iteration."""
        ast = parse_jq_expression(".[] | .name")
        data = [{"name": "a"}, {"name": "b"}]
        assert evaluate(ast, data) == ["a", "b"]


class TestDefaults:
    """Test default value evaluation."""

    def test_default_value(self):
        """Should use default when field is null."""
        ast = parse_jq_expression('.missing // "default"')
        assert evaluate(ast, {}) == "default"

    def test_default_not_used_when_present(self):
        """Should not use default when field exists."""
        ast = parse_jq_expression('.field // "default"')
        assert evaluate(ast, {"field": "actual"}) == "actual"

    def test_default_with_false(self):
        """False should trigger default."""
        ast = parse_jq_expression('.field // "default"')
        assert evaluate(ast, {"field": False}) == "default"

    def test_default_with_empty_string(self):
        """Empty string should not trigger default."""
        ast = parse_jq_expression('.field // "default"')
        assert evaluate(ast, {"field": ""}) == ""

    def test_default_with_zero(self):
        """Zero should not trigger default."""
        ast = parse_jq_expression(".field // 999")
        assert evaluate(ast, {"field": 0}) == 0


class TestVariables:
    """Test variable substitution."""

    def test_variable_substitution(self):
        """Should substitute variables."""
        ast = parse_jq_expression("$name")
        assert evaluate(ast, {}, {"name": "value"}) == "value"

    def test_undefined_variable_error(self):
        """Should error on undefined variable."""
        ast = parse_jq_expression("$undefined")
        with pytest.raises(EvaluationError):
            evaluate(ast, {})


class TestObjectConstruction:
    """Test object construction evaluation."""

    def test_simple_object(self):
        """Should construct objects."""
        ast = parse_jq_expression("{a: .x, b: .y}")
        data = {"x": 1, "y": 2}
        assert evaluate(ast, data) == {"a": 1, "b": 2}

    def test_object_with_variable(self):
        """Should construct objects with variables."""
        ast = parse_jq_expression("{name: $var}")
        assert evaluate(ast, {}, {"var": "value"}) == {"name": "value"}


class TestArrayConstruction:
    """Test array construction evaluation."""

    def test_array_construction(self):
        """Should construct arrays."""
        ast = parse_jq_expression("[.a, .b, .c]")
        data = {"a": 1, "b": 2, "c": 3}
        assert evaluate(ast, data) == [1, 2, 3]


class TestStringInterpolation:
    """Test string interpolation evaluation."""

    def test_string_interpolation(self):
        """Should interpolate strings."""
        ast = parse_jq_expression(r'"Hello \(.name)!"')
        assert evaluate(ast, {"name": "World"}) == "Hello World!"

    def test_string_interpolation_null(self):
        """Should handle null in interpolation."""
        ast = parse_jq_expression(r'"\(.missing)"')
        assert evaluate(ast, {}) == "null"


class TestComparisons:
    """Test comparison evaluation."""

    def test_equality_true(self):
        """Should evaluate equality to true."""
        ast = parse_jq_expression(".x == 1")
        assert evaluate(ast, {"x": 1}) is True

    def test_equality_false(self):
        """Should evaluate equality to false."""
        ast = parse_jq_expression(".x == 1")
        assert evaluate(ast, {"x": 2}) is False

    def test_less_than(self):
        """Should evaluate less than."""
        ast = parse_jq_expression(".x < 10")
        assert evaluate(ast, {"x": 5}) is True
        assert evaluate(ast, {"x": 15}) is False

    def test_greater_than(self):
        """Should evaluate greater than."""
        ast = parse_jq_expression(".x > 0")
        assert evaluate(ast, {"x": 5}) is True
        assert evaluate(ast, {"x": -1}) is False


class TestLogicalOperators:
    """Test logical operator evaluation."""

    def test_and_true(self):
        """Should evaluate and to true."""
        ast = parse_jq_expression(".a and .b")
        assert evaluate(ast, {"a": True, "b": True}) is True

    def test_and_false(self):
        """Should evaluate and to false."""
        ast = parse_jq_expression(".a and .b")
        assert evaluate(ast, {"a": True, "b": False}) is False

    def test_or_true(self):
        """Should evaluate or to true."""
        ast = parse_jq_expression(".a or .b")
        assert evaluate(ast, {"a": False, "b": True}) is True

    def test_not(self):
        """Should evaluate not."""
        ast = parse_jq_expression("not .a")
        assert evaluate(ast, {"a": True}) is False
        assert evaluate(ast, {"a": False}) is True


class TestBuiltinFunctions:
    """Test built-in function evaluation."""

    def test_select_true(self):
        """select() should return data when predicate is true."""
        ast = parse_jq_expression("select(.x > 0)")
        assert evaluate(ast, {"x": 5}) == {"x": 5}

    def test_select_false(self):
        """select() should return EMPTY when predicate is false."""
        ast = parse_jq_expression("select(.x > 0)")
        assert evaluate(ast, {"x": -1}) is EMPTY

    def test_map(self):
        """map() should apply expression to each element."""
        ast = parse_jq_expression("map(.name)")
        data = [{"name": "a"}, {"name": "b"}]
        assert evaluate(ast, data) == ["a", "b"]

    def test_keys(self):
        """keys should return sorted object keys."""
        ast = parse_jq_expression("keys")
        assert evaluate(ast, {"b": 1, "a": 2, "c": 3}) == ["a", "b", "c"]

    def test_values(self):
        """values should return object values."""
        ast = parse_jq_expression("values")
        result = evaluate(ast, {"a": 1, "b": 2})
        assert set(result) == {1, 2}

    def test_length_string(self):
        """length should return string length."""
        ast = parse_jq_expression("length")
        assert evaluate(ast, "hello") == 5

    def test_length_array(self):
        """length should return array length."""
        ast = parse_jq_expression("length")
        assert evaluate(ast, [1, 2, 3]) == 3

    def test_length_object(self):
        """length should return object key count."""
        ast = parse_jq_expression("length")
        assert evaluate(ast, {"a": 1, "b": 2}) == 2

    def test_type(self):
        """type should return type name."""
        assert evaluate(parse_jq_expression("type"), None) == "null"
        assert evaluate(parse_jq_expression("type"), True) == "boolean"
        assert evaluate(parse_jq_expression("type"), 42) == "number"
        assert evaluate(parse_jq_expression("type"), "hi") == "string"
        assert evaluate(parse_jq_expression("type"), []) == "array"
        assert evaluate(parse_jq_expression("type"), {}) == "object"

    def test_empty(self):
        """empty should produce no output."""
        ast = parse_jq_expression("empty")
        assert evaluate(ast, {"a": 1}) is EMPTY

    def test_has(self):
        """has() should check for key existence."""
        ast = parse_jq_expression('has("a")')
        assert evaluate(ast, {"a": 1}) is True
        assert evaluate(ast, {"b": 1}) is False

    def test_contains(self):
        """contains() should check for containment."""
        ast = parse_jq_expression('contains("ell")')
        assert evaluate(ast, "hello") is True
        assert evaluate(ast, "world") is False

    def test_add_numbers(self):
        """add should sum numbers."""
        ast = parse_jq_expression("add")
        assert evaluate(ast, [1, 2, 3]) == 6

    def test_add_strings(self):
        """add should concatenate strings."""
        ast = parse_jq_expression("add")
        assert evaluate(ast, ["a", "b", "c"]) == "abc"

    def test_reverse(self):
        """reverse should reverse arrays."""
        ast = parse_jq_expression("reverse")
        assert evaluate(ast, [1, 2, 3]) == [3, 2, 1]

    def test_sort(self):
        """sort should sort arrays."""
        ast = parse_jq_expression("sort")
        assert evaluate(ast, [3, 1, 2]) == [1, 2, 3]

    def test_unique(self):
        """unique should remove duplicates."""
        ast = parse_jq_expression("unique")
        assert evaluate(ast, [1, 2, 1, 3, 2]) == [1, 2, 3]

    def test_first(self):
        """first should return first element."""
        ast = parse_jq_expression("first")
        assert evaluate(ast, [1, 2, 3]) == 1

    def test_last(self):
        """last should return last element."""
        ast = parse_jq_expression("last")
        assert evaluate(ast, [1, 2, 3]) == 3

    def test_min(self):
        """min should return minimum."""
        ast = parse_jq_expression("min")
        assert evaluate(ast, [3, 1, 2]) == 1

    def test_max(self):
        """max should return maximum."""
        ast = parse_jq_expression("max")
        assert evaluate(ast, [3, 1, 2]) == 3

    def test_floor(self):
        """floor should round down."""
        ast = parse_jq_expression("floor")
        assert evaluate(ast, 3.7) == 3

    def test_ceil(self):
        """ceil should round up."""
        ast = parse_jq_expression("ceil")
        assert evaluate(ast, 3.2) == 4

    def test_round(self):
        """round should round to nearest."""
        ast = parse_jq_expression("round")
        assert evaluate(ast, 3.5) == 4
        assert evaluate(ast, 3.4) == 3

    def test_abs(self):
        """abs should return absolute value."""
        ast = parse_jq_expression("abs")
        assert evaluate(ast, -5) == 5
        assert evaluate(ast, 5) == 5

    def test_tostring(self):
        """tostring should convert to string."""
        ast = parse_jq_expression("tostring")
        assert evaluate(ast, 42) == "42"
        assert evaluate(ast, True) == "true"
        assert evaluate(ast, None) == "null"

    def test_tonumber(self):
        """tonumber should convert to number."""
        ast = parse_jq_expression("tonumber")
        assert evaluate(ast, "42") == 42
        assert evaluate(ast, "3.14") == 3.14

    def test_ascii_downcase(self):
        """ascii_downcase should lowercase."""
        ast = parse_jq_expression("ascii_downcase")
        assert evaluate(ast, "HELLO") == "hello"

    def test_ascii_upcase(self):
        """ascii_upcase should uppercase."""
        ast = parse_jq_expression("ascii_upcase")
        assert evaluate(ast, "hello") == "HELLO"

    def test_ltrimstr(self):
        """ltrimstr should trim prefix."""
        ast = parse_jq_expression('ltrimstr("hello ")')
        assert evaluate(ast, "hello world") == "world"

    def test_rtrimstr(self):
        """rtrimstr should trim suffix."""
        ast = parse_jq_expression('rtrimstr(" world")')
        assert evaluate(ast, "hello world") == "hello"

    def test_startswith(self):
        """startswith should check prefix."""
        ast = parse_jq_expression('startswith("hello")')
        assert evaluate(ast, "hello world") is True
        assert evaluate(ast, "world") is False

    def test_endswith(self):
        """endswith should check suffix."""
        ast = parse_jq_expression('endswith("world")')
        assert evaluate(ast, "hello world") is True
        assert evaluate(ast, "hello") is False

    def test_split(self):
        """split should split string."""
        ast = parse_jq_expression('split(",")')
        assert evaluate(ast, "a,b,c") == ["a", "b", "c"]

    def test_join(self):
        """join should join array."""
        ast = parse_jq_expression('join("-")')
        assert evaluate(ast, ["a", "b", "c"]) == "a-b-c"

    def test_test_regex(self):
        """test should match regex."""
        ast = parse_jq_expression('test("^hello")')
        assert evaluate(ast, "hello world") is True
        assert evaluate(ast, "world hello") is False

    def test_to_entries(self):
        """to_entries should convert object to entries."""
        ast = parse_jq_expression("to_entries")
        result = evaluate(ast, {"a": 1, "b": 2})
        assert len(result) == 2
        assert {"key": "a", "value": 1} in result
        assert {"key": "b", "value": 2} in result

    def test_from_entries(self):
        """from_entries should convert entries to object."""
        ast = parse_jq_expression("from_entries")
        data = [{"key": "a", "value": 1}, {"key": "b", "value": 2}]
        assert evaluate(ast, data) == {"a": 1, "b": 2}


class TestRealWorldPatterns:
    """Test patterns found in actual Claude Code hooks."""

    def test_bash_command_extraction(self):
        """Pattern: jq -r '.tool_input.command // \"\""""
        ast = parse_jq_expression('.tool_input.command // ""')
        data = {"tool_input": {"command": "npm install"}}
        assert evaluate(ast, data) == "npm install"

    def test_bash_command_missing(self):
        """Should return default when command missing."""
        ast = parse_jq_expression('.tool_input.command // ""')
        data = {"tool_input": {}}
        assert evaluate(ast, data) == ""

    def test_file_path_extraction(self):
        """Pattern: jq -r '.tool_input.file_path'"""
        ast = parse_jq_expression(".tool_input.file_path")
        data = {"tool_input": {"file_path": "/src/main.py"}}
        assert evaluate(ast, data) == "/src/main.py"

    def test_audit_log_construction(self):
        """Pattern from audit logging hooks."""
        ast = parse_jq_expression("{tool: .tool_name, command: .tool_input.command}")
        data = {"tool_name": "Bash", "tool_input": {"command": "git status"}}
        result = evaluate(ast, data)
        assert result == {"tool": "Bash", "command": "git status"}

    def test_empty_default(self):
        """Pattern: jq -r '.field // empty'"""
        ast = parse_jq_expression(".field // empty")
        data = {}
        result = evaluate(ast, data)
        assert result is EMPTY

    def test_tool_response_access(self):
        """PostToolUse pattern: jq -r '.tool_response'"""
        ast = parse_jq_expression(".tool_response")
        data = {"tool_response": "File written successfully"}
        assert evaluate(ast, data) == "File written successfully"

    def test_select_by_tool_name(self):
        """Pattern for filtering by tool name."""
        ast = parse_jq_expression('select(.tool_name == "Bash")')
        data_match = {"tool_name": "Bash"}
        data_no_match = {"tool_name": "Edit"}
        assert evaluate(ast, data_match) == data_match
        assert evaluate(ast, data_no_match) is EMPTY
