"""Pin the exact result examples consumed by the later choice-card UI."""
import json
from pathlib import Path
import re
from dataclasses import replace

from app.core.generation_resolver import Resolver
from tests.core.test_generation_resolver import candidate, FakeBackend, INTERACTIVE, PRICE

ROOT = Path(__file__).resolve().parents[2]


async def test_documented_contracts_are_exact_and_mirrored():
    path = Path("content/api-reference/generation-tools.md")
    document = (ROOT / "docs" / path).read_text(encoding="utf-8")
    assert document == (ROOT / "frontend/src/lib/docs" / path).read_text(encoding="utf-8")
    choose, confirmation = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", document, re.S)]
    a, b = candidate(), candidate(account="account-b", model="model-b")
    output = await Resolver(FakeBackend([a, b])).generate("image", {"prompt": "make it"}, replace(INTERACTIVE, last_choice=b.choice()))
    assert output == choose
    expensive = replace(a, price={**PRICE, "amount": "0.25", "total": "0.25"})
    output = await Resolver(FakeBackend([expensive])).generate("image", {"prompt": "make it", "source": a.account}, INTERACTIVE)
    output["confirmation_token"] = "<receipt>"
    output["retry"]["confirmation_token"] = "<receipt>"
    assert output == confirmation
