"""Tests for the jq expression parser."""

import pytest
from app.core.jq_shim.parser import (
    parse_jq_expression, ASTNode, NodeType, ParseError
)


class TestBasicParsing:
    """Test basic parsing functionality."""

    def test_parse_identity(self):
        """Should parse identity expression."""
        ast = parse_jq_expression(".")
        assert ast.type == NodeType.IDENTITY

    def test_parse_field_access(self):
        """Should parse simple field access."""
        ast = parse_jq_expression(".field")
        assert ast.type == NodeType.FIELD
        assert ast.value == "field"

    def test_parse_nested_field(self):
        """Should parse nested field access as a pipe."""
        ast = parse_jq_expression(".a.b.c")
        # .a.b.c parses as ((.a).b).c which is a chain of pipes
        assert ast.type == NodeType.PIPE

    def test_parse_array_index(self):
        """Should parse array indexing."""
        ast = parse_jq_expression(".[0]")
        assert ast.type == NodeType.INDEX

    def test_parse_negative_index(self):
        """Should parse negative array index."""
        ast = parse_jq_expression(".[-1]")
        assert ast.type == NodeType.INDEX

    def test_parse_iterate(self):
        """Should parse array iteration."""
        ast = parse_jq_expression(".[]")
        assert ast.type == NodeType.ITERATE


class TestPipeAndDefault:
    """Test pipe and default value parsing."""

    def test_parse_pipe(self):
        """Should parse pipe expression."""
        ast = parse_jq_expression(".a | .b")
        assert ast.type == NodeType.PIPE

    def test_parse_default(self):
        """Should parse default value expression."""
        ast = parse_jq_expression('.field // ""')
        assert ast.type == NodeType.DEFAULT

    def test_parse_default_with_empty(self):
        """Should parse default with empty."""
        ast = parse_jq_expression(".field // empty")
        assert ast.type == NodeType.DEFAULT

    def test_parse_chained_pipes(self):
        """Should parse chained pipes."""
        ast = parse_jq_expression(".a | .b | .c")
        assert ast.type == NodeType.PIPE


class TestLiterals:
    """Test literal value parsing."""

    def test_parse_number_int(self):
        """Should parse integer number."""
        ast = parse_jq_expression("42")
        assert ast.type == NodeType.NUMBER
        assert ast.value == 42

    def test_parse_number_negative(self):
        """Should parse negative number."""
        ast = parse_jq_expression("-42")
        assert ast.type == NodeType.NUMBER
        assert ast.value == -42

    def test_parse_number_float(self):
        """Should parse float number."""
        ast = parse_jq_expression("3.14")
        assert ast.type == NodeType.NUMBER
        assert ast.value == 3.14

    def test_parse_string(self):
        """Should parse string literal."""
        ast = parse_jq_expression('"hello"')
        assert ast.type == NodeType.STRING
        assert ast.value == "hello"

    def test_parse_string_with_escapes(self):
        """Should parse string with escapes."""
        ast = parse_jq_expression('"hello\\nworld"')
        assert ast.type == NodeType.STRING
        assert ast.value == "hello\nworld"

    def test_parse_bool_true(self):
        """Should parse true."""
        ast = parse_jq_expression("true")
        assert ast.type == NodeType.BOOL
        assert ast.value is True

    def test_parse_bool_false(self):
        """Should parse false."""
        ast = parse_jq_expression("false")
        assert ast.type == NodeType.BOOL
        assert ast.value is False

    def test_parse_null(self):
        """Should parse null."""
        ast = parse_jq_expression("null")
        assert ast.type == NodeType.NULL


class TestStringInterpolation:
    """Test string interpolation parsing."""

    def test_parse_string_interpolation(self):
        """Should parse string with interpolation."""
        ast = parse_jq_expression(r'"Hello \(.name)!"')
        assert ast.type == NodeType.STRING
        # Value should be a list with string parts and AST nodes
        assert isinstance(ast.value, list)
        assert len(ast.value) == 3
        assert ast.value[0] == "Hello "
        assert isinstance(ast.value[1], ASTNode)
        assert ast.value[2] == "!"


class TestVariables:
    """Test variable parsing."""

    def test_parse_variable(self):
        """Should parse variable reference."""
        ast = parse_jq_expression("$name")
        assert ast.type == NodeType.VARIABLE
        assert ast.value == "name"

    def test_parse_variable_in_expression(self):
        """Should parse variable in object construction."""
        ast = parse_jq_expression("{name: $value}")
        assert ast.type == NodeType.OBJECT


class TestObjectConstruction:
    """Test object construction parsing."""

    def test_parse_simple_object(self):
        """Should parse simple object construction."""
        ast = parse_jq_expression("{a: .b}")
        assert ast.type == NodeType.OBJECT
        assert len(ast.value) == 1
        key, value = ast.value[0]
        assert key == "a"
        assert value.type == NodeType.FIELD

    def test_parse_multi_field_object(self):
        """Should parse object with multiple fields."""
        ast = parse_jq_expression("{a: .x, b: .y, c: .z}")
        assert ast.type == NodeType.OBJECT
        assert len(ast.value) == 3


