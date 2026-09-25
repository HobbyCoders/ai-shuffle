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
import os
import shutil
import subprocess
from typing import List, Optional, Any

# Fix Windows console encoding - Windows uses cp1252 by default which
# can't handle Unicode characters like emojis in JSON data
if sys.platform == "win32":
    # Reconfigure stdout/stderr to use UTF-8
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from .parser import parse_jq_expression, ParseError
from .evaluator import evaluate, validate, is_stream, EvaluationError, EMPTY

# Marker present near the top of every copy of this shim (bin/jq, bin/jq.bat),
# used to skip them when looking for a real jq to delegate to.
SHIM_MARKER = "Self-contained Python jq shim"


class _ArgumentError(Exception):
    """Raised instead of argparse's exit so unknown options can be delegated."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise _ArgumentError(message)


def find_real_jq() -> Optional[str]:
    """
    Find a real jq binary on PATH, skipping copies of this shim.

    Returns None when JQ_SHIM_NO_FALLBACK is set or no other jq exists.
    """
    if os.environ.get("JQ_SHIM_NO_FALLBACK"):
        return None
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = shutil.which("jq", path=directory)
        if not candidate:
            continue
        try:
            with open(candidate, "rb") as f:
                head = f.read(1024)
        except OSError:
            continue
        if SHIM_MARKER.encode() in head:
            continue
        return candidate
    return None


def _delegate_or_fail(argv: List[str], message: str, status: int) -> int:
    """Hand the whole invocation to a real jq if there is one, else report the error."""
    real_jq = find_real_jq()
    if real_jq is not None:
        sys.stdout.flush()
        # If that "real" jq is a wrapper that re-enters the shim, stop there
        env = dict(os.environ, JQ_SHIM_NO_FALLBACK="1")
        return subprocess.call([real_jq] + argv, env=env)
    print(f"jq: error: {message}", file=sys.stderr)
    return status


def _parse_json_values(text: str) -> List[Any]:
    """Parse a stream of whitespace-separated JSON values (jq's input format)."""
    values = []
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(text):
        while idx < len(text) and text[idx] in " \t\n\r":
            idx += 1
        if idx >= len(text):
            break
        obj, end = decoder.raw_decode(text, idx)
        values.append(obj)
        idx = end
    return values


def main(args: Optional[List[str]] = None) -> int:
    """Main entry point for jq shim CLI."""
    argv = list(sys.argv[1:] if args is None else args)
    parser = _ArgumentParser(
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
        help="Raw output with no newline after each output",
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

    try:
        parsed = parser.parse_args(argv)
    except _ArgumentError as e:
        # An option the shim does not implement (e.g. -R, --indent): let a
        # real jq handle the invocation if one is installed.
        return _delegate_or_fail(argv, str(e), 2)

    if parsed.version:
        print("jq-shim - Python jq implementation for ai-shuffle")
        print("Version: 1.2.0")
        return 0

    # Compile the filter before touching stdin, so that a program the shim
    # cannot run can be handed to a real jq with the input still unread.
    try:
        ast = parse_jq_expression(parsed.filter)
        validate(ast)
    except (ParseError, EvaluationError) as e:
        return _delegate_or_fail(argv, str(e), 3)

    # Build variables dict
    variables = {}
    for name, value in parsed.arg:
        variables[name] = value
    for name, json_value in parsed.argjson:
        try:
            variables[name] = json.loads(json_value)
        except (json.JSONDecodeError, RecursionError) as e:
            print(f"jq: error: --argjson {name}: {e}", file=sys.stderr)
            return 2
    for name, filepath in parsed.slurpfile:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                variables[name] = _parse_json_values(f.read())
        except (OSError, json.JSONDecodeError, RecursionError) as e:
            print(f"jq: error: --slurpfile {name}: {e}", file=sys.stderr)
            return 2
    for name, filepath in parsed.rawfile:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                variables[name] = f.read()
        except OSError as e:
            print(f"jq: error: --rawfile {name}: {e}", file=sys.stderr)
            return 2

    # Get input data (each input is a stream of JSON values, like jq)
    inputs = []
    if parsed.null_input:
        inputs = [None]
    elif parsed.files:
        for filepath in parsed.files:
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    inputs.extend(_parse_json_values(f.read()))
            except OSError as e:
                print(f"jq: error reading {filepath}: {e}", file=sys.stderr)
                return 2
            except (json.JSONDecodeError, RecursionError) as e:
                print(f"jq: error parsing {filepath}: {_input_error(e)}", file=sys.stderr)
                return 2
    else:
        try:
            inputs = _parse_json_values(sys.stdin.read())
        except (json.JSONDecodeError, RecursionError) as e:
            print(f"jq: error parsing stdin: {_input_error(e)}", file=sys.stderr)
            return 2
        if not inputs and not parsed.slurp:
            inputs = [None]

    # Slurp mode: combine all inputs into array
    if parsed.slurp:
        inputs = [inputs]

    # Evaluate and output
    last_result = None
    produced = False
    multi = is_stream(ast)

    for input_data in inputs:
        try:
            result = evaluate(ast, input_data, variables)

            if result is EMPTY:
                continue

            # Handle multiple results from generators
            results = result if multi else [result]

            for r in results:
                if r is EMPTY:
                    continue

                last_result = r
                produced = True
                output_result(r, parsed)

        except EvaluationError as e:
            print(f"jq: error: {e}", file=sys.stderr)
            return 5
        except RecursionError:
            print("jq: error: value is nested too deeply to output", file=sys.stderr)
            return 5

    # Exit status based on last result (jq -e: 1 for false/null, 4 for no output)
    if parsed.exit_status:
        if not produced:
            return 4
        if last_result is None or last_result is False:
            return 1

    return 0


def _input_error(error: Exception) -> str:
    if isinstance(error, RecursionError):
        return "input is nested too deeply"
    return str(error)


def output_result(result: Any, opts: argparse.Namespace) -> None:
    """Output a single result value."""
    if (opts.raw_output or opts.join_output) and isinstance(result, str):
        output = result
    elif opts.compact_output:
        output = json.dumps(result, separators=(",", ":"), ensure_ascii=False,
                            sort_keys=opts.sort_keys)
    else:
        output = json.dumps(result, indent="\t" if opts.tab else 2,
                            ensure_ascii=False, sort_keys=opts.sort_keys)

    if opts.join_output:
        print(output, end="")
    else:
        print(output)


if __name__ == "__main__":
    sys.exit(main())
