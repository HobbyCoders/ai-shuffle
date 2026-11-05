"""Model authority and routing questions share the same positive-clause rule."""
import re

import pytest

from fluid.codemode.preferences import model_routing, routing_priority
from fluid.codemode.routing import affirmed_clauses, is_instruction


@pytest.mark.parametrize("text, positive", [
    ("Use opus for this as it needs to be buyllet proof no gpt for this", {"opus"}),
    ("Use opus not sonnet", {"opus"}),
    ("we use opus no gpt", {"opus"}),
    ("use opus and don't touch the db", {"opus"}),
    ("Use opus for this since it's not simple", {"opus"}),
    ("don't use opus", set()),
    ("opus not working", set()),
    ("use sonnet not opus", {"sonnet"}),
    ("Never use opus, sonnet or fable", set()),
    ("Never use sonnet, use opus", {"opus"}),
    ("use opus instead of sonnet", {"opus"}),
])
def test_positive_instruction_prefix_and_rejected_suffix(text, positive):
    words = set(re.findall(r"\w+", " ".join(affirmed_clauses(text)).lower()))
    assert words & {"opus", "sonnet", "fable", "gpt"} == positive


@pytest.mark.parametrize("text, directed", [
    ("can you use opus?", True),
    ("could you run this on opus?", True),
    ("we use opus no gpt", True),
    ("go with opus", True),
    ("would you use opus for this?", False),
    ("can you explain why we use opus?", False),
    ("the opus run used up the default budget", False),
])
def test_only_a_clause_leading_directive_counts(text, directed):
    assert is_instruction(text) is directed


@pytest.mark.parametrize("prefix", ["use", "we use", "please use"])
def test_routing_question_does_not_lose_the_authorized_model(prefix):
    text = f"{prefix} opus for reviews not sonnet"
    assert model_routing(text) == {"review": frozenset({"opus"})}
    assert routing_priority(text) == 1
