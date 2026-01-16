#!/usr/bin/env python3
"""
Python jq shim - CLI entry point.

This module provides a command-line interface compatible with jq,
allowing shell scripts to use jq syntax while the processing is
handled by Python.

Usage:
    echo '{"a": 1}' | python -m app.core.jq_shim '.a'
    python -m app.core.jq_shim -r '.field' file.json
    python -m app.core.jq_shim --arg name value '$name'
"""

import sys
import json
import argparse
import io
from typing import List, Optional, Any

# Fix Windows console encoding - Windows uses cp1252 by default which
# can't handle Unicode characters like emojis in JSON data
if sys.platform == "win32":
    # Reconfigure stdout/stderr to use UTF-8
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from .parser import parse_jq_expression, ParseError
from .evaluator import evaluate, EvaluationError, EMPTY


def main(args: Optional[List[str]] = None) -> int:
    """Main entry point for jq shim CLI."""
    parser = argparse.ArgumentParser(
        prog="jq",
        description="Python jq shim - JSON processor",
        add_help=True,
    )

    parser.add_argument(
        "filter",
        nargs="?",
        default=".",
        help="jq filter expression (default: .)",
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Input JSON files (default: stdin)",
    )
    parser.add_argument(
        "-r", "--raw-output",
        action="store_true",
        help="Output raw strings without JSON encoding",
    )
    parser.add_argument(
        "-n", "--null-input",
        action="store_true",
        help="Use null as single input value",
    )
    parser.add_argument(
        "-c", "--compact-output",
        action="store_true",
        help="Compact output (no pretty printing)",
    )
    parser.add_argument(
        "-e", "--exit-status",
        action="store_true",
        help="Exit with non-zero status on null/false result",
    )
    parser.add_argument(
        "-s", "--slurp",
        action="store_true",
        help="Read entire input into array",
    )
    parser.add_argument(
        "-S", "--sort-keys",
        action="store_true",
        help="Sort object keys in output",
    )
    parser.add_argument(
        "-j", "--join-output",
        action="store_true",
        help="No newline after each output",
    )
    parser.add_argument(
        "--tab",
        action="store_true",
        help="Use tabs for indentation",
    )
    parser.add_argument(
        "--arg",
        nargs=2,
        action="append",
        default=[],
        metavar=("NAME", "VALUE"),
        help="Set variable $NAME to string VALUE",
    )
    parser.add_argument(
        "--argjson",
        nargs=2,
        action="append",
        default=[],
        metavar=("NAME", "JSON"),
        help="Set variable $NAME to JSON value",
    )
    parser.add_argument(
        "--slurpfile",
        nargs=2,
        action="append",
        default=[],
        metavar=("NAME", "FILE"),
        help="Set variable $NAME to array of JSON values from FILE",
    )
    parser.add_argument(
        "--rawfile",
        nargs=2,
        action="append",
        default=[],
        metavar=("NAME", "FILE"),
        help="Set variable $NAME to string contents of FILE",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Show version information",
    )

    parsed = parser.parse_args(args)

    if parsed.version:
        print("jq-shim - Python jq implementation for ai-shuffle")
        print("Version: 1.0.0")
        return 0

    # Build variables dict
    variables = {}
    for name, value in parsed.arg:
        variables[name] = value
    for name, json_value in parsed.argjson:
        try:
            variables[name] = json.loads(json_value)
        except json.JSONDecodeError as e:
            print(f"jq: error: --argjson {name}: {e}", file=sys.stderr)
            return 2
    for name, filepath in parsed.slurpfile:
        try:
            with open(filepath, "r") as f:
                content = f.read()
                # Parse as multiple JSON values
                values = []
                decoder = json.JSONDecoder()
                idx = 0
                while idx < len(content):
                    while idx < len(content) and content[idx] in " \t\n\r":
                        idx += 1
                    if idx >= len(content):
                        break
                    obj, end = decoder.raw_decode(content, idx)
                    values.append(obj)
                    idx = end
                variables[name] = values
        except (OSError, json.JSONDecodeError) as e:
            print(f"jq: error: --slurpfile {name}: {e}", file=sys.stderr)
            return 2
    for name, filepath in parsed.rawfile:
        try:
            with open(filepath, "r") as f:
                variables[name] = f.read()
        except OSError as e:
            print(f"jq: error: --rawfile {name}: {e}", file=sys.stderr)
            return 2

    # Get input data
    inputs = []
    if parsed.null_input:
        inputs = [None]
    elif parsed.files:
        for filepath in parsed.files:
            try:
                with open(filepath, "r") as f:
                    content = f.read().strip()
                    if content:
                        if parsed.slurp:
                            # Parse all JSON objects from file
                            decoder = json.JSONDecoder()
                            idx = 0
                            while idx < len(content):
                                while idx < len(content) and content[idx] in " \t\n\r":
                                    idx += 1
                                if idx >= len(content):
                                    break
                                obj, end = decoder.raw_decode(content, idx)
                                inputs.append(obj)
                                idx = end
                        else:
                            inputs.append(json.loads(content))
            except OSError as e:
                print(f"jq: error reading {filepath}: {e}", file=sys.stderr)
                return 2
            except json.JSONDecodeError as e:
                print(f"jq: error parsing {filepath}: {e}", file=sys.stderr)
                return 3
    else:
        # Read from stdin
        stdin_data = sys.stdin.read().strip()
        if stdin_data:
            try:
                if parsed.slurp:
                    # Parse all JSON objects from stdin
                    decoder = json.JSONDecoder()
                    idx = 0
                    while idx < len(stdin_data):
                        while idx < len(stdin_data) and stdin_data[idx] in " \t\n\r":
                            idx += 1
                        if idx >= len(stdin_data):
                            break
                        obj, end = decoder.raw_decode(stdin_data, idx)
                        inputs.append(obj)
                        idx = end
                else:
                    inputs.append(json.loads(stdin_data))
            except json.JSONDecodeError as e:
                print(f"jq: error parsing stdin: {e}", file=sys.stderr)
                return 3
        else:
            inputs = [None]

    # Slurp mode: combine all inputs into array
    if parsed.slurp and len(inputs) > 0:
        inputs = [inputs]

    # Parse the filter expression
    try:
        ast = parse_jq_expression(parsed.filter)
    except ParseError as e:
        print(f"jq: error: {e}", file=sys.stderr)
        return 3

    # Evaluate and output
    had_output = False
    last_result = None

    for input_data in inputs:
        try:
            result = evaluate(ast, input_data, variables)

            if result is EMPTY:
                continue

            # Handle multiple results from iteration
            results = result if (isinstance(result, list) and _is_multi_output(ast)) else [result]

            for r in results:
                if r is EMPTY:
                    continue

                had_output = True
                last_result = r
                output_result(r, parsed)

        except EvaluationError as e:
            print(f"jq: error: {e}", file=sys.stderr)
            return 5

    # Exit status based on last result
    if parsed.exit_status:
        if last_result is None or last_result is False:
            return 1

    return 0


def _is_multi_output(ast) -> bool:
    """Check if AST produces multiple outputs (has .[] in pipeline)."""
    from .parser import NodeType
    if ast.type == NodeType.ITERATE:
        return True
    if ast.type == NodeType.PIPE:
        return _is_multi_output(ast.children[0]) or _is_multi_output(ast.children[1])
    return False


def output_result(result: Any, opts: argparse.Namespace) -> None:
    """Output a single result value."""
    if result is None:
        output = "null"
    elif opts.raw_output and isinstance(result, str):
        output = result
    else:
        if opts.sort_keys:
            output = json.dumps(result, sort_keys=True, ensure_ascii=False,
                              indent=None if opts.compact_output else (None if opts.tab else 2),
                              separators=(",", ":") if opts.compact_output else None)
        else:
            if opts.compact_output:
                output = json.dumps(result, separators=(",", ":"), ensure_ascii=False)
            elif opts.tab:
                output = json.dumps(result, indent="\t", ensure_ascii=False)
            else:
                output = json.dumps(result, indent=2, ensure_ascii=False)

    if opts.join_output:
        print(output, end="")
    else:
        print(output)


if __name__ == "__main__":
    sys.exit(main())
