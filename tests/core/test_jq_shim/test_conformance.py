"""
Table-driven conformance suite for the jq shim (app/core/jq_shim).

Each case is (id, program, input JSON, expected outputs | expected error).
Expected outputs were generated with the real jq 1.7 binary; cases where the
shim intentionally differs carry ``differs=<reason>`` and assert the shim's
own behaviour instead (see the package docstring for the full list).
Cases tagged ``# regression`` failed on the shim before these fixes.

When jq 1.7.x is installed, ``test_matches_real_jq`` re-checks every
non-``differs`` case against the binary.
"""

import json
import re
import subprocess
from typing import Any, List, NamedTuple, Optional

import pytest

from app.core.jq_shim.__main__ import find_real_jq
from app.core.jq_shim.evaluator import (
    EMPTY,
    EvaluationError,
    UnsupportedError,
    evaluate,
    is_stream,
)
from app.core.jq_shim.parser import ParseError, parse_jq_expression

VARS = {'x': {'k': 1}}

O = '{"a":1,"b":"two","c":null,"d":false,"e":[1,2,3],"f":{"g":"h"},"a-b":"dash"}'
A = '[{"n":"x","v":3,"t":["p","q"]},{"n":"y","v":1,"t":[]},{"n":"z","v":2,"t":["q"]}]'


class Case(NamedTuple):
    id: str
    program: str
    input: str
    expected: Optional[List[Any]]
    error: Optional[type]
    match: Optional[str]
    differs: Optional[str]


def ok(id, program, input, expected, differs=None):
    return Case(id, program, input, expected, None, None, differs)


def err(id, program, input, error, match, differs=None):
    return Case(id, program, input, None, error, match, differs)


