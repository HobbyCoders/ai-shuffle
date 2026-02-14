"""
jq expression evaluator.

Evaluates AST nodes against JSON data with variable support.
"""

import logging
from typing import Any, Dict, List, Optional
from .parser import ASTNode, NodeType

logger = logging.getLogger(__name__)


class EvaluationError(Exception):
    """Raised when evaluation fails."""

    pass


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
        - A list (for iterations)
        - EMPTY sentinel (for no output)
    """
    variables = variables or {}
    evaluator = JQEvaluator(variables)
    return evaluator.evaluate(node, data)


class JQEvaluator:
    """Evaluator for jq AST nodes."""

    def __init__(self, variables: Dict[str, Any]):
        self.variables = variables

    def evaluate(self, node: ASTNode, data: Any) -> Any:
        """Evaluate a node against data."""
        method_name = f"_eval_{node.type.value}"
        method = getattr(self, method_name, None)
        if method is None:
            raise EvaluationError(f"Unsupported node type: {node.type}")
        return method(node, data)

    def _eval_identity(self, node: ASTNode, data: Any) -> Any:
        """Evaluate identity expression (.)"""
        return data

    def _eval_field(self, node: ASTNode, data: Any) -> Any:
        """Evaluate field access (.field)"""
        if data is None:
            return None
        if not isinstance(data, dict):
            raise EvaluationError(
                f"Cannot index {type(data).__name__} with string {node.value!r}"
            )
        return data.get(node.value)

    def _eval_index(self, node: ASTNode, data: Any) -> Any:
        """Evaluate array index (.[n]) or object key access"""
        if data is None:
            return None

        # Evaluate the index expression
        index = self.evaluate(node.children[0], data)

        if isinstance(index, int):
            if not isinstance(data, list):
                raise EvaluationError(
                    f"Cannot index {type(data).__name__} with number"
                )
            if index < 0:
                index = len(data) + index
            if 0 <= index < len(data):
                return data[index]
            return None
        elif isinstance(index, str):
            if not isinstance(data, dict):
                raise EvaluationError(
                    f"Cannot index {type(data).__name__} with string"
                )
            return data.get(index)
        else:
            raise EvaluationError(f"Invalid index type: {type(index).__name__}")

    def _eval_iterate(self, node: ASTNode, data: Any) -> Any:
        """Evaluate array/object iteration (.[] )"""
        if data is None:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return list(data.values())
        raise EvaluationError(f"Cannot iterate over {type(data).__name__}")

    def _eval_pipe(self, node: ASTNode, data: Any) -> Any:
        """Evaluate pipe expression (a | b)"""
        left_result = self.evaluate(node.children[0], data)

        # Handle EMPTY
        if left_result is EMPTY:
            return EMPTY

        # If left produces multiple values, evaluate right for each
        if isinstance(left_result, list) and self._is_iterator(node.children[0]):
            results = []
            for item in left_result:
                result = self.evaluate(node.children[1], item)
                if result is EMPTY:
                    continue
                if isinstance(result, list) and self._is_iterator(node.children[1]):
                    results.extend(result)
                else:
                    results.append(result)
            return results

        return self.evaluate(node.children[1], left_result)

    def _is_iterator(self, node: ASTNode) -> bool:
        """Check if node produces multiple values."""
        if node.type == NodeType.ITERATE:
            return True
        if node.type == NodeType.PIPE:
            return self._is_iterator(node.children[1])
        return False

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
                if not isinstance(key_val, str):
                    key_val = str(key_val)
            else:
                key_val = str(key)

            # Evaluate value
            value_result = self.evaluate(value_node, data)
            result[key_val] = value_result

        return result

    def _eval_array(self, node: ASTNode, data: Any) -> Any:
        """Evaluate array construction ([a, b, c])"""
        result = []
        for child in node.children:
            value = self.evaluate(child, data)
            if value is EMPTY:
                continue
            if isinstance(value, list) and self._is_iterator(child):
                result.extend(value)
            else:
                result.append(value)
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
                if value is None:
                    parts.append("null")
                elif isinstance(value, bool):
                    parts.append("true" if value else "false")
                elif isinstance(value, str):
                    parts.append(value)
                else:
                    parts.append(str(value))
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
        if var_name not in self.variables:
            raise EvaluationError(f"Variable ${var_name} is not defined")
        return self.variables[var_name]

    def _eval_comparison(self, node: ASTNode, data: Any) -> Any:
        """Evaluate comparison expression."""
        left = self.evaluate(node.children[0], data)
        right = self.evaluate(node.children[1], data)
        op = node.value

        try:
            if op == "==":
                return left == right
            elif op == "!=":
                return left != right
            elif op == "<":
                return left < right
            elif op == ">":
                return left > right
            elif op == "<=":
                return left <= right
            elif op == ">=":
                return left >= right
            else:
                raise EvaluationError(f"Unknown comparison operator: {op}")
        except TypeError:
            # Handle incomparable types
            if op == "==":
                return False
            elif op == "!=":
                return True
            raise EvaluationError(
                f"Cannot compare {type(left).__name__} and {type(right).__name__}"
            )

    def _eval_logical(self, node: ASTNode, data: Any) -> Any:
        """Evaluate logical expression (and, or, not)"""
        op = node.value

        if op == "not":
            operand = self.evaluate(node.children[0], data)
            return not self._truthy(operand)
        elif op == "and":
            left = self.evaluate(node.children[0], data)
            if not self._truthy(left):
                return False
            right = self.evaluate(node.children[1], data)
            return self._truthy(right)
        elif op == "or":
            left = self.evaluate(node.children[0], data)
            if self._truthy(left):
                return True
            right = self.evaluate(node.children[1], data)
            return self._truthy(right)
        else:
            raise EvaluationError(f"Unknown logical operator: {op}")

    def _eval_negate(self, node: ASTNode, data: Any) -> Any:
        """Evaluate unary negation (-expr)"""
        operand = self.evaluate(node.children[0], data)
        if isinstance(operand, (int, float)):
            return -operand
        raise EvaluationError(f"Cannot negate {type(operand).__name__}")

    def _eval_function(self, node: ASTNode, data: Any) -> Any:
        """Evaluate function call."""
        func_name = node.value
        args = node.children

        # Built-in functions
        if func_name == "empty":
            return EMPTY

        if func_name == "select":
            if len(args) != 1:
                raise EvaluationError("select() requires exactly 1 argument")
            predicate_result = self.evaluate(args[0], data)
            if self._truthy(predicate_result):
                return data
            return EMPTY

        if func_name == "map":
            if len(args) != 1:
                raise EvaluationError("map() requires exactly 1 argument")
            if not isinstance(data, list):
                raise EvaluationError(f"Cannot map over {type(data).__name__}")
            results = []
            for item in data:
                result = self.evaluate(args[0], item)
                if result is not EMPTY:
                    results.append(result)
            return results

        if func_name == "keys":
            if isinstance(data, dict):
                return sorted(data.keys())
            if isinstance(data, list):
                return list(range(len(data)))
            raise EvaluationError(f"Cannot get keys of {type(data).__name__}")

        if func_name == "values":
            if isinstance(data, dict):
                return list(data.values())
            if isinstance(data, list):
                return data
            raise EvaluationError(f"Cannot get values of {type(data).__name__}")

        if func_name == "length":
            if data is None:
                return 0
            if isinstance(data, (str, list, dict)):
                return len(data)
            raise EvaluationError(f"Cannot get length of {type(data).__name__}")

        if func_name == "type":
            return self._type_name(data)

        if func_name == "keys_unsorted":
            if isinstance(data, dict):
                return list(data.keys())
            if isinstance(data, list):
                return list(range(len(data)))
            raise EvaluationError(f"Cannot get keys of {type(data).__name__}")

        if func_name == "has":
            if len(args) != 1:
                raise EvaluationError("has() requires exactly 1 argument")
            key = self.evaluate(args[0], data)
            if isinstance(data, dict):
                return key in data
            if isinstance(data, list) and isinstance(key, int):
                return 0 <= key < len(data)
            return False

        if func_name == "in":
            if len(args) != 1:
                raise EvaluationError("in() requires exactly 1 argument")
            container = self.evaluate(args[0], data)
            if isinstance(container, dict):
                return data in container
            return False

        if func_name == "contains":
            if len(args) != 1:
                raise EvaluationError("contains() requires exactly 1 argument")
            needle = self.evaluate(args[0], data)
            return self._contains(data, needle)

        if func_name == "inside":
            if len(args) != 1:
                raise EvaluationError("inside() requires exactly 1 argument")
            container = self.evaluate(args[0], data)
            return self._contains(container, data)

        if func_name == "add":
            if not isinstance(data, list):
                raise EvaluationError(f"Cannot add {type(data).__name__}")
            if len(data) == 0:
                return None
            result = data[0]
            for item in data[1:]:
                if isinstance(result, str) and isinstance(item, str):
                    result = result + item
                elif isinstance(result, (int, float)) and isinstance(item, (int, float)):
                    result = result + item
                elif isinstance(result, list) and isinstance(item, list):
                    result = result + item
                elif isinstance(result, dict) and isinstance(item, dict):
                    result = {**result, **item}
                else:
                    raise EvaluationError(
                        f"Cannot add {type(result).__name__} and {type(item).__name__}"
                    )
            return result

        if func_name == "flatten":
            depth = 1
            if args:
                depth = self.evaluate(args[0], data)
            if not isinstance(data, list):
                raise EvaluationError(f"Cannot flatten {type(data).__name__}")
            return self._flatten(data, depth)

        if func_name == "reverse":
            if isinstance(data, list):
                return list(reversed(data))
            if isinstance(data, str):
                return data[::-1]
            raise EvaluationError(f"Cannot reverse {type(data).__name__}")

        if func_name == "sort":
            if not isinstance(data, list):
                raise EvaluationError(f"Cannot sort {type(data).__name__}")
            return sorted(data, key=self._sort_key)

        if func_name == "sort_by":
            if len(args) != 1:
                raise EvaluationError("sort_by() requires exactly 1 argument")
            if not isinstance(data, list):
                raise EvaluationError(f"Cannot sort {type(data).__name__}")
            return sorted(data, key=lambda x: self._sort_key(self.evaluate(args[0], x)))

        if func_name == "unique":
            if not isinstance(data, list):
                raise EvaluationError(f"Cannot get unique of {type(data).__name__}")
            seen = []
            result = []
            for item in data:
                if item not in seen:
                    seen.append(item)
                    result.append(item)
            return result

        if func_name == "first":
            if isinstance(data, list):
                return data[0] if data else None
            return data

        if func_name == "last":
            if isinstance(data, list):
                return data[-1] if data else None
            return data

        if func_name == "nth":
            if len(args) != 1:
                raise EvaluationError("nth() requires exactly 1 argument")
            n = self.evaluate(args[0], data)
            if isinstance(data, list):
                if 0 <= n < len(data):
                    return data[n]
                return None
            return data if n == 0 else None

        if func_name == "min":
            if not isinstance(data, list) or len(data) == 0:
                return None
            return min(data, key=self._sort_key)

        if func_name == "max":
            if not isinstance(data, list) or len(data) == 0:
                return None
            return max(data, key=self._sort_key)

        if func_name == "floor":
            if isinstance(data, (int, float)):
                import math
                return math.floor(data)
            raise EvaluationError(f"Cannot floor {type(data).__name__}")

        if func_name == "ceil":
            if isinstance(data, (int, float)):
                import math
                return math.ceil(data)
            raise EvaluationError(f"Cannot ceil {type(data).__name__}")

        if func_name == "round":
            if isinstance(data, (int, float)):
                return round(data)
            raise EvaluationError(f"Cannot round {type(data).__name__}")

        if func_name == "abs":
            if isinstance(data, (int, float)):
                return abs(data)
            raise EvaluationError(f"Cannot abs {type(data).__name__}")

        if func_name == "tostring":
            if data is None:
                return "null"
            if isinstance(data, bool):
                return "true" if data else "false"
            if isinstance(data, str):
                return data
            import json
            return json.dumps(data)

        if func_name == "tonumber":
            if isinstance(data, (int, float)):
                return data
            if isinstance(data, str):
                try:
                    if "." in data or "e" in data.lower():
                        return float(data)
                    return int(data)
                except ValueError:
                    raise EvaluationError(f"Cannot convert {data!r} to number")
            raise EvaluationError(f"Cannot convert {type(data).__name__} to number")

        if func_name == "not":
            return not self._truthy(data)

        if func_name == "ascii_downcase":
            if isinstance(data, str):
                return data.lower()
            raise EvaluationError(f"Cannot lowercase {type(data).__name__}")

        if func_name == "ascii_upcase":
            if isinstance(data, str):
                return data.upper()
            raise EvaluationError(f"Cannot uppercase {type(data).__name__}")

        if func_name == "ltrimstr":
            if len(args) != 1:
                raise EvaluationError("ltrimstr() requires exactly 1 argument")
            prefix = self.evaluate(args[0], data)
            if isinstance(data, str) and isinstance(prefix, str):
                if data.startswith(prefix):
                    return data[len(prefix):]
                return data
            raise EvaluationError("ltrimstr requires string arguments")

        if func_name == "rtrimstr":
            if len(args) != 1:
                raise EvaluationError("rtrimstr() requires exactly 1 argument")
            suffix = self.evaluate(args[0], data)
            if isinstance(data, str) and isinstance(suffix, str):
                if data.endswith(suffix):
                    return data[:-len(suffix)] if suffix else data
                return data
            raise EvaluationError("rtrimstr requires string arguments")

        if func_name == "startswith":
            if len(args) != 1:
                raise EvaluationError("startswith() requires exactly 1 argument")
            prefix = self.evaluate(args[0], data)
            if isinstance(data, str) and isinstance(prefix, str):
                return data.startswith(prefix)
            raise EvaluationError("startswith requires string arguments")

        if func_name == "endswith":
            if len(args) != 1:
                raise EvaluationError("endswith() requires exactly 1 argument")
            suffix = self.evaluate(args[0], data)
            if isinstance(data, str) and isinstance(suffix, str):
                return data.endswith(suffix)
            raise EvaluationError("endswith requires string arguments")

        if func_name == "split":
            if len(args) != 1:
                raise EvaluationError("split() requires exactly 1 argument")
            sep = self.evaluate(args[0], data)
            if isinstance(data, str) and isinstance(sep, str):
                return data.split(sep)
            raise EvaluationError("split requires string arguments")

        if func_name == "join":
            if len(args) != 1:
                raise EvaluationError("join() requires exactly 1 argument")
            sep = self.evaluate(args[0], data)
            if isinstance(data, list) and isinstance(sep, str):
                return sep.join(str(item) for item in data)
            raise EvaluationError("join requires array and string separator")

        if func_name == "test":
            if len(args) != 1:
                raise EvaluationError("test() requires exactly 1 argument")
            pattern = self.evaluate(args[0], data)
            if isinstance(data, str) and isinstance(pattern, str):
                import re
                return bool(re.search(pattern, data))
            raise EvaluationError("test requires string arguments")

        if func_name == "match":
            if len(args) != 1:
                raise EvaluationError("match() requires exactly 1 argument")
            pattern = self.evaluate(args[0], data)
            if isinstance(data, str) and isinstance(pattern, str):
                import re
                m = re.search(pattern, data)
                if m:
                    return {
                        "offset": m.start(),
                        "length": m.end() - m.start(),
                        "string": m.group(0),
                        "captures": [
                            {"offset": m.start(i+1), "length": m.end(i+1) - m.start(i+1), "string": m.group(i+1), "name": None}
                            for i in range(len(m.groups()))
                        ] if m.groups() else []
                    }
                return None
            raise EvaluationError("match requires string arguments")

        if func_name == "capture":
            if len(args) != 1:
                raise EvaluationError("capture() requires exactly 1 argument")
            pattern = self.evaluate(args[0], data)
            if isinstance(data, str) and isinstance(pattern, str):
                import re
                m = re.search(pattern, data)
                if m:
                    return m.groupdict()
                return {}
            raise EvaluationError("capture requires string arguments")

        if func_name == "sub":
            if len(args) != 2:
                raise EvaluationError("sub() requires exactly 2 arguments")
            pattern = self.evaluate(args[0], data)
            replacement = self.evaluate(args[1], data)
            if isinstance(data, str) and isinstance(pattern, str) and isinstance(replacement, str):
                import re
                return re.sub(pattern, replacement, data, count=1)
            raise EvaluationError("sub requires string arguments")

        if func_name == "gsub":
            if len(args) != 2:
                raise EvaluationError("gsub() requires exactly 2 arguments")
            pattern = self.evaluate(args[0], data)
            replacement = self.evaluate(args[1], data)
            if isinstance(data, str) and isinstance(pattern, str) and isinstance(replacement, str):
                import re
                return re.sub(pattern, replacement, data)
            raise EvaluationError("gsub requires string arguments")

        if func_name == "error":
            msg = data
            if args:
                msg = self.evaluate(args[0], data)
            raise EvaluationError(str(msg) if msg else "error")

        if func_name == "debug":
            import sys
            import json
            logger.debug("jq debug: %s", json.dumps(data))
            return data

        if func_name == "getpath":
            if len(args) != 1:
                raise EvaluationError("getpath() requires exactly 1 argument")
            path = self.evaluate(args[0], data)
            if not isinstance(path, list):
                raise EvaluationError("getpath requires array path")
            result = data
            for key in path:
                if result is None:
                    return None
                if isinstance(key, str) and isinstance(result, dict):
                    result = result.get(key)
                elif isinstance(key, int) and isinstance(result, list):
                    if 0 <= key < len(result):
                        result = result[key]
                    else:
                        return None
                else:
                    return None
            return result

        if func_name == "group_by":
            if len(args) != 1:
                raise EvaluationError("group_by() requires exactly 1 argument")
            if not isinstance(data, list):
                raise EvaluationError(f"Cannot group {type(data).__name__}")
            groups: Dict[Any, List] = {}
            group_order = []
            for item in data:
                key = self.evaluate(args[0], item)
                # Convert to string for hashable key
                key_str = str(key) if not isinstance(key, (str, int, float, bool, type(None))) else key
                if key_str not in groups:
                    groups[key_str] = []
                    group_order.append(key_str)
                groups[key_str].append(item)
            return [groups[k] for k in group_order]

        if func_name == "any":
            if not isinstance(data, list):
                return self._truthy(data)
            if args:
                return any(self._truthy(self.evaluate(args[0], item)) for item in data)
            return any(self._truthy(item) for item in data)

        if func_name == "all":
            if not isinstance(data, list):
                return self._truthy(data)
            if args:
                return all(self._truthy(self.evaluate(args[0], item)) for item in data)
            return all(self._truthy(item) for item in data)

        if func_name == "from_entries":
            if not isinstance(data, list):
                raise EvaluationError(f"Cannot convert {type(data).__name__} from entries")
            result = {}
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                key = entry.get("key") or entry.get("name") or entry.get("k")
                value = entry.get("value") or entry.get("v")
                if key is not None:
                    result[str(key)] = value
            return result

        if func_name == "to_entries":
            if not isinstance(data, dict):
                raise EvaluationError(f"Cannot convert {type(data).__name__} to entries")
            return [{"key": k, "value": v} for k, v in data.items()]

        if func_name == "with_entries":
            if len(args) != 1:
                raise EvaluationError("with_entries() requires exactly 1 argument")
            if not isinstance(data, dict):
                raise EvaluationError(f"Cannot process {type(data).__name__} with_entries")
            entries = [{"key": k, "value": v} for k, v in data.items()]
            transformed = [self.evaluate(args[0], entry) for entry in entries]
            result = {}
            for entry in transformed:
                if isinstance(entry, dict):
                    key = entry.get("key") or entry.get("name") or entry.get("k")
                    value = entry.get("value") or entry.get("v")
                    if key is not None:
                        result[str(key)] = value
            return result

        if func_name == "recurse":
            results = [data]
            to_process = [data]
            while to_process:
                current = to_process.pop(0)
                if args:
                    children = self.evaluate(args[0], current)
                else:
                    # Default: recurse on array elements and object values
                    if isinstance(current, list):
                        children = current
                    elif isinstance(current, dict):
                        children = list(current.values())
                    else:
                        continue

                if children is EMPTY:
                    continue
                if isinstance(children, list):
                    for child in children:
                        if child is not None and child is not EMPTY:
                            results.append(child)
                            to_process.append(child)
                elif children is not None:
                    results.append(children)
                    to_process.append(children)
            return results

        if func_name == "walk":
            if len(args) != 1:
                raise EvaluationError("walk() requires exactly 1 argument")
            return self._walk(data, args[0])

        if func_name == "env":
            import os
            if isinstance(data, str):
                return os.environ.get(data)
            return dict(os.environ)

        if func_name == "now":
            import time
            return time.time()

        if func_name == "infinite":
            return float('inf')

        if func_name == "nan":
            return float('nan')

        if func_name == "isinfinite":
            if isinstance(data, float):
                import math
                return math.isinf(data)
            return False

        if func_name == "isnan":
            if isinstance(data, float):
                import math
                return math.isnan(data)
            return False

        if func_name == "isnormal":
            if isinstance(data, (int, float)):
                import math
                if isinstance(data, int):
                    return data != 0
                return not (math.isnan(data) or math.isinf(data) or data == 0)
            return False

        raise EvaluationError(f"Unknown function: {func_name}")

    def _walk(self, data: Any, transform: ASTNode) -> Any:
        """Recursively transform data bottom-up."""
        if isinstance(data, list):
            data = [self._walk(item, transform) for item in data]
        elif isinstance(data, dict):
            data = {k: self._walk(v, transform) for k, v in data.items()}
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

    def _contains(self, container: Any, needle: Any) -> bool:
        """Check if container contains needle (recursive for objects/arrays)."""
        if container == needle:
            return True
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
        return False

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
        """Generate a sort key for jq sorting."""
        # jq sorts by type first, then value
        type_order = {"null": 0, "boolean": 1, "number": 2, "string": 3, "array": 4, "object": 5}
        type_name = self._type_name(value)
        type_rank = type_order.get(type_name, 6)

        if value is None:
            return (type_rank, 0)
        if isinstance(value, bool):
            return (type_rank, int(value))
        if isinstance(value, (int, float)):
            return (type_rank, value)
        if isinstance(value, str):
            return (type_rank, value)
        if isinstance(value, list):
            return (type_rank, tuple(self._sort_key(v) for v in value))
        if isinstance(value, dict):
            return (type_rank, tuple(sorted((k, self._sort_key(v)) for k, v in value.items())))
        return (type_rank, str(value))
