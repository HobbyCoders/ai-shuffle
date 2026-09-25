"""
Python jq shim - intercepts jq calls from shell scripts.

This module provides a Python implementation of common jq operations,
eliminating the need for external jq installation. It's designed to
support the subset of jq used by Claude Code plugin hooks.

hook_service puts bin/ first on PATH for plugin hooks. bin/jq (bash) and
bin/jq.bat (cmd) embed a generated copy of parser.py, evaluator.py and
__main__.py; tests/core/test_jq_shim/test_embedded_sync.py fails when the
copies drift from these modules.

Usage from Python:
    from app.core.jq_shim import jq
    result = jq('.field', {'field': 'value'})

Usage from shell (via shim scripts):
    echo '{"a": 1}' | jq '.a'
    jq -r '.name' file.json

Supported subset
----------------
Paths ``.a``, ``."a-b"``, ``.["a"]``, ``.[n]`` (negative and fractional
indices), ``.a[.i]``, ``.[]``, ``..``, optional ``?``; ``|``, ``,``,
parentheses; literals and string interpolation (``\\(...)``, JSON escapes
including ``\\uXXXX``); ``==  !=  <  <=  >  >=`` (jq's total order across
types); ``and``/``or``/``not``; ``//``; ``-expr``; ``$var``, ``$ENV``,
``$__loc__``; array construction and object construction with identifier,
string, interpolated and computed ``(expr)`` keys and ``{a}``/``{$a}``
shorthand; and the builtins listed in ``evaluator.BUILTIN_ARITIES``.

Not supported (a ParseError/UnsupportedError at compile time): arithmetic
operators, ``if``, ``try``/``catch``, ``reduce``/``foreach``, ``as`` bindings,
``def``, ``label``, slices, ``@format`` strings, assignment operators, and
every builtin not in ``BUILTIN_ARITIES`` (``range``, ``until``, ``limit``,
``paths``, ...). The CLI hands such programs, and options it does not
implement (``-R``, ``--indent``, ...), to the next real ``jq`` on PATH when
there is one (set ``JQ_SHIM_NO_FALLBACK=1`` to disable), so hooks keep
working wherever jq is installed.

Semantic differences from jq 1.7
--------------------------------
- Generators (``.[]``, ``..``, ``,``, ``recurse``) are only fanned out by
  ``|``, ``[...]``, ``,``, ``?``, a ``term[...]`` target and the first argument
  of ``map``, ``map_values``, ``with_entries``, ``first``, ``last``,
  ``recurse``, ``sort_by``, ``group_by``, ``any`` and ``all``. Elsewhere (object values, string interpolation, operands of
  comparisons/``and``/``or``/``//``, other function arguments) they are
  rejected at compile time instead of producing jq's cartesian products.
- ``.[]`` on null yields nothing (like ``.[]?``); jq raises an error.
- ``not f`` is accepted as prefix negation (``(f | not)``); jq reads it as
  ``not | f``. ``f | not`` works as in jq.
- ``recurse(f)`` drops null children, so ``recurse(.a)`` terminates where jq
  would emit ``null`` forever. Bare ``recurse``/``..`` match jq exactly.
- ``from_entries``/``with_entries`` accept ``k``/``v``/``Value`` entries,
  stringify non-string keys and skip entries whose key is null; jq 1.7
  raises for those.
- An empty program is the identity (jq 1.6 behaviour; jq 1.7 errors).
- Trailing commas are tolerated in ``[...]``, ``{...}`` and ``(...)``.
- Regexes use Python's ``re`` (``(?<name>...)`` is translated), not
  Oniguruma; the ``g`` flag is supported for ``test``/``sub``/``gsub`` only.
  A regex with catastrophic backtracking is not interrupted by the limits.
- ``tonumber`` rejects ``"nan"``; numbers are printed the way Python's json
  module prints them (``1e3`` is ``1000.0``, ``infinite`` is ``Infinity``).
- ``min``/``max`` return null for non-arrays; ``reverse`` reverses strings
  (jq 1.7 errors).
- ``?`` around a generator discards the outputs it produced before an error
  (``[(.[] | .a)?]`` on ``[{"a":1},"s"]`` is ``[]``; jq gives ``[1]``).
- The CLI evaluates each input completely before printing, so an error
  suppresses that input's earlier outputs.
- Error messages follow jq's wording but not its exact text.

Resource limits (evaluator.MAX_STEPS, MAX_SECONDS, MAX_EVAL_DEPTH; parser
MAX_PARSE_DEPTH, MAX_AST_DEPTH) turn runaway programs and deeply nested input
into a LimitError / ParseError; ``?`` and ``//`` never swallow a LimitError.
"""

from .parser import parse_jq_expression, ASTNode, NodeType, ParseError
from .evaluator import (
    evaluate,
    validate,
    is_stream,
    EvaluationError,
    UnsupportedError,
    LimitError,
    EMPTY,
)


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
    "UnsupportedError",
    "LimitError",
    "validate",
    "is_stream",
    "EMPTY",
]
