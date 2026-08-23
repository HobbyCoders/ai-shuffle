"""Projection — ToolRegistry specs → typed Python stubs for the kernel VFS.

Progressive disclosure, code-mode flavor: instead of N tool schemas in the
prompt, the model gets four tools and a virtual ``tools/`` package. It calls
``list_tools`` / ``search_tools`` to find a tool, ``read_tool_docs`` to see the
generated stub (signature + docstring), then ``from tools import <name>`` inside
``run_python`` (``tools.<name>`` as a module path still resolves to the stub, so
``from tools.<name> import <name>`` keeps working). Calling a stub routes to
``__host_call__``.

JSON Schema → signature mapping (per the plan): ``string→str``, ``integer→int``,
``number→float``, ``boolean→bool``, ``array→list``, ``object→dict``; required
params first, optionals default ``None``; docstring = the full description + a
``Parameters:`` block + ``[risk: X]``.
Tools with ``code_mode_allowed=False`` are still *listed* and get a stub, but the
stub is annotated "JSON-path only" and the host gate rejects the call (hard,
host-side) — so the escape hatch cannot be bypassed from inside the kernel.
"""

from __future__ import annotations

import keyword
from collections.abc import Callable, Iterable

from fluid.tools.registry import ToolSpec

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
    base = base.removeprefix("mcp__")
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


def _param_doc(pyname: str, prop: dict, *, required: bool) -> str:
    """One ``Parameters:`` entry — ``name (type, required|optional): desc``."""
    head = f"{pyname} ({_annotation(prop)}, {'required' if required else 'optional'})"
    desc = str(prop.get("description") or "").strip()
    extras: list[str] = []
    enum = prop.get("enum")
    if isinstance(enum, (list, tuple)) and enum:
        extras.append("one of: " + ", ".join(str(v) for v in enum))
    if "default" in prop:
        extras.append(f"default: {prop['default']!r}")
    if extras:
        joined = "; ".join(extras)
        desc = f"{desc} ({joined})" if desc else joined
    return f"{head}: {desc}" if desc else f"{head}:"


def _docstring_block(lines: list[str]) -> str:
    """Render doc lines as a compilable, correctly indented docstring literal.

    The tool description is reproduced verbatim, so it may contain anything —
    backslashes, quotes, even a literal ``\"\"\"``. Backslashes are doubled and
    triple quotes escaped so the generated module still parses; both render
    back to the original text when the model reads ``__doc__``.
    """
    first, *rest = lines or [""]
    body = "\n".join([first] + [f"    {line}" if line else "" for line in rest])
    body = body.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    if body.endswith('"'):
        body = body[:-1] + '\\"'
    return f'    """{body}\n    """'


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

    # The docstring carries the tool's FULL description (the model has no other
    # schema for it in code-mode) plus a Parameters block, so a stub read via
    # read_tool_docs is as informative as the JSON schema would have been.
    description = (spec.description or "").strip()
    doc_lines: list[str] = description.splitlines() if description else [func]
    if ordered:
        doc_lines.append("")
        doc_lines.append("Parameters:")
        for jname in ordered:
            doc_lines.append("  " + _param_doc(
                py_by_json[jname], props.get(jname, {}) or {}, required=jname in required,
            ))
    doc_lines.append("")
    doc_lines.append(f"[risk: {spec.risk}]")
    if not spec.code_mode_allowed:
        doc_lines.append("JSON-path only — not callable from code-mode; the host gate rejects it.")

    body: list[str] = [f'def {func}({sig}) -> str:', _docstring_block(doc_lines), "    _args: dict = {}"]
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
    """A snapshot of the registry's work-tools, projected to VFS stubs.

    ``tool_filter`` is the session's tool-surface predicate (ruling T9): a spec
    it rejects is dropped here, before any leaf name exists for it, so nothing
    downstream — stubs, catalog, search, docs, or a host rendering per-tool
    documentation sections — can leak a tool the session cannot call.
    """

    def __init__(
        self,
        specs: Iterable[ToolSpec],
        *,
        tool_filter: Callable[[str], bool] | None = None,
    ) -> None:
        self._by_leaf: dict[str, ToolSpec] = {}
        for spec in specs:
            if tool_filter is not None and not tool_filter(spec.name):
                continue
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