CASES = [
    ok('identity', '.', O, [{'a': 1, 'b': 'two', 'c': None, 'd': False, 'e': [1, 2, 3], 'f': {'g': 'h'}, 'a-b': 'dash'}]),
    ok('field', '.a', O, [1]),
    ok('field_missing', '.zz', O, [None]),
    ok('nested_field', '.f.g', O, ['h']),
    ok('nested_missing_chain', '.zz.yy.xx', O, [None]),
    ok('bracket_string_key', '.["a-b"]', O, ['dash']),
    ok('quoted_field', '."a-b"', O, ['dash']),  # new
    ok('quoted_field_postfix', '.f."g"', O, ['h']),  # new
    ok('quoted_field_interp', '."\\("a")"', O, [1]),  # new
    ok('index', '.e[0]', O, [1]),
    ok('index_negative', '.e[-1]', O, [3]),
    ok('index_out_of_range', '.e[10]', O, [None]),
    ok('index_negative_out_of_range', '.e[-10]', O, [None]),
    ok('index_float_floors', '.[1.7]', '[10,20,30]', [20]),  # regression
    ok('index_chain', '.[0][1]', '[[1,2],[3]]', [2]),
    ok('index_then_field', '.[0].n', A, ['x']),
    ok('index_expr', '.e[.a]', O, [2]),  # regression
    ok('field_on_null', '.a', 'null', [None]),
    ok('index_on_null', '.[0]', 'null', [None]),
    ok('string_key_on_null', '.["x"]', 'null', [None]),
    err('field_on_array_error', '.a', '[1]', EvaluationError, 'Cannot index array with "a"'),
    err('field_on_string_error', '.a.x', '{"a":"s"}', EvaluationError, 'Cannot index string with "x"'),
    err('number_index_on_object_error', '.[0]', '{}', EvaluationError, 'Cannot index object with number'),
    err('string_index_on_array_error', '.["a"]', '[1]', EvaluationError, 'Cannot index array with string "a"'),
    err('bool_index_error', '.[true]', '[1,2]', EvaluationError, 'Cannot index array with boolean'),  # regression
    err('null_index_error', '.[null]', '{}', EvaluationError, 'Cannot index object with null'),
    ok('optional_field_on_string', '.a?', '"s"', []),  # new
    ok('optional_nested', '.a.b?', '{"a":"s"}', []),  # new
    ok('optional_ok', '.a?', O, [1]),  # new
    ok('optional_iterate_number', '[.[]?]', '3', [[]]),  # new
    ok('optional_in_pipe', '[.[] | .a?]', '[{"a":1},"s",{"a":2}]', [[1, 2]]),  # new
    ok('optional_then_default', '.a.b? // "d"', '{"a":"s"}', ['d']),  # new
    ok('iterate_array', '.[]', '[1,2,3]', [1, 2, 3]),
    ok('iterate_object', '.[]', '{"x":1,"y":2}', [1, 2]),
    ok('iterate_empty', '.[]', '[]', []),
    ok('iterate_field', '.e[]', O, [1, 2, 3]),
    ok('iterate_then_field', '.[] | .n', A, ['x', 'y', 'z']),
    ok('iterate_dot_field', '.[].n', A, ['x', 'y', 'z']),
    ok('iterate_nested', '.[][]', '[[1,2],[3]]', [1, 2, 3]),
    ok('iterate_pipe_iterate', '.[] | .t[]', A, ['p', 'q', 'q']),
    ok('iterate_chain_three', '.[] | .n | ascii_upcase', A, ['X', 'Y', 'Z']),  # regression
    ok('iterate_chain_field_field', '.[].f.g', '[{"f":{"g":1}},{"f":{"g":2}}]', [1, 2]),  # regression
    ok('iterate_select_field', '.[] | select(.v > 1) | .n', A, ['x', 'z']),  # regression
    ok('iterate_collect', '[.[] | .v]', A, [[3, 1, 2]]),  # regression
    ok('iterate_collect_chain', '[.[] | .n | ascii_upcase]', A, [['X', 'Y', 'Z']]),  # regression
    err('iterate_number_error', '.[]', '5', EvaluationError, 'Cannot iterate over number'),
    err('iterate_string_error', '.[]', '"abc"', EvaluationError, 'Cannot iterate over string'),
    ok('iterate_null', '.[]', 'null', [], differs='.[] on null yields nothing (like .[]?); jq raises'),
    err('slice_unsupported', '.[1:2]', '[1,2,3]', ParseError, "unsupported jq syntax: slice or ':' at position 3", differs='unsupported syntax/builtin: compile error (the CLI delegates to a real jq)'),
    ok('comma', '.a, .b', O, [1, 'two']),  # new
    ok('comma_in_parens', '(.a, .b)', O, [1, 'two']),  # new
    ok('comma_then_pipe', '(.a, .b) | tostring', O, ['1', 'two']),  # new
    ok('comma_binds_tighter_than_pipe', '.f | .g, .a', O, ['h', None]),  # new
    ok('comma_with_iterate', '.e[], .a', O, [1, 2, 3, 1]),  # new
    ok('comma_in_array_with_pipe', '[.e[] | ., 0]', O, [[1, 0, 2, 0, 3, 0]]),  # regression
    ok('comma_first', 'first(.zz, .a)', O, [None]),  # new
    err('comma_in_select_unsupported', 'select(.a, .b)', O, UnsupportedError, 'generators such as .[] are not supported inside the arguments of select(); collect them with [...] or map() first', differs='generator in a position the shim cannot fan out: compile error'),
    err('comma_in_object_value_parens_unsupported', '{x: (1, 2)}', 'null', UnsupportedError, 'generators such as .[] are not supported inside object construction; collect them with [...] or map() first', differs='generator in a position the shim cannot fan out: compile error'),
    ok('comma_array_trailing', '[1, 2,]', 'null', [[1, 2]], differs='trailing comma tolerated; jq syntax error'),
    ok('postfix_index_uses_dot', '.e[.a]', O, [2]),  # regression
    ok('postfix_index_on_iterate', '[.[][0]]', '[[1,2],[3]]', [[1, 3]]),  # regression
    ok('postfix_index_after_field_iter', '[.[].t[0]]', A, [['p', None, 'q']]),  # regression
    ok('postfix_index_computed_key', '.f[.b | "g"]', O, ['h']),
    err('postfix_index_generator_unsupported', '.e[.e[]]', O, UnsupportedError, 'generators such as .[] are not supported inside an index expression; collect them with [...] or map() first', differs='generator in a position the shim cannot fan out: compile error'),
    err('plus_unsupported', '.a + 1', O, ParseError, "unsupported jq syntax: arithmetic operator '+' at position 3", differs='unsupported syntax/builtin: compile error (the CLI delegates to a real jq)'),
    err('minus_unsupported', '.a - 1', O, ParseError, "unsupported jq syntax: arithmetic operator '-' at position 3", differs='unsupported syntax/builtin: compile error (the CLI delegates to a real jq)'),
    err('times_unsupported', '2 * 3', 'null', ParseError, "unsupported jq syntax: arithmetic operator '*' at position 2", differs='unsupported syntax/builtin: compile error (the CLI delegates to a real jq)'),
    err('division_unsupported', '1 / 0', 'null', ParseError, "unsupported jq syntax: arithmetic operator '/' at position 2"),
    err('modulo_unsupported', '5 % 0', 'null', ParseError, "unsupported jq syntax: arithmetic operator '%' at position 2"),
    err('if_unsupported', 'if .a then "y" else "n" end', O, ParseError, "unsupported jq syntax: 'if' at position 0", differs='unsupported syntax/builtin: compile error (the CLI delegates to a real jq)'),
    err('try_unsupported', 'try error("x") catch .', 'null', ParseError, "unsupported jq syntax: 'try' at position 0", differs='unsupported syntax/builtin: compile error (the CLI delegates to a real jq)'),
    err('reduce_unsupported', 'reduce .[] as $x (0; . + $x)', '[1,2]', ParseError, "unsupported jq syntax: 'reduce' at position 0", differs='unsupported syntax/builtin: compile error (the CLI delegates to a real jq)'),
    err('as_unsupported', '. as $x | $x', '1', ParseError, "unsupported jq syntax: 'as' at position 2", differs='unsupported syntax/builtin: compile error (the CLI delegates to a real jq)'),
    err('def_unsupported', 'def f: .; f', '1', ParseError, "unsupported jq syntax: 'def' at position 0", differs='unsupported syntax/builtin: compile error (the CLI delegates to a real jq)'),
    err('format_base64_unsupported', '@base64', '"hi"', ParseError, 'unsupported jq syntax: @format strings at position 0', differs='unsupported syntax/builtin: compile error (the CLI delegates to a real jq)'),
    err('format_json_unsupported', '@json', '[1]', ParseError, 'unsupported jq syntax: @format strings at position 0', differs='unsupported syntax/builtin: compile error (the CLI delegates to a real jq)'),
    err('assignment_unsupported', '.a = 1', '{}', ParseError, 'unsupported jq syntax: assignment operator at position 3', differs='unsupported syntax/builtin: compile error (the CLI delegates to a real jq)'),
    err('update_assignment_unsupported', '.a |= 1', '{}', ParseError, 'unsupported jq syntax: assignment operator at position 3', differs='unsupported syntax/builtin: compile error (the CLI delegates to a real jq)'),
    err('chained_comparison_error', '1 < 2 < 3', 'null', ParseError, "Unexpected character at position 6: '<'"),
    ok('pipe', '. | .a', O, [1]),
    ok('pipe_chain', '.f | .g', O, ['h']),
    ok('parens', '(.a)', O, [1]),
    ok('parens_pipe', '(.f | .g)', O, ['h']),
    ok('parens_nested', '((((1))))', 'null', [1]),
    ok('literal_number', '42', 'null', [42]),
    ok('literal_float', '1.5', 'null', [1.5]),
    ok('literal_negative', '-3', 'null', [-3]),
    ok('literal_exponent', '1e3', 'null', [1000.0]),
    ok('literal_string', '"hi"', 'null', ['hi']),
    ok('literal_true', 'true', 'null', [True]),
    ok('literal_null', 'null', '1', [None]),
    ok('negate_paren', '-(.a)', O, [-1]),
    ok('negate_field', '-.a', O, [-1]),  # regression
    err('negate_string_error', '-.b', O, EvaluationError, 'string cannot be negated'),  # regression
    ok('empty_program', '', '[1]', [[1]], differs='an empty program is the identity (jq 1.6); jq 1.7 errors'),
    ok('interp', '"a=\\(.a) b=\\(.b)"', O, ['a=1 b=two']),
    ok('interp_null_bool', '"\\(.c)-\\(.d)"', O, ['null-false']),
    ok('interp_array_object', '"\\(.e) \\(.f)"', O, ['[1,2,3] {"g":"h"}']),  # regression
    ok('interp_float', '"\\(.)"', '1.5', ['1.5']),
    ok('interp_nested', '"x\\("y\\(.a)")"', O, ['xy1']),
    ok('interp_empty', '"x\\(empty)"', 'null', []),  # regression
    ok('escapes', '"a\\tb\\nc\\"d\\\\e\\/f"', 'null', ['a\tb\nc"d\\e/f']),
    ok('unicode_escape', '"\\u00e9\\u0041"', 'null', ['éA']),  # regression
    ok('surrogate_escape', '"\\ud83d\\ude00"', 'null', ['😀']),  # regression
    err('bad_escape_error', '"\\q"', 'null', ParseError, "Invalid escape '\\q' at position 2"),  # regression
    err('unterminated_string_error', '"abc', 'null', ParseError, 'Expected \'"\' but reached end of input'),
    ok('eq_num', '.a == 1', O, [True]),
    ok('eq_int_float', '1 == 1.0', 'null', [True]),
    ok('eq_num_bool', '1 == true', 'null', [False]),  # regression
    ok('ne', '.b != "two"', O, [False]),
    ok('eq_arrays', '[1,[2]] == [1,[2]]', 'null', [True]),
    ok('eq_objects_order', '{"a":1,"b":2} == {"b":2,"a":1}', 'null', [True]),
    ok('lt_num', '1 < 2', 'null', [True]),
    ok('le_num', '2 <= 2', 'null', [True]),
    ok('gt_string', '"b" > "a"', 'null', [True]),
    ok('ge_string', '"a" >= "b"', 'null', [False]),
    ok('lt_null_false', 'null < false', 'null', [True]),  # regression
    ok('lt_false_true', 'false < true', 'null', [True]),
    ok('lt_true_number', 'true < 0', 'null', [True]),  # regression
    ok('lt_number_string', '99 < "a"', 'null', [True]),  # regression
    ok('lt_string_array', '"z" < []', 'null', [True]),  # regression
    ok('lt_array_object', '[] < {}', 'null', [True]),  # regression
    ok('lt_arrays_lexicographic', '[1,2] < [1,3]', 'null', [True]),
    ok('lt_objects_by_keys', '{"a":2} < {"b":1}', 'null', [True]),  # regression
    ok('lt_objects_by_values', '{"a":1} < {"a":2}', 'null', [True]),  # regression
    ok('and_tt', 'true and true', 'null', [True]),
    ok('and_tf', 'true and false', 'null', [False]),
    ok('and_null', '1 and null', 'null', [False]),
    ok('or_ff', 'false or null', 'null', [False]),
    ok('or_tf', '.a or .zz', O, [True]),
    ok('or_and_precedence', 'true or false and false', 'null', [True]),  # regression
    ok('and_or_precedence', 'false and true or true', 'null', [True]),
    ok('not_filter', '.d | not', O, [True]),  # regression
    ok('not_filter_truthy', '.e | not', O, [False]),  # regression
    ok('not_zero', '0 | not', 'null', [False]),  # regression
    ok('not_in_select', '[.[] | select(.t | length == 0 | not) | .n]', A, [['x', 'z']]),  # regression
    ok('not_then_and', '.d | not and true', O, [True]),  # regression
    ok('not_prefix_shim_extension', 'not .d', O, [True], differs='shim extension: `not f` is prefix negation; jq reads `not | f`'),
    ok('comparison_and', '.a == 1 and .b == "two"', O, [True]),
    ok('alt_present', '.a // "d"', O, [1]),
    ok('alt_null', '.c // "d"', O, ['d']),
    ok('alt_false', '.d // "d"', O, ['d']),
    ok('alt_missing', '.zz // "d"', O, ['d']),
    ok('alt_zero_kept', '0 // "d"', 'null', [0]),
    ok('alt_empty_string_kept', '"" // "d"', 'null', ['']),
    ok('alt_empty_array_kept', '[] // "d"', 'null', [[]]),
    ok('alt_chain', '.c // .d // .a', O, [1]),
    ok('alt_empty_lhs', '(empty) // 1', 'null', [1]),
    ok('alt_to_empty', '.zz // empty', O, []),
    err('alt_error_not_swallowed', '.a.b // "d"', '{"a":"s"}', EvaluationError, 'Cannot index string with "b"'),
    ok('select_true', 'select(.a == 1)', O, [{'a': 1, 'b': 'two', 'c': None, 'd': False, 'e': [1, 2, 3], 'f': {'g': 'h'}, 'a-b': 'dash'}]),
    ok('select_false', 'select(.a == 2)', O, []),
    ok('select_iter', '[.[] | select(.v >= 2)]', A, [[{'n': 'x', 'v': 3, 't': ['p', 'q']}, {'n': 'z', 'v': 2, 't': ['q']}]]),  # regression
    ok('select_null_falsy', '[.[] | select(.)]', '[1,null,false,0,""]', [[1, 0, '']]),  # regression
    ok('map', 'map(.v)', A, [[3, 1, 2]]),
    ok('map_object', 'map(not)', '{"a":1}', [[False]]),  # regression
    ok('map_object_values', 'map(tostring)', '{"a":1,"b":true}', [['1', 'true']]),  # regression
    ok('map_select', 'map(select(.v > 1))', A, [[{'n': 'x', 'v': 3, 't': ['p', 'q']}, {'n': 'z', 'v': 2, 't': ['q']}]]),
    ok('map_iterate_flattens', 'map(.[])', '[[1,2],[3]]', [[1, 2, 3]]),  # regression
    ok('map_iterate_field', 'map(.t[])', A, [['p', 'q', 'q']]),  # regression
    err('map_on_number_error', 'map(.)', '5', EvaluationError, 'Cannot iterate over number'),
    ok('map_values_object', 'map_values(tostring)', '{"a":1,"b":[2]}', [{'a': '1', 'b': '[2]'}]),  # new
    ok('map_values_array', 'map_values(. > 1)', '[1,2]', [[False, True]]),  # new
    ok('map_values_empty_drops', 'map_values(select(. != null))', '{"a":1,"b":null}', [{'a': 1}]),  # new
    ok('map_values_first_output', 'map_values(.[])', '{"a":[1,2],"b":[]}', [{'a': 1}]),  # new
    ok('has_key', 'has("a")', O, [True]),
    ok('has_missing', 'has("zz")', O, [False]),
    ok('has_null_valued', 'has("c")', O, [True]),
    ok('has_index', 'has(1)', '[5,6]', [True]),
    ok('has_index_out', 'has(2)', '[5,6]', [False]),
    err('has_wrong_type_error', 'has(0)', '{}', EvaluationError, 'Cannot check whether object has a number key'),  # regression
    err('has_on_array_string_error', 'has("a")', '[1]', EvaluationError, 'Cannot check whether array has a string key'),  # regression
    ok('in_object', 'in({"a":1})', '"a"', [True]),
    ok('in_array', 'in([5])', '0', [True]),  # regression
    ok('in_missing', 'in({"a":1})', '"b"', [False]),
    ok('keys_sorted', 'keys', '{"b":1,"a":2,"C":3}', [['C', 'a', 'b']]),
    ok('keys_array', 'keys', '[5,6,7]', [[0, 1, 2]]),
    ok('keys_unsorted', 'keys_unsorted', '{"b":1,"a":2}', [['b', 'a']]),
    err('keys_null_error', 'keys', 'null', EvaluationError, 'null (null) has no keys'),
    ok('length_string', 'length', '"héllo"', [5]),
    ok('length_array', 'length', '[1,2]', [2]),
    ok('length_object', 'length', '{"a":1}', [1]),
    ok('length_null', 'length', 'null', [0]),
    ok('length_number_abs', 'length', '-3.5', [3.5]),  # regression
    err('length_bool_error', 'length', 'true', EvaluationError, 'boolean (true) has no length'),
    ok('add_numbers', 'add', '[1,2,3.5]', [6.5]),
    ok('add_strings', 'add', '["a","b"]', ['ab']),
    ok('add_arrays', 'add', '[[1],[2,3]]', [[1, 2, 3]]),
    ok('add_objects', 'add', '[{"a":1},{"b":2},{"a":3}]', [{'a': 3, 'b': 2}]),
    ok('add_empty', 'add', '[]', [None]),
    ok('add_nulls', 'add', '[null,1,null]', [1]),  # regression
    ok('add_object_input', 'add', '{"a":1,"b":2}', [3]),  # regression
    err('add_null_input', 'add', 'null', EvaluationError, 'Cannot iterate over null'),
    err('add_mixed_error', 'add', '[1,"a"]', EvaluationError, 'number (1) and string ("a") cannot be added'),
    ok('join', 'join(", ")', '["a","b","c"]', ['a, b, c']),
    ok('join_mixed', 'join("-")', '["a",1,null,true]', ['a-1--true']),  # regression
    ok('join_empty', 'join(",")', '[]', ['']),
    err('join_nested_error', 'join(",")', '[[1]]', EvaluationError, 'Cannot join with array ([1])'),  # regression
    ok('split', 'split(",")', '"a,b,,c"', [['a', 'b', '', 'c']]),
    ok('split_empty_sep', 'split("")', '"abc"', [['a', 'b', 'c']]),  # regression (crashed)
    err('split_number_error', 'split(",")', '1', EvaluationError, 'split input and separator must be strings'),
    ok('ascii_downcase', 'ascii_downcase', '"HeLLo ÀB"', ['hello Àb']),  # regression
    ok('ascii_upcase', 'ascii_upcase', '"hello é"', ['HELLO é']),  # regression
    err('ascii_downcase_error', 'ascii_downcase', '1', EvaluationError, 'number cannot be lowercased, as it is not a string'),
    ok('ltrimstr', 'ltrimstr("foo")', '"foobar"', ['bar']),
    ok('ltrimstr_nomatch', 'ltrimstr("x")', '"foobar"', ['foobar']),
    ok('ltrimstr_non_string', 'ltrimstr("x")', 'null', [None]),  # regression
    ok('rtrimstr', 'rtrimstr("bar")', '"foobar"', ['foo']),
    ok('rtrimstr_non_string_arg', 'rtrimstr(1)', '"foobar"', ['foobar']),  # regression
    ok('startswith', 'startswith("foo")', '"foobar"', [True]),
    ok('endswith', 'endswith("foo")', '"foobar"', [False]),
    err('startswith_error', 'startswith("a")', '1', EvaluationError, 'startswith() requires string inputs'),
    ok('tostring_string', 'tostring', '"s"', ['s']),
    ok('tostring_number', 'tostring', '1.5', ['1.5']),
    ok('tostring_array', 'tostring', '[1,{"a":"é"}]', ['[1,{"a":"é"}]']),  # regression
    ok('tostring_null', 'tostring', 'null', ['null']),
    ok('tojson', 'tojson', '{"a":[1,"x"]}', ['{"a":[1,"x"]}']),  # new
    ok('tojson_string', 'tojson', '"s"', ['"s"']),  # new
    ok('fromjson', 'fromjson', '"{\\"a\\":[1,2]}"', [{'a': [1, 2]}]),  # new
    err('fromjson_error', 'fromjson', '"{nope"', EvaluationError, "'{nope' cannot be parsed as JSON: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"),
    ok('tonumber_int', 'tonumber', '"42"', [42]),
    ok('tonumber_float', 'tonumber', '"-3.25"', [-3.25]),
    ok('tonumber_exp', 'tonumber', '"1e2"', [100.0]),
    ok('tonumber_number', 'tonumber', '7', [7]),
    err('tonumber_bad_error', 'tonumber', '"12abc"', EvaluationError, "Cannot parse '12abc' as a number"),
    err('tonumber_underscore_error', 'tonumber', '"1_000"', EvaluationError, "Cannot parse '1_000' as a number"),  # regression
    err('tonumber_bool_error', 'tonumber', 'true', EvaluationError, 'boolean (true) cannot be parsed as a number'),  # regression
    ok('type_all', '[.[] | type]', '[null,true,1,"s",[],{}]', [['null', 'boolean', 'number', 'string', 'array', 'object']]),  # regression
    ok('test_regex', 'test("b+")', '"abbc"', [True]),
    ok('test_regex_flags', 'test("B"; "i")', '"abc"', [True]),  # new
    err('test_regex_invalid_error', 'test("[")', '"a"', EvaluationError, '[ (at offset 0) is not a valid regex: unterminated character set'),  # regression (crashed)
    ok('match', 'match("(b+)(?<x>c)")', '"abbc"', [{'offset': 1, 'length': 3, 'string': 'bbc', 'captures': [{'offset': 1, 'length': 2, 'string': 'bb', 'name': None}, {'offset': 3, 'length': 1, 'string': 'c', 'name': 'x'}]}]),  # regression (crashed)
    ok('match_none', 'match("z")', '"abc"', []),  # regression
    ok('capture', 'capture("(?<k>[a-z]+)=(?<v>\\\\d+)")', '"key=42"', [{'k': 'key', 'v': '42'}]),  # regression (crashed)
    ok('capture_none', 'capture("(?<k>z)")', '"abc"', []),  # regression (crashed)
    ok('sub', 'sub("o"; "0")', '"foo"', ['f0o']),
    ok('gsub', 'gsub("o"; "0")', '"foo"', ['f00']),
    ok('gsub_backslash_literal', 'gsub("a"; "\\\\")', '"aa"', ['\\\\']),  # regression (crashed)
    ok('sub_named_capture', 'sub("(?<x>[a-z]+)"; "<\\(.x)>")', '"ab12"', ['<ab>12']),  # regression
    ok('sub_global_flag', 'sub("a"; "b"; "g")', '"aa"', ['bb']),  # new
    ok('empty', 'empty', '1', []),
    ok('empty_in_array', '[1, empty, 2]', 'null', [[1, 2]]),
    ok('empty_in_object', '{a: empty}', 'null', []),  # regression (crashed)
    ok('empty_arg', 'has(empty)', '{}', []),  # regression
    ok('empty_comparison', 'empty == 1', 'null', []),  # regression
    err('error_string', 'error("boom")', 'null', EvaluationError, 'boom'),
    err('error_input', 'error', '"msg"', EvaluationError, 'msg'),
    err('error_object', 'error', '{"a":1}', EvaluationError, '{"a":1} (not a string)'),
    ok('error_suppressed_by_optional', '[error("x")?]', 'null', [[]]),  # new
    ok('to_entries', 'to_entries', '{"a":1,"b":false}', [[{'key': 'a', 'value': 1}, {'key': 'b', 'value': False}]]),
    ok('to_entries_array', 'to_entries', '["x"]', [[{'key': 0, 'value': 'x'}]]),  # regression
    ok('from_entries', 'from_entries', '[{"key":"a","value":1},{"k":"b","v":2},{"name":"c","value":null}]', [{'a': 1, 'b': 2, 'c': None}], differs='shim accepts k/v entries; jq 1.7 does not'),
    ok('from_entries_falsy_values', 'from_entries', '[{"key":"a","value":false},{"key":"b","value":0},{"key":"c","value":""}]', [{'a': False, 'b': 0, 'c': ''}]),  # regression
    ok('from_entries_numeric_key', 'from_entries', '[{"key":1,"value":"x"}]', [{'1': 'x'}], differs='shim stringifies non-string keys; jq 1.7 errors'),
    ok('with_entries_identity', 'with_entries(.)', '{"a":false,"b":0}', [{'a': False, 'b': 0}]),  # regression
    ok('with_entries_select', 'with_entries(select(.value > 1))', '{"a":1,"b":2,"c":3}', [{'b': 2, 'c': 3}]),
    ok('with_entries_rename', 'with_entries({key: (.key | ascii_upcase), value})', '{"a":1}', [{'A': 1}]),  # new
    ok('object_simple', '{x: .a, y: .b}', O, [{'x': 1, 'y': 'two'}]),
    ok('object_string_key', '{"x y": .a}', O, [{'x y': 1}]),
    ok('object_interp_key', '{"k\\(.a)": 1}', O, [{'k1': 1}]),
    ok('object_computed_key', '{(.b): .a}', O, [{'two': 1}]),
    err('object_computed_key_non_string_error', '{(.a): 1}', O, EvaluationError, 'Cannot use number (1) as object key'),  # regression
    ok('object_shorthand', '{a, b}', O, [{'a': 1, 'b': 'two'}]),  # new
    ok('object_shorthand_string', '{"a-b"}', O, [{'a-b': 'dash'}]),  # new
    ok('object_var_shorthand', '{$x}', O, [{'x': {'k': 1}}]),  # new
    ok('object_nested', '{x: {y: .f.g}}', O, [{'x': {'y': 'h'}}]),
    ok('object_pipe_value', '{x: .f | .g}', O, [{'x': 'h'}]),
    ok('object_keyword_key', '{if: 1, then: 2}', 'null', [{'if': 1, 'then': 2}]),
    ok('object_trailing_comma', '{a: 1,}', 'null', [{'a': 1}]),
    ok('object_empty', '{}', 'null', [{}]),
    err('object_generator_unsupported', '{x: .e[]}', O, UnsupportedError, 'generators such as .[] are not supported inside object construction; collect them with [...] or map() first', differs='generator in a position the shim cannot fan out: compile error'),  # regression (was silently wrong)
    ok('array_items', '[.a, .b, .c]', O, [[1, 'two', None]]),
    ok('array_empty', '[]', 'null', [[]]),
    ok('array_nested', '[[.a], [.b]]', O, [[[1], ['two']]]),
    ok('array_pipe_items', '[.f | .g, .a]', O, [['h', None]]),  # regression
    ok('array_of_iterate', '[.e[]]', O, [[1, 2, 3]]),
    ok('array_iterate_and_items', '[.e[], .a]', O, [[1, 2, 3, 1]]),
    ok('recurse_dotdot', '[..]', '[1,[2,{"a":3}]]', [[[1, [2, {'a': 3}]], 1, [2, {'a': 3}], 2, {'a': 3}, 3]]),  # new
    ok('recurse_dotdot_nulls', '[..]', '{"a":null}', [[{'a': None}, None]]),  # new
    ok('recurse_dotdot_field', '[.. | .a? | select(. != null)]', '{"a":1,"b":{"a":2},"c":[{"a":3}]}', [[1, 2, 3]]),  # new
    ok('recurse_named', '[recurse]', '[[1]]', [[[[1]], [1], 1]]),  # regression
    ok('recurse_generator_arg', '[recurse(.c[]?) | .n]', '{"n":1,"c":[{"n":2,"c":[{"n":3}]},{"n":4}]}', [[1, 2, 3, 4]]),  # new
    ok('recurse_null_children_shim', '[recurse(.a)]', '{"a":{"a":null}}', [[{'a': {'a': None}}, {'a': None}]], differs='recurse(f) drops null children and terminates; jq loops forever'),  # regression
    ok('walk_identity', 'walk(.)', '[1,{"a":[2]}]', [[1, {'a': [2]}]]),
    ok('walk_strings', 'walk(ascii_downcase? // .)', '{"A":["X",{"B":"Y"}]}', [{'A': ['x', {'B': 'y'}]}]),  # new
    ok('walk_drop_nulls', 'walk(select(. != null))', '{"a":null,"b":[null,1]}', [{'b': [1]}]),  # regression (crashed)
    ok('env_type', 'env | type', 'null', ['object']),
    ok('env_var_type', '$ENV | type', 'null', ['object']),  # regression
    ok('env_path_string', '$ENV.PATH | type', 'null', ['string']),  # regression
    ok('loc', '$__loc__', 'null', [{'file': '<top-level>', 'line': 1}]),  # regression
    ok('var', '$x', 'null', [{'k': 1}]),
    ok('var_in_object', '{v: $x}', 'null', [{'v': {'k': 1}}]),
    err('undefined_var_error', '$nope', 'null', EvaluationError, 'Variable $nope is not defined'),
    ok('first', 'first', '[1,2]', [1]),
    ok('last', 'last', '[1,2]', [2]),
    ok('first_empty', 'first', '[]', [None]),
    ok('first_null', 'first', 'null', [None]),
    err('first_object_error', 'first', '{"a":1}', EvaluationError, 'Cannot index object with number'),  # regression
    ok('first_generator', 'first(.[] | select(. > 1))', '[1,2,3]', [2]),  # regression
    ok('last_generator', 'last(.[])', '[1,2,3]', [3]),
    ok('first_generator_empty', '[first(empty)]', 'null', [[]]),  # regression
    ok('nth', 'nth(1)', '[1,2]', [2]),
    ok('nth_negative', 'nth(-1)', '[1,2]', [2]),  # regression
    ok('nth_out', 'nth(5)', '[1,2]', [None]),
    ok('min', 'min', '[3,1,2]', [1]),
    ok('max', 'max', '[3,"a",1]', ['a']),
    ok('min_empty', 'min', '[]', [None]),
    ok('sort', 'sort', '[3,"a",null,true,false,[1],{"a":1},1]', [[None, False, True, 1, 3, 'a', [1], {'a': 1}]]),
    ok('sort_objects', 'sort', '[{"b":1},{"a":2},{"a":1}]', [[{'a': 1}, {'a': 2}, {'b': 1}]]),
    ok('sort_by', 'sort_by(.v)', A, [[{'n': 'y', 'v': 1, 't': []}, {'n': 'z', 'v': 2, 't': ['q']}, {'n': 'x', 'v': 3, 't': ['p', 'q']}]]),
    ok('sort_by_stable', 'sort_by(.k) | map(.i)', '[{"k":1,"i":0},{"k":0,"i":1},{"k":1,"i":2}]', [[1, 0, 2]]),
    ok('unique', 'unique', '[3,1,3,"a",null,1]', [[None, 1, 3, 'a']]),  # regression
    ok('unique_int_float', 'unique', '[1,1.0,true]', [[True, 1]]),  # regression
    ok('group_by', 'group_by(.a) | map(length)', '[{"a":2},{"a":1},{"a":2}]', [[1, 2]]),  # regression
    ok('group_by_mixed_keys', 'group_by(.) ', '[[1],"[1]",[1]]', [[['[1]'], [[1], [1]]]]),  # regression
    ok('flatten', 'flatten', '[1,[2,[3,[4]]]]', [[1, 2, 3, 4]]),  # regression
    ok('flatten_depth', 'flatten(1)', '[1,[2,[3]]]', [[1, 2, [3]]]),
    err('flatten_negative_error', 'flatten(-1)', '[1]', EvaluationError, 'flatten depth must not be negative'),  # regression
    ok('reverse_array', 'reverse', '[1,2,3]', [[3, 2, 1]]),
    ok('reverse_null', 'reverse', 'null', [[]]),  # regression
    ok('contains_string', 'contains("bar")', '"foobar"', [True]),
    ok('contains_object', 'contains({a: [1]})', '{"a":[1,2],"b":3}', [True]),
    ok('contains_array', 'contains([1])', '[1,2]', [True]),
    ok('contains_bool_vs_number', 'contains([1])', '[true]', [False]),  # regression
    err('contains_kind_error', 'contains(1)', 'true', EvaluationError, 'boolean (true) and number (1) cannot have their containment checked'),  # regression
    ok('inside', 'inside("foobar")', '"bar"', [True]),
    ok('getpath', 'getpath(["a","b"])', '{"a":{"b":5}}', [5]),
    ok('getpath_missing', 'getpath(["x",0])', '{"a":1}', [None]),
    err('getpath_type_error', 'getpath(["a","b"])', '{"a":1}', EvaluationError, 'Cannot index number with string'),  # regression
    ok('any', 'any', '[false,1]', [True]),
    ok('all', 'all', '[true,1]', [True]),
    ok('any_empty', 'any', '[]', [False]),
    ok('all_empty', 'all', '[]', [True]),
    ok('any_object', 'any', '{"a":false}', [False]),  # regression
    ok('any_cond', 'any(. > 2)', '[1,3]', [True]),
    ok('all_cond', 'all(. > 2)', '[1,3]', [False]),
    ok('floor', 'floor', '3.7', [3]),
    ok('ceil', 'ceil', '3.2', [4]),
    ok('round_half_up', 'round', '2.5', [3]),  # regression
    ok('round_half_negative', 'round', '-2.5', [-3]),  # regression
    err('floor_string_error', 'floor', '"x"', EvaluationError, 'string ("x") number required for floor'),
    ok('isnan', '[nan | isnan, (1 | isnan)]', 'null', [[True, False]]),
    ok('isinfinite', '[infinite | isinfinite, (1 | isinfinite)]', 'null', [[True, False]]),
    ok('isnormal', '[0, 1] | map(isnormal)', 'null', [[False, True]]),
    ok('values_filter', '[.[] | values]', '[1,null,false]', [[1, False]]),  # regression
    ok('select_type', '[.[] | select(type == "string")]', '[1,"a",null,"b"]', [['a', 'b']]),  # regression
    err('unknown_function', 'frobnicate', 'null', UnsupportedError, 'frobnicate/0 is not defined'),
    err('unknown_arity', 'length(1)', 'null', UnsupportedError, 'length/1 is not defined'),  # regression
    err('range_unsupported', '[range(10)]', 'null', UnsupportedError, 'range/1 is not defined', differs='unsupported syntax/builtin: compile error (the CLI delegates to a real jq)'),
    err('limit_unsupported', '[limit(1; .[])]', '[1,2]', UnsupportedError, 'limit/2 is not defined', differs='unsupported syntax/builtin: compile error (the CLI delegates to a real jq)'),
    err('until_unsupported', 'until(. > 3; . + 1)', '0', ParseError, "unsupported jq syntax: arithmetic operator '+' at position 15", differs='unsupported syntax/builtin: compile error (the CLI delegates to a real jq)'),
    err('unclosed_paren', '(.a', 'null', ParseError, "Expected ')' but reached end of input"),
    err('unclosed_bracket', '[1, 2', 'null', ParseError, "Expected ']' but reached end of input"),
    err('trailing_garbage', '.a b', O, ParseError, "Unexpected character at position 3: 'b'"),
    err('generator_in_comparison_unsupported', 'select(.e[] == 2)', O, UnsupportedError, 'generators such as .[] are not supported inside a comparison expression; collect them with [...] or map() first', differs='generator in a position the shim cannot fan out: compile error'),  # regression (was silently wrong)
    err('generator_in_interp_unsupported', '"\\(.e[])"', O, UnsupportedError, 'generators such as .[] are not supported inside string interpolation; collect them with [...] or map() first', differs='generator in a position the shim cannot fan out: compile error'),  # regression (was silently wrong)
    ok('empty_in_computed_key', '{(empty): 1}', 'null', []),  # regression
    ok('empty_in_index', '[.[empty]]', '[1]', [[]]),  # regression
    ok('empty_index_target', '[(empty)[0]]', 'null', [[]]),
    ok('empty_negate', '[-(empty)]', 'null', [[]]),  # regression
    ok('empty_and_lhs', '[empty and true]', 'null', [[]]),  # regression
    ok('empty_and_rhs', '[true and empty]', 'null', [[]]),  # regression
    ok('empty_or_lhs', '[empty or true]', 'null', [[]]),  # regression
    ok('empty_or_rhs', '[false or empty]', 'null', [[]]),  # regression
    ok('empty_comparison_rhs', '[1 == empty]', 'null', [[]]),  # regression
    ok('empty_not_prefix_shim', '[not empty]', 'null', [[]], differs='shim extension: prefix `not f`'),  # regression
    ok('negate_negative', '-(-1)', 'null', [1]),
    err('negate_bool_error', '-true', 'null', EvaluationError, 'boolean cannot be negated'),  # regression
    err('map_values_non_container_error', 'map_values(.)', '"s"', EvaluationError, 'Cannot iterate over string'),
    ok('reverse_string_shim', 'reverse', '"abc"', ['cba'], differs='shim reverses strings (jq 1.7.1+); jq 1.7 errors'),
    err('reverse_number_error', 'reverse', '1', EvaluationError, 'Cannot reverse number'),
    err('sort_non_array_error', 'sort', '{}', EvaluationError, 'object cannot be sorted, as it is not an array'),
    err('sort_by_non_array_error', 'sort_by(.)', '1', EvaluationError, 'number cannot be sorted, as it is not an array'),
    err('unique_non_array_error', 'unique', '1', EvaluationError, 'number cannot be sorted, as it is not an array'),
    ok('flatten_zero', 'flatten(0)', '[[1]]', [[[1]]]),
    err('flatten_non_array_error', 'flatten', '1', EvaluationError, 'Cannot flatten number'),
    err('flatten_depth_string_error', 'flatten("a")', '[1]', EvaluationError, 'flatten depth must be a number', differs='shim requires a numeric depth; jq 1.7 ignores it'),  # regression (crashed)
    ok('last_null', 'last', 'null', [None]),
    ok('nth_null', 'nth(0)', 'null', [None]),
    err('nth_object_error', 'nth(0)', '{}', EvaluationError, 'Cannot index object with number'),  # regression
    err('nth_string_index_error', 'nth("a")', '[1]', EvaluationError, 'Cannot index array with string'),  # regression (crashed)
    ok('first_value_arg', 'first(.a)', '{"a":5}', [5]),  # regression
    ok('last_empty_arg', '[last(empty)]', 'null', [[None]]),
    ok('getpath_negative', 'getpath([-1])', '[1,2]', [2]),  # regression
    err('getpath_non_array_error', 'getpath("a")', '{}', EvaluationError, 'Path must be specified as an array'),
    err('fromjson_non_string_error', 'fromjson', '1', EvaluationError, 'number (1) cannot be parsed as JSON'),
    err('ascii_upcase_error', 'ascii_upcase', '1', EvaluationError, 'number cannot be uppercased, as it is not a string'),
    err('endswith_error', 'endswith("a")', '1', EvaluationError, 'endswith() requires string inputs'),
    err('join_non_array_error', 'join(",")', '"a"', EvaluationError, 'Cannot iterate over string'),
    err('join_sep_error', 'join(1)', '["a"]', EvaluationError, 'join separator must be a string', differs='shim requires a string separator; jq 1.7 only fails once one is used'),
    err('test_non_string_error', 'test("a")', '1', EvaluationError, 'number (1) cannot be matched, as it is not a string'),
    err('sub_non_string_error', 'sub("a"; "b")', '1', EvaluationError, 'number (1) cannot be matched, as it is not a string'),
    err('sub_replacement_non_string_error', 'sub("a"; 1)', '"a"', EvaluationError, 'number (1) cannot be added to a string'),
    ok('test_flag_x', 'test("a b"; "x")', '"ab"', [True]),  # new
    ok('test_flag_s_is_default', 'test("a.b"; "s")', '"a\\nb"', [False]),  # new
    ok('test_flag_p_dot_matches_newline', 'test("a.b"; "p")', '"a\\nb"', [True]),  # new
    ok('test_flag_n', 'test("a"; "n")', '"a"', [True]),  # new
    err('test_flag_bad_error', 'test("a"; "q")', '"a"', EvaluationError, 'q is not a valid modifier string'),
    err('test_flag_non_string_error', 'test("a"; 1)', '"a"', EvaluationError, '1 is not a string'),
    err('match_global_unsupported', 'match("a"; "g")', '"aa"', UnsupportedError, "the 'g' flag of match/capture (multiple outputs) is not supported", differs='unsupported syntax/builtin: compile error (the CLI delegates to a real jq)'),
    ok('gsub_named_capture', 'gsub("(?<d>[0-9])"; "<\\(.d)>")', '"a1b2"', ['a<1>b<2>']),  # regression
    ok('debug_passthrough', 'debug', '1', [1]),
    err('group_by_non_array_error', 'group_by(.)', '1', EvaluationError, 'Cannot index number with number'),
    err('any_non_iterable_error', 'any', '1', EvaluationError, 'Cannot iterate over number'),  # regression
    ok('all_object', 'all', '{"a":1}', [True]),
    err('from_entries_non_array_error', 'from_entries', '1', EvaluationError, 'Cannot iterate over number'),
    err('from_entries_non_object_entry_error', 'from_entries', '[1]', EvaluationError, 'Cannot index number with "key"'),  # regression
    ok('from_entries_Key_Value', 'from_entries', '[{"Key":"a","Value":1}]', [{'a': 1}]),  # regression
    ok('from_entries_null_key_skipped', 'from_entries', '[{"key":null,"value":1}]', [{}], differs='shim skips entries with a null key; jq errors'),
    err('to_entries_non_object_error', 'to_entries', '1', EvaluationError, 'number (1) has no keys'),
    err('with_entries_non_object_error', 'with_entries(.)', '[1]', EvaluationError, 'array ([1]) has no keys'),
    err('keys_unsorted_error', 'keys_unsorted', '1', EvaluationError, 'number (1) has no keys'),
    ok('now_type', 'now | type', 'null', ['number']),
    ok('isnormal_values', '[1.5, 0.0, null] | map(isnormal)', 'null', [[True, False, False]]),
    err('add_bools_error', 'add', '[true, true]', EvaluationError, 'boolean (true) and boolean (true) cannot be added'),  # regression
    err('in_error', 'in(1)', '"a"', EvaluationError, 'Cannot check whether number has a string key'),  # regression
    ok('tostring_bool', 'tostring', 'true', ['true']),
    ok('length_float', 'length', '2.5', [2.5]),  # regression
    err('error_null', 'error(null)', '1', EvaluationError, 'null (not a string)'),
    ok('recurse_scalar', '[recurse]', '1', [[1]]),  # regression
    err('walk_generator_unsupported', 'walk(.[])', '[1]', UnsupportedError, 'generators such as .[] are not supported inside the arguments of walk(); collect them with [...] or map() first'),
    ok('min_non_array_shim', 'min', '1', [None], differs='min/max of a non-array is null in the shim; jq errors'),
    ok('contains_nested_mismatch', 'contains({"a":1})', '{"a":"1"}', [False]),
    ok('inside_object', 'inside({"a":1,"b":2})', '{"a":1}', [True]),
    ok('optional_generator_error_shim', '[(.[] | .a)?]', '[{"a":1},"s"]', [[]], differs='? around a generator drops outputs made before the error'),  # new
    ok('recurse_value_arg_shim', '[recurse(.a)] | length', '{"a":{"a":{}}}', [3], differs='recurse(f) drops null children and terminates; jq loops forever'),  # regression
    ok('tonumber_leading_space', 'tonumber', '" 12"', [12]),
    err('tonumber_nan_shim', 'tonumber', '"nan"', EvaluationError, "Cannot parse 'nan' as a number", differs="shim rejects 'nan'; jq 1.7 returns null"),
    ok('abs', 'abs', '-5', [5]),
    ok('index_nan', '[.[nan]]', '[1]', [[None]]),  # regression
    err('floor_nan_shim', 'nan | floor', 'null', EvaluationError, 'cannot convert float NaN to integer', differs='shim raises for floor(nan); jq returns null'),  # regression (crashed)
    ok('sort_by_generator_key', 'sort_by(.[])', '[[2],[1]]', [[[1], [2]]]),
    ok('sort_by_multiple_keys', 'sort_by(.a, .b) | map(.i)', '[{"a":1,"b":2,"i":0},{"a":1,"b":1,"i":1},{"a":0,"b":9,"i":2}]', [[2, 1, 0]]),  # new
    ok('group_by_multiple_keys', 'group_by(.a, .b) | map(length)', '[{"a":1,"b":2},{"a":1,"b":2},{"a":1,"b":1}]', [[1, 2]]),  # new
    ok('any_generator', 'any(.[]; . > 2)', '[[1],[3]]', [True]),
    err('any_generator_in_comparison_unsupported', 'any(.[] > 2)', '[[1],[3]]', UnsupportedError, 'generators such as .[] are not supported inside a comparison expression; collect them with [...] or map() first', differs='generator in a position the shim cannot fan out: compile error'),
    ok('all_generator_arg', 'all(.[]; . > 0)', '[[1],[3]]', [True]),
    ok('quoted_field_postfix_interp', '.f."\\(.k)"', '{"k":"g","f":{"g":1}}', [1]),  # new
    ok('select_values_pipeline', '[.[] | select(.t | length > 0) | {n, first: .t[0]}]', A, [[{'n': 'x', 'first': 'p'}, {'n': 'z', 'first': 'q'}]]),  # new
    ok('hook_pattern_command', '.tool_input.command // empty', '{"tool_input":{"command":"ls"}}', ['ls']),
    ok('hook_pattern_missing', '.tool_input.command // empty', '{"tool_input":{}}', []),
    ok('hook_pattern_decision', '{decision: "block", reason: "blocked \\(.tool_name)"}', '{"tool_name":"Bash"}', [{'decision': 'block', 'reason': 'blocked Bash'}]),
    ok('hook_pattern_file_paths', '[.tool_input.edits[]?.file_path] | unique', '{"tool_input":{"edits":[{"file_path":"b"},{"file_path":"a"},{"file_path":"b"}]}}', [['a', 'b']]),  # new
    ok('hook_pattern_test_regex', '.tool_input.command | test("^(rm|git push)\\b")', '{"tool_input":{"command":"git push origin"}}', [False]),
]


