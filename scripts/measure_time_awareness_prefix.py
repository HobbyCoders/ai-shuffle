"""Reproduce owner/code prefix sizes by section/tool on both host harness legs.

PYTHONPATH=fluid python scripts/measure_time_awareness_prefix.py --baseline 10b9ea081
No provider, database, credential or live session. Uses the shipped catalog fixture,
production compiler and environment renderer. The CLI's proprietary preset is NOT
measured. Deferred schemas remain deferred. Tokens are ceil(chars/4), not billing.
The optional baseline loads only the changed prompt producers from the specified
local git revision, in memory; it never switches branches or touches a worktree.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import importlib
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "fluid")]
from scripts.measure_prompt_catalog import compile_fixture


@contextmanager
def producers(ref=None):
    with ExitStack() as stack:
        if ref:
            for name in ("app.mcp.working_record_tools", "app.mcp.builtin_tools", "fluid.context.environment"):
                old = importlib.import_module(name)
                relative = ("fluid/" if name.startswith("fluid.") else "") + name.replace(".", "/") + ".py"
                source = subprocess.check_output(["git", "show", f"{ref}:{relative}"], cwd=ROOT, text=True)
                mod = ModuleType(name)
                mod.__file__ = old.__file__
                stack.enter_context(patch.dict(sys.modules, {name: mod}))
                parent, child = name.rsplit(".", 1)
                stack.enter_context(patch.object(importlib.import_module(parent), child, mod))
                exec(compile(source, mod.__file__, "exec"), mod.__dict__)
        yield


def render(ref=None):
    with producers(ref):
        from fluid.context.environment import environment_block
        # Stable fixture inputs, not a frozen environment paragraph that could
        # accidentally omit a newly shipped behaviour rule.
        environment = environment_block("/fixture", include_volatile=False)
        result = {}
        for native in (True, False):
            compiled, full, specs = compile_fixture(native=native, environment_text=environment)
            sections = ({f"{i}:{s.kind}": s.text for i, s in enumerate(compiled.sections)} if native
                        else {"host_append": compiled.text})
            tools = {name: text for text, name in full}
            def sizes(mapping):
                return {key: {"chars": len(value), "tokens_chars_div_4": (len(value) + 3) // 4}
                        for key, value in mapping.items()}
            result["native_code" if native else "delegated_host"] = {
                "compiled_eager": {"chars": len(compiled.text), "tokens_chars_div_4": (len(compiled.text) + 3) // 4},
                "sections": sizes(sections), "tools_full_catalog_not_all_eager": sizes(tools),
                "tool_count": len(specs)}
        return result


def duplicate_paragraphs(text):
    from collections import Counter
    paragraphs = [" ".join(p.split()) for p in text.split("\n\n") if len(p.strip()) >= 100]
    return [p for p, count in Counter(paragraphs).items() if count > 1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline")
    args = parser.parse_args()
    report = {"current": render()}
    if args.baseline:
        report["baseline"] = render(args.baseline)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