class TestArrayConstruction:
    """Test array construction parsing."""

    def test_parse_empty_array(self):
        """Should parse empty array."""
        ast = parse_jq_expression("[]")
        assert ast.type == NodeType.ARRAY
        assert len(ast.children) == 0

    def test_parse_array_with_elements(self):
        """Should parse array with elements."""
        ast = parse_jq_expression("[1, 2, 3]")
        assert ast.type == NodeType.ARRAY
        assert len(ast.children) == 3


class TestFunctions:
    """Test function call parsing."""

    def test_parse_select(self):
        """Should parse select function."""
        ast = parse_jq_expression("select(.x > 0)")
        assert ast.type == NodeType.FUNCTION
        assert ast.value == "select"

    def test_parse_map(self):
        """Should parse map function."""
        ast = parse_jq_expression("map(.name)")
        assert ast.type == NodeType.FUNCTION
        assert ast.value == "map"

    def test_parse_keys(self):
        """Should parse keys function."""
        ast = parse_jq_expression("keys")
        assert ast.type == NodeType.FUNCTION
        assert ast.value == "keys"

    def test_parse_length(self):
        """Should parse length function."""
        ast = parse_jq_expression("length")
        assert ast.type == NodeType.FUNCTION
        assert ast.value == "length"


class TestComparisons:
    """Test comparison operator parsing."""

    def test_parse_equality(self):
        """Should parse equality comparison."""
        ast = parse_jq_expression(".x == 1")
        assert ast.type == NodeType.COMPARISON
        assert ast.value == "=="

    def test_parse_inequality(self):
        """Should parse inequality comparison."""
        ast = parse_jq_expression(".x != 0")
        assert ast.type == NodeType.COMPARISON
        assert ast.value == "!="

    def test_parse_less_than(self):
        """Should parse less than comparison."""
        ast = parse_jq_expression(".x < 10")
        assert ast.type == NodeType.COMPARISON
        assert ast.value == "<"

    def test_parse_greater_than(self):
        """Should parse greater than comparison."""
        ast = parse_jq_expression(".x > 0")
        assert ast.type == NodeType.COMPARISON
        assert ast.value == ">"


class TestLogicalOperators:
    """Test logical operator parsing."""

    def test_parse_and(self):
        """Should parse and operator."""
        ast = parse_jq_expression(".a and .b")
        assert ast.type == NodeType.LOGICAL
        assert ast.value == "and"

    def test_parse_or(self):
        """Should parse or operator."""
        ast = parse_jq_expression(".a or .b")
        assert ast.type == NodeType.LOGICAL
        assert ast.value == "or"

    def test_parse_not(self):
        """Should parse not operator."""
        ast = parse_jq_expression("not .a")
        assert ast.type == NodeType.LOGICAL
        assert ast.value == "not"


class TestParentheses:
    """Test parenthesized expressions."""

    def test_parse_parenthesized(self):
        """Should parse parenthesized expression."""
        # Use a simple parenthesized expression (not arithmetic since we don't support +)
        ast = parse_jq_expression("(.a | .b)")
        # Should just return the inner expression
        assert ast is not None
        assert ast.type == NodeType.PIPE


class TestParseErrors:
    """Test parse error handling."""

    def test_error_unclosed_string(self):
        """Should error on unclosed string."""
        with pytest.raises(ParseError):
            parse_jq_expression('"unclosed')

    def test_error_unclosed_bracket(self):
        """Should error on unclosed bracket."""
        with pytest.raises(ParseError):
            parse_jq_expression("[1, 2")

    def test_error_invalid_char(self):
        """Should error on invalid character."""
        with pytest.raises(ParseError):
            parse_jq_expression("@invalid")


class TestRealWorldPatterns:
    """Test patterns found in real Claude Code hooks."""

    def test_tool_input_command(self):
        """Pattern: .tool_input.command // \"\""""
        ast = parse_jq_expression('.tool_input.command // ""')
        assert ast.type == NodeType.DEFAULT

    def test_tool_name_extraction(self):
        """Pattern: .tool_name"""
        ast = parse_jq_expression(".tool_name")
        assert ast.type == NodeType.FIELD
        assert ast.value == "tool_name"

    def test_object_construction_pattern(self):
        """Pattern: {tool: .tool_name, path: .tool_input.file_path}"""
        ast = parse_jq_expression("{tool: .tool_name, path: .tool_input.file_path}")
        assert ast.type == NodeType.OBJECT

    def test_select_type_pattern(self):
        """Pattern: select(.type == \"file\")"""
        ast = parse_jq_expression('select(.type == "file")')
        assert ast.type == NodeType.FUNCTION
        assert ast.value == "select"