def run_shim(program: str, input_text: str) -> List[Any]:
    """Run a program the way the CLI does and return its outputs."""
    ast = parse_jq_expression(program)
    result = evaluate(ast, json.loads(input_text), dict(VARS))
    if result is EMPTY:
        return []
    return result if is_stream(ast) else [result]


def test_case_table_is_large_and_unique():
    ids = [c.id for c in CASES]
    assert len(ids) == len(set(ids))
    assert len(CASES) >= 150


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_shim(case: Case):
    if case.error is not None:
        with pytest.raises(case.error, match=re.escape(case.match)):
            run_shim(case.program, case.input)
    else:
        actual = run_shim(case.program, case.input)
        # Compare as JSON so 1 / 1.0 / true stay distinct
        assert json.dumps(actual) == json.dumps(case.expected)


def _real_jq_17() -> Optional[str]:
    jq = find_real_jq()
    if jq is None:
        return None
    try:
        version = subprocess.run(
            [jq, "--version"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return jq if version.startswith("jq-1.7") else None


REAL_JQ = _real_jq_17()
COMPARABLE = [c for c in CASES if not c.differs]


@pytest.mark.skipif(REAL_JQ is None, reason="jq 1.7.x not installed")
@pytest.mark.parametrize("case", COMPARABLE, ids=[c.id for c in COMPARABLE])
def test_matches_real_jq(case: Case):
    proc = subprocess.run(
        [REAL_JQ, "-c", "--argjson", "x", json.dumps(VARS["x"]), case.program],
        input=case.input,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if case.error is not None:
        assert proc.returncode != 0, proc.stdout
    else:
        assert proc.returncode == 0, proc.stderr
        outputs = [json.loads(line) for line in proc.stdout.splitlines()]
        assert json.dumps(outputs) == json.dumps(case.expected)
