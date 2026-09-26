"""
jq expression evaluator.

Evaluates AST nodes against JSON data with variable support.

Streams: jq filters can emit any number of values. This evaluator represents
the output of a *generator* node (see ``is_stream``: ``.[]``, ``..``,
``recurse`` and pipes/optionals containing them) as a Python list, and the
output of ``empty``/``select`` that yields nothing as the ``EMPTY`` sentinel.
Every other node yields exactly one value. ``validate`` rejects programs that
put a generator where the evaluator cannot fan it out (object values, string
interpolation, operands of comparisons, most function arguments) instead of
silently returning a wrong answer.

Resource limits: evaluation is bounded by ``MAX_STEPS`` node evaluations,
``MAX_SECONDS`` of wall time and ``MAX_EVAL_DEPTH`` nested evaluations, and
input nested deeper than the interpreter can recurse raises ``LimitError``
rather than crashing. ``?`` and ``//`` never swallow a ``LimitError``.
"""

import json
import math
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional
from .parser import ASTNode, NodeType


# Resource limits (module-level so tests and embedders can tune them).
MAX_STEPS = 5_000_000
MAX_SECONDS = 10.0
MAX_EVAL_DEPTH = 200


class EvaluationError(Exception):
    """Raised when evaluation fails."""

    pass


class UnsupportedError(EvaluationError):
    """Raised by validate() for programs the shim cannot evaluate faithfully."""

    pass


class LimitError(EvaluationError):
    """Raised when evaluation exceeds a resource limit."""

    pass


class _EmptyArgument(Exception):
    """Internal: a function argument produced no value, so the call yields nothing."""


class EmptyValue:
    """Sentinel for jq 'empty' - produces no output."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self):
        return "empty"


EMPTY = EmptyValue()


# Builtins the shim implements, with the arities it accepts.
BUILTIN_ARITIES: Dict[str, frozenset] = {
    name: frozenset(arities)
    for name, arities in {
        "empty": {0}, "select": {1}, "map": {1}, "map_values": {1},
        "keys": {0}, "keys_unsorted": {0}, "values": {0}, "length": {0},
        "type": {0}, "has": {1}, "in": {1}, "contains": {1}, "inside": {1},
        "add": {0}, "flatten": {0, 1}, "reverse": {0}, "sort": {0},
        "sort_by": {1}, "unique": {0}, "first": {0, 1}, "last": {0, 1},
        "nth": {1}, "min": {0}, "max": {0}, "floor": {0}, "ceil": {0},
        "round": {0}, "abs": {0}, "tostring": {0}, "tonumber": {0},
        "tojson": {0}, "fromjson": {0}, "not": {0}, "ascii_downcase": {0},
        "ascii_upcase": {0}, "ltrimstr": {1}, "rtrimstr": {1},
        "startswith": {1}, "endswith": {1}, "split": {1}, "join": {1},
        "test": {1, 2}, "match": {1, 2}, "capture": {1, 2}, "sub": {2, 3},
        "gsub": {2, 3}, "error": {0, 1}, "debug": {0}, "getpath": {1},
        "group_by": {1}, "any": {0, 1, 2}, "all": {0, 1, 2}, "from_entries": {0},
        "to_entries": {0}, "with_entries": {1}, "recurse": {0, 1},
        "walk": {1}, "env": {0}, "now": {0}, "infinite": {0}, "nan": {0},
        "isinfinite": {0}, "isnan": {0}, "isnormal": {0},
    }.items()
}

# Functions whose (first) argument may be a generator.
_STREAM_ARG_FUNCTIONS = frozenset(
    {"map", "map_values", "first", "last", "recurse", "with_entries",
     "sort_by", "group_by", "any", "all"}
)

_NAMED_GROUP_RE = re.compile(r"(?<!\\)\(\?<(?![=!])")
_NUMBER_RE = re.compile(r"\s*-?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?\s*")


def is_stream(node: ASTNode) -> bool:
    """True if the node is a generator whose (list) result is a stream of outputs."""
    if node.type == NodeType.ITERATE:
        return True
    if node.type == NodeType.PIPE:
        return is_stream(node.children[0]) or is_stream(node.children[1])
    if node.type == NodeType.OPTIONAL:
        return is_stream(node.children[0])
    if node.type == NodeType.COMMA:
        return True
    if node.type == NodeType.INDEX and len(node.children) > 1:
        return is_stream(node.children[1])
    if node.type == NodeType.FUNCTION and node.value == "recurse":
        return True
    return False


def validate(node: ASTNode) -> None:
    """
    Statically check a parsed program, like jq's compile step.

    Raises UnsupportedError for unknown functions / arities and for generators
    in positions the evaluator cannot fan out.
    """
    t = node.type
    if t == NodeType.FUNCTION:
        arities = BUILTIN_ARITIES.get(node.value)
        if arities is None or len(node.children) not in arities:
            raise UnsupportedError(
                f"{node.value}/{len(node.children)} is not defined"
            )
        for i, arg in enumerate(node.children):
            if is_stream(arg) and not (i == 0 and node.value in _STREAM_ARG_FUNCTIONS):
                _reject_stream(f"the arguments of {node.value}()")
            validate(arg)
        return
    if t in (NodeType.PIPE, NodeType.ARRAY, NodeType.OPTIONAL, NodeType.COMMA):
        for child in node.children:
            validate(child)
        return
    if t == NodeType.INDEX:
        # children: [index expression, optional target term]; only the target
        # may be a generator
        if is_stream(node.children[0]):
            _reject_stream("an index expression")
        for child in node.children:
            validate(child)
        return
    if t == NodeType.OBJECT:
        for key, value in node.value:
            for part in (key, value):
                if isinstance(part, ASTNode):
                    if is_stream(part):
                        _reject_stream("object construction")
                    validate(part)
        return
    if t == NodeType.STRING:
        if not isinstance(node.value, str):
            for part in node.value:
                if isinstance(part, ASTNode):
                    if is_stream(part):
                        _reject_stream("string interpolation")
                    validate(part)
        return
    for child in node.children:
        if is_stream(child):
            _reject_stream(f"a {t.value} expression")
        validate(child)


def _reject_stream(where: str) -> None:
    raise UnsupportedError(
        f"generators such as .[] are not supported inside {where}; "
        "collect them with [...] or map() first"
    )


def evaluate(
    node: ASTNode,
    data: Any,
    variables: Optional[Dict[str, Any]] = None,
) -> Any:
    """
    Evaluate an AST node against input data.

    Args:
        node: The AST node to evaluate
        data: The input JSON data
        variables: Dict of $name -> value bindings

    Returns:
        The result of evaluation. May return:
        - A single value
        - A list (for generators; see is_stream)
        - EMPTY sentinel (for no output)
    """
    variables = variables or {}
    evaluator = JQEvaluator(variables)
    try:
        validate(node)
        return evaluator.evaluate(node, data)
    except RecursionError:
        raise LimitError("input is nested too deeply to process") from None
    except OverflowError as e:
        raise EvaluationError(f"number out of range: {e}") from None
    except ValueError as e:
        # e.g. floor/round of nan
        raise EvaluationError(str(e)) from None


def tojson(value: Any) -> str:
    """Serialize like jq's tojson (compact, UTF-8)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


