"""Projection — ToolRegistry specs → typed Python stubs for the kernel VFS.

Progressive disclosure, code-mode flavor: instead of N tool schemas in the
prompt, the model gets four tools and a virtual ``tools/`` package. It calls
``list_tools`` / ``search_tools`` to find a tool, ``read_tool_docs`` to see the
generated stub (signature + docstring), then ``from tools.<name> import <name>``
inside ``run_python``. Importing/calling a stub routes to ``__host_call__``.

JSON Schema → signature mapping (per the plan): ``string→str``, ``integer→int``,
``number→float``, ``boolean→bool``, ``array→list``, ``object→dict``; required
params first, optionals default ``None``; docstring = description + ``[risk: X]``.
Tools with ``code_mode_allowed=False`` are still *listed* and get a stub, but the
stub is annotated "JSON-path only" and the host gate rejects the call (hard,
host-side) — so the escape hatch cannot be bypassed from inside the kernel.
"""

from __future__ import annotations

import keyword
from typing import Iterable

from loom.tools.registry import ToolSpec

_TYPE_MAP = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
}


def func_name(tool_name: str) -> str:
    """Last segment of ``mcp__server__tool`` (or the bare builtin name)."""
    return tool_name.split("__")[-1]


def _sanitize_full(tool_name: str) -> str:
    base = tool_name
    if base.startswith("mcp__"):
        base = base[len("mcp__"):]
    return base.replace("__", "_")


def _py_ident(name: str) -> str:
    out = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in name)
    if not out or out[0].isdigit():
        out = "_" + out
    if keyword.iskeyword(out):
        out += "_"
    return out


def _annotation(schema: dict) -> str:
    t = schema.get("type")
    if isinstance(t, list):  # e.g. ["string", "null"]
        t = next((x for x in t if x != "null"), None)
    return _TYPE_MAP.get(t, "object")


def _first_line(text: str) -> str:
    for line in (text or "").strip().splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def stub_source(spec: ToolSpec, func: str) -> str:
    """Generate the typed stub module source for one tool."""
    schema = spec.input_schema or {}
    props: dict[str, dict] = schema.get("properties", {}) or {}
    required = list(schema.get("required", []) or [])

    ordered = [p for p in props if p in required] + [p for p in props if p not in required]
    params: list[str] = []
    py_by_json: dict[str, str] = {}
    for jname in ordered:
        pyname = _py_ident(jname)
        py_by_json[jname] = pyname
        ann = _annotation(props.get(jname, {}))
        if jname in required:
            params.append(f"{pyname}: {ann}")
        else:
            params.append(f"{pyname}: {ann} | None = None")
    sig = ", ".join(params)

    doc_lines = [_first_line(spec.description) or func]
    doc_lines.append("")
    doc_lines.append(f"[risk: {spec.risk}]")
    if not spec.code_mode_allowed:
        doc_lines.append("JSON-path only — not callable from code-mode; the host gate rejects it.")
    docstring = "\n    ".join(doc_lines)

    body: list[str] = [f'def {func}({sig}) -> str:', f'    """{docstring}\n    """', "    _args: dict = {}"]
    for jname in ordered:
        pyname = py_by_json[jname]
        if jname in required:
            body.append(f'    _args[{jname!r}] = {pyname}')
        else:
            body.append(f"    if {pyname} is not None:")
            body.append(f'        _args[{jname!r}] = {pyname}')
    body.append(f'    return __host_call__({spec.name!r}, _args)')
    return "\n".join(body) + "\n"


class Projection:
    """A snapshot of the registry's work-tools, projected to VFS stubs."""

    def __init__(self, specs: Iterable[ToolSpec]) -> None:
        self._by_leaf: dict[str, ToolSpec] = {}
        for spec in specs:
            leaf = _py_ident(func_name(spec.name))
            if leaf in self._by_leaf and self._by_leaf[leaf].name != spec.name:
                leaf = _py_ident(_sanitize_full(spec.name))
            self._by_leaf[leaf] = spec

    def leaves(self) -> list[str]:
        return sorted(self._by_leaf)

    def spec_for(self, name: str) -> ToolSpec | None:
        if name in self._by_leaf:
            return self._by_leaf[name]
        for leaf, spec in self._by_leaf.items():  # allow lookup by full tool name
            if spec.name == name:
                return spec
        return None

    def leaf_for(self, name: str) -> str | None:
        if name in self._by_leaf:
            return name
        for leaf, spec in self._by_leaf.items():
            if spec.name == name:
                return leaf
        return None

    def sources(self) -> dict[str, str]:
        """``{leaf: stub_source}`` for the kernel VFS."""
        return {leaf: stub_source(spec, leaf) for leaf, spec in self._by_leaf.items()}

    def entries(self) -> list[tuple[str, str]]:
        """``[(leaf, one_liner)]`` sorted by leaf — for ``list_tools``."""
        out = []
        for leaf in sorted(self._by_leaf):
            spec = self._by_leaf[leaf]
            note = "" if spec.code_mode_allowed else "  [JSON-path only]"
            out.append((leaf, f"{_first_line(spec.description)} [risk: {spec.risk}]{note}"))
        return out

    def search(self, query: str) -> list[tuple[str, str]]:
        tokens = [t for t in query.lower().split() if t]
        scored: list[tuple[int, str, str]] = []
        for leaf, one in self.entries():
            hay = f"{leaf} {self._by_leaf[leaf].name} {one}".lower()
            score = sum(1 for t in tokens if t in hay)
            if not tokens and query.lower() in hay:
                score = 1
            if score:
                scored.append((score, leaf, one))
        scored.sort(key=lambda s: (-s[0], s[1]))
        return [(leaf, one) for _, leaf, one in scored]

    def docs(self, name: str) -> str | None:
        leaf = self.leaf_for(name)
        if leaf is None:
            return None
        return stub_source(self._by_leaf[leaf], leaf)
