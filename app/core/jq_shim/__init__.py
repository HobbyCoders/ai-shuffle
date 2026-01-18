"""
Python jq shim - intercepts jq calls from shell scripts.

This module provides a Python implementation of common jq operations,
eliminating the need for external jq installation. It's designed to
support the subset of jq used by Claude Code plugin hooks.

Usage from Python:
    from app.core.jq_shim import jq
    result = jq('.field', {'field': 'value'})

Usage from shell (via shim scripts):
    echo '{"a": 1}' | jq '.a'
    jq -r '.name' file.json
"""

from .parser import parse_jq_expression, ASTNode, NodeType, ParseError
from .evaluator import evaluate, EvaluationError, EMPTY


def jq(
    expression: str,
    data: any,
    variables: dict = None,
    raw_output: bool = False,
) -> any:
    """
    Evaluate a jq expression against JSON data.

    Args:
        expression: jq filter expression (e.g., '.field', '.[] | .name')
        data: Input JSON data (Python dict, list, or scalar)
        variables: Optional dict of $name -> value bindings
        raw_output: If True and result is string, return without quotes

    Returns:
        The result of the jq expression evaluation.

    Example:
        >>> jq('.name', {'name': 'Alice', 'age': 30})
        'Alice'
        >>> jq('.items[0]', {'items': [1, 2, 3]})
        1
        >>> jq('.field // "default"', {})
        'default'
    """
    ast = parse_jq_expression(expression)
    result = evaluate(ast, data, variables or {})

    # Handle EMPTY sentinel
    if result is EMPTY:
        return None

    return result


__all__ = [
    "jq",
    "parse_jq_expression",
    "evaluate",
    "ASTNode",
    "NodeType",
    "ParseError",
    "EvaluationError",
    "EMPTY",
]