class JQEvaluator:
    """Evaluator for jq AST nodes."""

    def __init__(self, variables: Dict[str, Any]):
        self.variables = variables
        self._steps = 0
        self._depth = 0
        self._deadline = time.monotonic() + MAX_SECONDS

    def _tick(self) -> None:
        """Count one unit of work against the step and time limits."""
        self._steps += 1
        if self._steps > MAX_STEPS:
            raise LimitError(
                f"jq program exceeded the evaluation step limit ({MAX_STEPS})"
            )
        if self._steps % 1024 == 0 and time.monotonic() > self._deadline:
            raise LimitError(
                f"jq program exceeded the time limit ({MAX_SECONDS:g}s)"
            )

    def outputs(self, node: ASTNode, data: Any) -> List[Any]:
        """Evaluate a node and return its outputs as a list (0..n values)."""
        result = self.evaluate(node, data)
        if result is EMPTY:
            return []
        if is_stream(node):
            return result
        return [result]

    def evaluate(self, node: ASTNode, data: Any) -> Any:
        """Evaluate a node against data."""
        self._tick()
        method_name = f"_eval_{node.type.value}"
        method = getattr(self, method_name, None)
        if method is None:
            raise EvaluationError(f"Unsupported node type: {node.type}")
        self._depth += 1
        try:
            if self._depth > MAX_EVAL_DEPTH:
                raise LimitError(
                    f"jq evaluation nested too deeply (limit {MAX_EVAL_DEPTH})"
                )
            return method(node, data)
        finally:
            self._depth -= 1

    def _eval_identity(self, node: ASTNode, data: Any) -> Any:
        """Evaluate identity expression (.)"""
        return data

    def _eval_field(self, node: ASTNode, data: Any) -> Any:
        """Evaluate field access (.field)"""
        if data is None:
            return None
        if not isinstance(data, dict):
            raise EvaluationError(
                f"Cannot index {self._type_name(data)} with {json.dumps(node.value)}"
            )
        return data.get(node.value)

    def _eval_index(self, node: ASTNode, data: Any) -> Any:
        """Evaluate .[expr] or term[expr]; expr is evaluated against `.` in both."""
        index = self.evaluate(node.children[0], data)
        if index is EMPTY:
            return EMPTY
        if len(node.children) == 1:
            return self._index(data, index)
        target = node.children[1]
        if is_stream(target):
            return [self._index(item, index) for item in self.outputs(target, data)]
        value = self.evaluate(target, data)
        if value is EMPTY:
            return EMPTY
        return self._index(value, index)

    def _index(self, data: Any, index: Any) -> Any:
        """Index a value with a number or string key (jq semantics)."""
        if data is None and isinstance(index, (int, float, str)) and not isinstance(index, bool):
            return None

        if isinstance(index, (int, float)) and not isinstance(index, bool):
            if not isinstance(data, list):
                raise EvaluationError(
                    f"Cannot index {self._type_name(data)} with number"
                )
            if isinstance(index, float) and not math.isfinite(index):
                return None
            index = math.floor(index)
            if index < 0:
                index = len(data) + index
            if 0 <= index < len(data):
                return data[index]
            return None
        elif isinstance(index, str):
            if not isinstance(data, dict):
                raise EvaluationError(
                    f"Cannot index {self._type_name(data)} with string {json.dumps(index)}"
                )
            return data.get(index)
        else:
            raise EvaluationError(
                f"Cannot index {self._type_name(data)} with {self._type_name(index)}"
            )

    def _eval_iterate(self, node: ASTNode, data: Any) -> Any:
        """Evaluate array/object iteration (.[] )"""
        if data is None:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return list(data.values())
        raise EvaluationError(f"Cannot iterate over {self._type_name(data)}")

    def _eval_optional(self, node: ASTNode, data: Any) -> Any:
        """Evaluate expr? - errors produce no output (resource limits still apply)."""
        try:
            return self.evaluate(node.children[0], data)
        except LimitError:
            raise
        except EvaluationError:
            return [] if is_stream(node.children[0]) else EMPTY

    def _eval_comma(self, node: ASTNode, data: Any) -> Any:
        """Evaluate a, b: the outputs of each side in turn."""
        results = []
        for child in node.children:
            results.extend(self.outputs(child, data))
        return results

    def _eval_pipe(self, node: ASTNode, data: Any) -> Any:
        """Evaluate pipe expression (a | b)"""
        left_result = self.evaluate(node.children[0], data)

        # Handle EMPTY
        if left_result is EMPTY:
            return EMPTY

        # If left produces multiple values, evaluate right for each
        if is_stream(node.children[0]):
            results = []
            for item in left_result:
                results.extend(self.outputs(node.children[1], item))
            return results

        return self.evaluate(node.children[1], left_result)

    def _eval_default(self, node: ASTNode, data: Any) -> Any:
        """Evaluate default expression (a // b)"""
        left_result = self.evaluate(node.children[0], data)
        if left_result is None or left_result is False or left_result is EMPTY:
            return self.evaluate(node.children[1], data)
        return left_result

    def _eval_object(self, node: ASTNode, data: Any) -> Any:
        """Evaluate object construction ({a: b})"""
        result = {}
        for key, value_node in node.value:
            # Evaluate key (can be string or expression)
            if isinstance(key, str):
                key_val = key
            elif isinstance(key, ASTNode):
                key_val = self.evaluate(key, data)
                if key_val is EMPTY:
                    return EMPTY
                if not isinstance(key_val, str):
                    raise EvaluationError(
                        f"Cannot use {self._type_name(key_val)} ({tojson(key_val)}) as object key"
                    )
            else:
                key_val = str(key)

            # Evaluate value; an empty value means no object at all (as in jq)
            value_result = self.evaluate(value_node, data)
            if value_result is EMPTY:
                return EMPTY
            result[key_val] = value_result

        return result

    def _eval_array(self, node: ASTNode, data: Any) -> Any:
        """Evaluate array construction ([a, b, c])"""
        result = []
        for child in node.children:
            result.extend(self.outputs(child, data))
        return result

    def _eval_string(self, node: ASTNode, data: Any) -> Any:
        """Evaluate string literal with possible interpolation."""
        if isinstance(node.value, str):
            return node.value

        # Handle interpolation
        parts = []
        for part in node.value:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, ASTNode):
                value = self.evaluate(part, data)
                if value is EMPTY:
                    return EMPTY
                parts.append(self._tostring(value))
            else:
                parts.append(str(part))

        return "".join(parts)

    def _eval_number(self, node: ASTNode, data: Any) -> Any:
        """Evaluate number literal."""
        return node.value

    def _eval_bool(self, node: ASTNode, data: Any) -> Any:
        """Evaluate boolean literal."""
        return node.value

    def _eval_null(self, node: ASTNode, data: Any) -> Any:
        """Evaluate null literal."""
        return None

    def _eval_variable(self, node: ASTNode, data: Any) -> Any:
        """Evaluate variable reference ($name)"""
        var_name = node.value
        if var_name in self.variables:
            return self.variables[var_name]
        if var_name == "ENV":
            return dict(os.environ)
        if var_name == "__loc__":
            return {"file": "<top-level>", "line": 1}
        raise EvaluationError(f"Variable ${var_name} is not defined")

    def _eval_comparison(self, node: ASTNode, data: Any) -> Any:
        """Evaluate comparison expression (jq total order across types)."""
        left = self.evaluate(node.children[0], data)
        if left is EMPTY:
            return EMPTY
        right = self.evaluate(node.children[1], data)
        if right is EMPTY:
            return EMPTY
        op = node.value

        lk = self._sort_key(left)
        rk = self._sort_key(right)
        if op == "==":
            return lk == rk
        elif op == "!=":
            return lk != rk
        elif op == "<":
            return lk < rk
        elif op == ">":
            return lk > rk
        elif op == "<=":
            return lk <= rk
        elif op == ">=":
            return lk >= rk
        raise EvaluationError(f"Unknown comparison operator: {op}")

    def _eval_logical(self, node: ASTNode, data: Any) -> Any:
        """Evaluate logical expression (and, or, not)"""
        op = node.value

        if op == "not":
            operand = self.evaluate(node.children[0], data)
            if operand is EMPTY:
                return EMPTY
            return not self._truthy(operand)
        elif op == "and":
            left = self.evaluate(node.children[0], data)
            if left is EMPTY:
                return EMPTY
            if not self._truthy(left):
                return False
            right = self.evaluate(node.children[1], data)
            if right is EMPTY:
                return EMPTY
            return self._truthy(right)
        elif op == "or":
            left = self.evaluate(node.children[0], data)
            if left is EMPTY:
                return EMPTY
            if self._truthy(left):
                return True
            right = self.evaluate(node.children[1], data)
            if right is EMPTY:
                return EMPTY
            return self._truthy(right)
        else:
            raise EvaluationError(f"Unknown logical operator: {op}")

    def _eval_negate(self, node: ASTNode, data: Any) -> Any:
        """Evaluate unary negation (-expr)"""
        operand = self.evaluate(node.children[0], data)
        if operand is EMPTY:
            return EMPTY
        if isinstance(operand, (int, float)) and not isinstance(operand, bool):
            return -operand
        raise EvaluationError(f"{self._type_name(operand)} cannot be negated")

    def _arg(self, arg: ASTNode, data: Any) -> Any:
        """Evaluate a value argument; an empty argument makes the call produce nothing."""
        value = self.evaluate(arg, data)
        if value is EMPTY:
            raise _EmptyArgument()
        return value

    def _eval_function(self, node: ASTNode, data: Any) -> Any:
        """Evaluate function call."""
        try:
            return self._call_function(node, data)
        except _EmptyArgument:
            return EMPTY

    def _call_function(self, node: ASTNode, data: Any) -> Any:
        """Dispatch a builtin call (arity was checked by validate())."""
        func_name = node.value
        args = node.children

        # Built-in functions
        if func_name == "empty":
            return EMPTY

        if func_name == "select":
            predicate_result = self.evaluate(args[0], data)
            if self._truthy(predicate_result):
                return data
            return EMPTY

        if func_name == "map":
            if isinstance(data, dict):
                data = list(data.values())
            if not isinstance(data, list):
                raise EvaluationError(f"Cannot iterate over {self._type_name(data)}")
            results = []
            for item in data:
                results.extend(self.outputs(args[0], item))
            return results

        if func_name == "map_values":
            def first_output(item):
                result = self.outputs(args[0], item)
                return result[0] if result else EMPTY

            if isinstance(data, dict):
                out = {}
                for k, v in data.items():
                    r = first_output(v)
                    if r is not EMPTY:
                        out[k] = r
                return out
            if isinstance(data, list):
                return [r for r in (first_output(v) for v in data) if r is not EMPTY]
            raise EvaluationError(f"Cannot iterate over {self._type_name(data)}")

        if func_name == "keys":
            if isinstance(data, dict):
                return sorted(data.keys())
            if isinstance(data, list):
                return list(range(len(data)))
            raise EvaluationError(f"{self._type_name(data)} ({tojson(data)}) has no keys")

        if func_name == "values":
            # jq: def values: select(. != null)
            return EMPTY if data is None else data

        if func_name == "length":
            if data is None:
                return 0
            if isinstance(data, bool):
                raise EvaluationError(f"boolean ({tojson(data)}) has no length")
            if isinstance(data, (int, float)):
                return abs(data)
            if isinstance(data, (str, list, dict)):
                return len(data)
            raise EvaluationError(f"Cannot get length of {self._type_name(data)}")

        if func_name == "type":
            return self._type_name(data)

        if func_name == "keys_unsorted":
            if isinstance(data, dict):
                return list(data.keys())
            if isinstance(data, list):
                return list(range(len(data)))
            raise EvaluationError(f"{self._type_name(data)} ({tojson(data)}) has no keys")

        if func_name == "has":
            key = self._arg(args[0], data)
            return self._has(data, key)

        if func_name == "in":
            container = self._arg(args[0], data)
            return self._has(container, data)

        if func_name == "contains":
            needle = self._arg(args[0], data)
            return self._checked_contains(data, needle)

        if func_name == "inside":
            container = self._arg(args[0], data)
            return self._checked_contains(container, data)

        if func_name == "add":
            if isinstance(data, dict):
                data = list(data.values())
            if not isinstance(data, list):
                raise EvaluationError(f"Cannot iterate over {self._type_name(data)}")
            result = None
            for item in data:
                result = self._add(result, item)
            return result

        if func_name == "flatten":
            depth = 1_000_000_000
            if args:
                depth = self._arg(args[0], data)
                if not isinstance(depth, (int, float)) or isinstance(depth, bool):
                    raise EvaluationError("flatten depth must be a number")
                if depth < 0:
                    raise EvaluationError("flatten depth must not be negative")
            if not isinstance(data, list):
                raise EvaluationError(f"Cannot flatten {self._type_name(data)}")
            return self._flatten(data, depth)

        if func_name == "reverse":
            if data is None:
                return []
            if isinstance(data, list):
                return list(reversed(data))
            if isinstance(data, str):
                return data[::-1]
            raise EvaluationError(f"Cannot reverse {self._type_name(data)}")

        if func_name == "sort":
            if not isinstance(data, list):
                raise EvaluationError(f"{self._type_name(data)} cannot be sorted, as it is not an array")
            return sorted(data, key=self._sort_key)

        if func_name == "sort_by":
            if not isinstance(data, list):
                raise EvaluationError(f"{self._type_name(data)} cannot be sorted, as it is not an array")
            return sorted(data, key=lambda x: self._sort_key(self.outputs(args[0], x)))

        if func_name == "unique":
            if not isinstance(data, list):
                raise EvaluationError(f"{self._type_name(data)} cannot be sorted, as it is not an array")
            result = []
            last_key = None
            for item in sorted(data, key=self._sort_key):
                key = self._sort_key(item)
                if not result or key != last_key:
                    result.append(item)
                    last_key = key
            return result

        if func_name in ("first", "last"):
            if args:
                outputs = self.outputs(args[0], data)
                if not outputs:
                    # jq 1.7: first(empty) is empty, last(empty) is null
                    return EMPTY if func_name == "first" else None
                return outputs[0] if func_name == "first" else outputs[-1]
            if data is None:
                return None
            if not isinstance(data, list):
                raise EvaluationError(f"Cannot index {self._type_name(data)} with number")
            if not data:
                return None
            return data[0] if func_name == "first" else data[-1]

        if func_name == "nth":
            n = self._arg(args[0], data)
            if data is None:
                return None
            if not isinstance(data, list):
                raise EvaluationError(f"Cannot index {self._type_name(data)} with number")
            if not isinstance(n, (int, float)) or isinstance(n, bool):
                raise EvaluationError(f"Cannot index array with {self._type_name(n)}")
            n = math.floor(n)
            if n < 0:
                n += len(data)
            if 0 <= n < len(data):
                return data[n]
            return None

        if func_name == "min":
            if not isinstance(data, list) or len(data) == 0:
                return None
            return min(data, key=self._sort_key)

        if func_name == "max":
            if not isinstance(data, list) or len(data) == 0:
                return None
            return max(data, key=self._sort_key)

        if func_name == "floor":
            self._require_number(data, "floor")
            return math.floor(data)

        if func_name == "ceil":
            self._require_number(data, "ceil")
            return math.ceil(data)

        if func_name == "round":
            self._require_number(data, "round")
            # C round(): halves go away from zero (Python's round() is banker's)
            return math.floor(data + 0.5) if data >= 0 else -math.floor(-data + 0.5)

        if func_name == "abs":
            self._require_number(data, "abs")
            return abs(data)

        if func_name == "tostring":
            return self._tostring(data)

        if func_name == "tojson":
            return tojson(data)

        if func_name == "fromjson":
            if not isinstance(data, str):
                raise EvaluationError(f"{self._type_name(data)} ({tojson(data)}) cannot be parsed as JSON")
            try:
                return json.loads(data)
            except ValueError as e:
                raise EvaluationError(f"{data!r} cannot be parsed as JSON: {e}") from None

        if func_name == "tonumber":
            if isinstance(data, (int, float)) and not isinstance(data, bool):
                return data
            if isinstance(data, str):
                if not _NUMBER_RE.fullmatch(data):
                    raise EvaluationError(f"Cannot parse {data!r} as a number")
                text = data.strip()
                if "." in text or "e" in text.lower():
                    return float(text)
                return int(text)
            raise EvaluationError(f"{self._type_name(data)} ({tojson(data)}) cannot be parsed as a number")

        if func_name == "not":
            return not self._truthy(data)

        if func_name == "ascii_downcase":
            if isinstance(data, str):
                return data.translate(_ASCII_LOWER)
            raise EvaluationError(f"{self._type_name(data)} cannot be lowercased, as it is not a string")

        if func_name == "ascii_upcase":
            if isinstance(data, str):
                return data.translate(_ASCII_UPPER)
            raise EvaluationError(f"{self._type_name(data)} cannot be uppercased, as it is not a string")

        if func_name == "ltrimstr":
            prefix = self._arg(args[0], data)
            # jq returns non-string inputs (or a non-string prefix) unchanged
            if isinstance(data, str) and isinstance(prefix, str) and data.startswith(prefix):
                return data[len(prefix):]
            return data

        if func_name == "rtrimstr":
            suffix = self._arg(args[0], data)
            if isinstance(data, str) and isinstance(suffix, str) and suffix and data.endswith(suffix):
                return data[:-len(suffix)]
            return data

        if func_name == "startswith":
            prefix = self._arg(args[0], data)
            if isinstance(data, str) and isinstance(prefix, str):
                return data.startswith(prefix)
            raise EvaluationError("startswith() requires string inputs")

        if func_name == "endswith":
            suffix = self._arg(args[0], data)
            if isinstance(data, str) and isinstance(suffix, str):
                return data.endswith(suffix)
            raise EvaluationError("endswith() requires string inputs")

        if func_name == "split":
            sep = self._arg(args[0], data)
            if isinstance(data, str) and isinstance(sep, str):
                if sep == "":
                    return list(data)
                return data.split(sep)
            raise EvaluationError("split input and separator must be strings")

        if func_name == "join":
            sep = self._arg(args[0], data)
            if not isinstance(data, list):
                raise EvaluationError(f"Cannot iterate over {self._type_name(data)}")
            if not isinstance(sep, str):
                raise EvaluationError("join separator must be a string")
            parts = []
            for item in data:
                if item is None:
                    parts.append("")
                elif isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, (bool, int, float)):
                    parts.append(tojson(item))
                else:
                    raise EvaluationError(
                        f"Cannot join with {self._type_name(item)} ({tojson(item)})"
                    )
            return sep.join(parts)

        if func_name in ("test", "match", "capture"):
            pattern = self._arg(args[0], data)
            flags = self._arg(args[1], data) if len(args) > 1 else None
            if not (isinstance(data, str) and isinstance(pattern, str)):
                raise EvaluationError(f"{self._type_name(data)} ({tojson(data)}) cannot be matched, as it is not a string")
            m = self._compile_regex(pattern, flags, allow_global=func_name == "test").search(data)
            if func_name == "test":
                return m is not None
            if m is None:
                return EMPTY
            if func_name == "capture":
                return m.groupdict()
            names = {index: name for name, index in m.re.groupindex.items()}
            return {
                "offset": m.start(),
                "length": m.end() - m.start(),
                "string": m.group(0),
                "captures": [
                    {
                        "offset": m.start(i),
                        "length": (m.end(i) - m.start(i)) if m.group(i) is not None else 0,
                        "string": m.group(i),
                        "name": names.get(i),
                    }
                    for i in range(1, len(m.groups()) + 1)
                ],
            }

        if func_name in ("sub", "gsub"):
            pattern = self._arg(args[0], data)
            flags = self._arg(args[2], data) if len(args) > 2 else None
            if not (isinstance(data, str) and isinstance(pattern, str)):
                raise EvaluationError(f"{self._type_name(data)} ({tojson(data)}) cannot be matched, as it is not a string")
            regex = self._compile_regex(pattern, flags, allow_global=True)
            count = 0 if (func_name == "gsub" or "g" in (flags or "")) else 1

            def replace(m):
                # The replacement is a jq expression evaluated against the
                # named captures, e.g. sub("(?<x>a)"; "<\(.x)>")
                replacement = self._arg(args[1], m.groupdict())
                if not isinstance(replacement, str):
                    raise EvaluationError(
                        f"{self._type_name(replacement)} ({tojson(replacement)}) cannot be added to a string"
                    )
                return replacement

            return regex.sub(replace, data, count=count)

        if func_name == "error":
            msg = data
            if args:
                msg = self._arg(args[0], data)
            if isinstance(msg, str):
                raise EvaluationError(msg)
            raise EvaluationError(f"{tojson(msg)} (not a string)")

        if func_name == "debug":
            print(tojson(["DEBUG:", data]), file=sys.stderr)
            return data

        if func_name == "getpath":
            path = self._arg(args[0], data)
            if not isinstance(path, list):
                raise EvaluationError("Path must be specified as an array")
            result = data
            for key in path:
                if result is None:
                    return None
                if isinstance(key, str) and isinstance(result, dict):
                    result = result.get(key)
                elif isinstance(key, int) and not isinstance(key, bool) and isinstance(result, list):
                    if -len(result) <= key < len(result):
                        result = result[key]
                    else:
                        return None
                else:
                    raise EvaluationError(
                        f"Cannot index {self._type_name(result)} with {self._type_name(key)}"
                    )
            return result

        if func_name == "group_by":
            if not isinstance(data, list):
                raise EvaluationError(f"Cannot index {self._type_name(data)} with number")
            keyed = sorted(
                ((self._sort_key(self.outputs(args[0], item)), item) for item in data),
                key=lambda pair: pair[0],
            )
            groups: List[List[Any]] = []
            last_key = None
            for key, item in keyed:
                if groups and key == last_key:
                    groups[-1].append(item)
                else:
                    groups.append([item])
                    last_key = key
            return groups

        if func_name in ("any", "all"):
            if len(args) == 2:
                # any(generator; condition)
                values = (
                    self._truthy(out)
                    for item in self.outputs(args[0], data)
                    for out in self.outputs(args[1], item)
                )
                return any(values) if func_name == "any" else all(values)
            if isinstance(data, dict):
                data = list(data.values())
            if not isinstance(data, list):
                raise EvaluationError(f"Cannot iterate over {self._type_name(data)}")
            if args:
                values = (
                    self._truthy(out)
                    for item in data
                    for out in self.outputs(args[0], item)
                )
            else:
                values = (self._truthy(item) for item in data)
            return any(values) if func_name == "any" else all(values)

        if func_name == "from_entries":
            if not isinstance(data, list):
                raise EvaluationError(f"Cannot iterate over {self._type_name(data)}")
            return self._from_entries(data)

        if func_name == "to_entries":
            if isinstance(data, list):
                return [{"key": i, "value": v} for i, v in enumerate(data)]
            if not isinstance(data, dict):
                raise EvaluationError(f"{self._type_name(data)} ({tojson(data)}) has no keys")
            return [{"key": k, "value": v} for k, v in data.items()]

        if func_name == "with_entries":
            if not isinstance(data, dict):
                raise EvaluationError(f"{self._type_name(data)} ({tojson(data)}) has no keys")
            transformed = []
            for k, v in data.items():
                transformed.extend(self.outputs(args[0], {"key": k, "value": v}))
            return self._from_entries(transformed)

        if func_name == "recurse":
            # Depth-first, pre-order, like jq's `def recurse(f): def r: ., (f | r); r;`
            if args:
                def children_of(value):
                    children = self.outputs(args[0], value)
                    # Shim difference: null children are dropped, so recurse(.a)
                    # terminates instead of emitting null forever.
                    return [c for c in children if c is not None]
            else:
                def children_of(value):
                    if isinstance(value, list):
                        return value
                    if isinstance(value, dict):
                        return list(value.values())
                    return []

            results = []
            to_process = [data]
            while to_process:
                current = to_process.pop()
                results.append(current)
                self._tick()  # every output counts against the step limit
                to_process.extend(reversed(children_of(current)))
            return results

        if func_name == "walk":
            return self._walk(data, args[0])

        if func_name == "env":
            return dict(os.environ)

        if func_name == "now":
            return time.time()

        if func_name == "infinite":
            return float('inf')

        if func_name == "nan":
            return float('nan')

        if func_name == "isinfinite":
            return isinstance(data, float) and math.isinf(data)

        if func_name == "isnan":
            return isinstance(data, float) and math.isnan(data)

        if func_name == "isnormal":
            if isinstance(data, bool) or not isinstance(data, (int, float)):
                return False
            if isinstance(data, int):
                return data != 0
            return not (math.isnan(data) or math.isinf(data) or data == 0)

        raise UnsupportedError(f"{func_name}/{len(args)} is not defined")

    def _walk(self, data: Any, transform: ASTNode) -> Any:
        """Recursively transform data bottom-up (values that become empty are dropped)."""
        if isinstance(data, list):
            data = [
                v for v in (self._walk(item, transform) for item in data)
                if v is not EMPTY
            ]
        elif isinstance(data, dict):
            walked = {}
            for k, v in data.items():
                w = self._walk(v, transform)
                if w is not EMPTY:
                    walked[k] = w
            data = walked
        return self.evaluate(transform, data)

    def _truthy(self, value: Any) -> bool:
        """Check if value is truthy in jq terms."""
        if value is None:
            return False
        if value is False:
            return False
        if value is EMPTY:
            return False
        return True

    def _type_name(self, value: Any) -> str:
        """Get jq type name for a value."""
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, (int, float)):
            return "number"
        if isinstance(value, str):
            return "string"
        if isinstance(value, list):
            return "array"
        if isinstance(value, dict):
            return "object"
        return "unknown"

    def _tostring(self, value: Any) -> str:
        """jq tostring: strings unchanged, everything else as compact JSON."""
        if isinstance(value, str):
            return value
        return tojson(value)

    def _require_number(self, value: Any, name: str) -> None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise EvaluationError(f"{self._type_name(value)} ({tojson(value)}) number required for {name}")

    def _has(self, container: Any, key: Any) -> bool:
        """jq has(key) semantics (also used for `key | in(container)`)."""
        if isinstance(container, dict) and isinstance(key, str):
            return key in container
        if isinstance(container, list) and isinstance(key, (int, float)) and not isinstance(key, bool):
            return 0 <= key < len(container)
        raise EvaluationError(
            f"Cannot check whether {self._type_name(container)} has a {self._type_name(key)} key"
        )

    def _add(self, left: Any, right: Any) -> Any:
        """jq `+` for the types add() combines (null is the identity)."""
        if left is None:
            return right
        if right is None:
            return left
        if isinstance(left, bool) or isinstance(right, bool):
            pass
        elif isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return left + right
        elif isinstance(left, str) and isinstance(right, str):
            return left + right
        elif isinstance(left, list) and isinstance(right, list):
            return left + right
        elif isinstance(left, dict) and isinstance(right, dict):
            return {**left, **right}
        raise EvaluationError(
            f"{self._type_name(left)} ({tojson(left)}) and "
            f"{self._type_name(right)} ({tojson(right)}) cannot be added"
        )

    def _from_entries(self, entries: List[Any]) -> Dict[str, Any]:
        """Build an object from {key|k|name|Name|K|Key, value|v} entries."""
        result = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise EvaluationError(
                    f"Cannot index {self._type_name(entry)} with \"key\""
                )
            key = None
            for name in ("key", "k", "name", "Name", "K", "Key"):
                if entry.get(name) is not None:
                    key = entry[name]
                    break
            if key is None:
                # Shim difference: jq errors on a null key; the entry is skipped.
                continue
            if "value" in entry:
                value = entry["value"]
            elif "v" in entry:
                value = entry["v"]
            else:
                value = entry.get("Value")
            result[self._tostring(key)] = value
        return result

    def _compile_regex(self, pattern: str, flags: Any, allow_global: bool):
        """Compile a jq regex (Python `re` dialect) with jq's flag letters."""
        if flags is not None and not isinstance(flags, str):
            raise EvaluationError(f"{tojson(flags)} is not a string")
        re_flags = 0
        for flag in flags or "":
            if flag == "i":
                re_flags |= re.IGNORECASE
            elif flag in ("m", "p"):
                # Oniguruma's multiline mode lets '.' match newlines
                re_flags |= re.DOTALL
            elif flag == "x":
                re_flags |= re.VERBOSE
            elif flag in ("s", "n", "l"):
                # s: ^/$ anchor the whole string, which is already Python's
                # default; n/l have no Python equivalent and are ignored
                pass
            elif flag == "g":
                if not allow_global:
                    raise UnsupportedError(
                        "the 'g' flag of match/capture (multiple outputs) is not supported"
                    )
            else:
                raise EvaluationError(f"{flags} is not a valid modifier string")
        # Oniguruma's (?<name>...) is Python's (?P<name>...)
        pattern = _NAMED_GROUP_RE.sub("(?P<", pattern)
        try:
            return re.compile(pattern, re_flags)
        except re.error as e:
            raise EvaluationError(f"{pattern} (at offset {e.pos}) is not a valid regex: {e.msg}") from None

    def _checked_contains(self, container: Any, needle: Any) -> bool:
        """jq contains(): the two values must be of the same kind."""
        if self._sort_key(container)[0] != self._sort_key(needle)[0]:
            raise EvaluationError(
                f"{self._type_name(container)} ({tojson(container)}) and "
                f"{self._type_name(needle)} ({tojson(needle)}) cannot have their containment checked"
            )
        return self._contains(container, needle)

    def _contains(self, container: Any, needle: Any) -> bool:
        """Check if container contains needle (recursive for objects/arrays)."""
        if isinstance(needle, dict) and isinstance(container, dict):
            for key, value in needle.items():
                if key not in container:
                    return False
                if not self._contains(container[key], value):
                    return False
            return True
        if isinstance(needle, list) and isinstance(container, list):
            for item in needle:
                if not any(self._contains(c_item, item) for c_item in container):
                    return False
            return True
        if isinstance(needle, str) and isinstance(container, str):
            return needle in container
        return self._sort_key(container) == self._sort_key(needle)

    def _flatten(self, data: List, depth: int) -> List:
        """Flatten a list to the given depth."""
        if depth <= 0:
            return data
        result = []
        for item in data:
            if isinstance(item, list):
                result.extend(self._flatten(item, depth - 1))
            else:
                result.append(item)
        return result

    def _sort_key(self, value: Any) -> tuple:
        """
        Sort/comparison key implementing jq's total order:
        null < false < true < numbers < strings < arrays < objects.
        Objects compare by their sorted key lists first, then values key by key.
        """
        if value is None:
            return (0,)
        if value is False:
            return (1,)
        if value is True:
            return (2,)
        if isinstance(value, (int, float)):
            return (3, value)
        if isinstance(value, str):
            return (4, value)
        if isinstance(value, list):
            return (5, tuple(self._sort_key(v) for v in value))
        if isinstance(value, dict):
            keys = sorted(value.keys())
            return (6, tuple(keys), tuple(self._sort_key(value[k]) for k in keys))
        raise EvaluationError(f"Cannot compare {type(value).__name__}")


_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_ASCII_UPPER = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
