"""Keep the linear predicate equivalent to the original, on tractable inputs."""
import re
from random import Random
from time import perf_counter

import pytest

from app.db import session_questions as questions

_OLD_LIST = re.compile(r"(?:\r?\n[ \t]*(?:[-*+]|\d+[.)])[ \t]+[^\n]*)+[ \t\r\n]*$")
_OLD_SPACE = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_OLD_TRIM = re.compile(f"^[{_OLD_SPACE}]+|[{_OLD_SPACE}]+$")


def old_asks_question(content):
    prose = re.sub(r"`[^`]*`", "", content)
    prose = re.sub(r"\]\([^\)" + _OLD_SPACE + r"]*\)", "]", prose)
    prose = re.sub(r"<https?://[^>" + _OLD_SPACE + r"]*>", "", prose)
    prose = re.sub(r"(?<![a-zA-Z0-9_])https?://[^" + _OLD_SPACE + r"]*[^" + _OLD_SPACE + r".,;:!?)\]'\"]", "", prose)
    return re.search(r"\?[\"'’”)*_\]" + _OLD_SPACE + r"]*$", _OLD_LIST.sub("", prose)) is not None


def test_small_inputs_match_old_regexes():
    random = Random(678)
    for _ in range(1000):
        prefix = random.choice(["Which?", "Done.", "", "- first?", "Which?\r", "Which?\n"])
        lines = [
            random.choice(["", " ", "\t", " \t"])
            + random.choice(["-", "*", "+", "1.", "12)", "١.", "no", "1x", ""])
            + random.choice(["", " ", "  ", "\t", " \t ", "   ", "    "])
            + random.choice(["", "item", "item?", "\r", "`?`", "[label?](target)"])
            for _ in range(random.randrange(9))
        ]
        content = random.choice(["\n", "\r\n"]).join([prefix, *lines])
        content += random.choice(["", "\n", "\n \t\r\n", "\nclosing prose.", "\nclosing?", "\v", "\u00a0", "\r"])
        assert questions._strip_trailing_list(content) == _OLD_LIST.sub("", content)
        assert questions.asks_question(content) == old_asks_question(content)
        assert content.strip(questions._JS_SPACE) == _OLD_TRIM.sub("", content)


def test_small_code_and_url_inputs_match_old_regexes():
    random = Random(679)
    tokens = ["`", "](", ")", "<https://", ">", "https://", "http://", "x", "?", "!", ".", "'", '"', "/", " ", "\t", "\n", "\ufeff", "\x1c", "é"]
    for _ in range(2000):
        content = "".join(random.choices(tokens, k=random.randrange(25)))
        assert questions.asks_question(content) == old_asks_question(content), repr(content)


@pytest.mark.timeout(1)
def test_issue_678_lists_complete_well_under_one_second():
    start = perf_counter()
    for gap in (2, 3, 4):
        for count in range(22, 201):
            for marker in ("1.", "-"):
                content = "Which?\n" + (marker + " " * gap + "item\n") * count
                assert not questions.asks_question(content + "closing prose.")
                assert questions.asks_question(content)
    assert perf_counter() - start < 0.5


@pytest.mark.timeout(1)
def test_other_adversarial_inputs_complete_well_under_one_second():
    start = perf_counter()
    for content in (
        "](" * 20000 + "?",
        "<https://" * 20000 + "?",
        "https://" + "?" * 200000,
        "`" * 200000 + "?",
        "Which?\n" + "9" * 200000 + "x item?",
        "Which?\n" + " " * 200000 + "not a list?",
    ):
        assert questions.asks_question(content)
    assert perf_counter() - start < 0.5


@pytest.mark.parametrize(("content", "expected"), [
    ("Which?\r\n\t12)\titem\r\n+ \r\n", True),
    ("Which?\n-\t\n+  ", True),
    ("Which?\n- item\n\u00a0", False),
])
def test_raw_predicate_preserves_untrimmed_suffixes(content, expected):
    assert questions.asks_question(content) is expected
    assert questions.asks_question(content) == old_asks_question(content)


def test_no_bounded_tail_can_preserve_long_lists_and_code_spans():
    assert questions.asks_question("Which?\n" + "- item\n" * 2000)
    assert not questions.asks_question("`" + "code\n" * 2000 + "?`")


@pytest.mark.parametrize("char", [chr(code) for code in range(0x2000, 0x200B)] + ["\ufeff", "-", "\x1c"])
def test_trim_keeps_javascript_whitespace_semantics(char):
    content = char + "Which?" + char
    assert content.strip(questions._JS_SPACE) == _OLD_TRIM.sub("", content)
