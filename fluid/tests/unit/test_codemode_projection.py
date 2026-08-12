"""M5 — projection: tools → compilable typed Python stubs + discovery catalog."""

from loom.codemode.projection import (
    Projection,
    _annotation,
    _py_ident,
    func_name,
    stub_source,
)
from loom.tools.registry import ToolSpec


def _spec(name: str, *, risk: str = "safe", code_mode_allowed: bool = True) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="Post a message to a channel.",
        input_schema={
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "count": {"type": "integer"},
                "ratio": {"type": "number"},
                "silent": {"type": "boolean"},
                "tags": {"type": "array"},
                "meta": {"type": "object"},
                "thread": {"type": ["string", "null"]},
            },
            "required": ["channel", "count"],
        },
        risk=risk,
        code_mode_allowed=code_mode_allowed,
    )


def test_json_type_to_annotation_mapping() -> None:
    assert _annotation({"type": "string"}) == "str"
    assert _annotation({"type": "integer"}) == "int"
    assert _annotation({"type": "number"}) == "float"
    assert _annotation({"type": "boolean"}) == "bool"
    assert _annotation({"type": "array"}) == "list"
    assert _annotation({"type": "object"}) == "dict"
    # union with null → the non-null member; unknown/absent → a safe fallback
    assert _annotation({"type": ["string", "null"]}) == "str"
    assert _annotation({}) == "object"


def test_stub_compiles_and_builds_args() -> None:
    src = stub_source(_spec("mcp__slack__post_message"), "post_message")
    # It must be syntactically valid Python.
    code = compile(src, "<stub>", "exec")

    calls: list[tuple] = []

    def fake_host_call(name, args):
        calls.append((name, args))
        return "ok"

    ns: dict = {"__host_call__": fake_host_call}
    exec(code, ns)
    fn = ns["post_message"]

    # Required params only: optionals default to None and are omitted from _args.
    assert fn("general", 3) == "ok"
    assert calls[-1] == ("mcp__slack__post_message", {"channel": "general", "count": 3})

    # Supplying an optional threads it through; required stay first in the signature.
    fn(channel="general", count=3, silent=True)
    assert calls[-1][1] == {"channel": "general", "count": 3, "silent": True}


def test_stub_signature_orders_required_first() -> None:
    src = stub_source(_spec("mcp__x__y"), "y")
    header = src.splitlines()[0]
    # required (no default) precede optionals (``| None = None``)
    assert header.startswith("def y(channel: str, count: int, ")
    assert "silent: bool | None = None" in header
    assert "thread: str | None = None" in header  # ["string","null"] optional


def test_identifier_sanitization() -> None:
    # mcp__server__tool → the tool leaf; bare builtins pass through.
    assert func_name("mcp__slack__post_message") == "post_message"
    assert func_name("mcp__x__y") == "y"
    assert func_name("Read") == "Read"
    # non-identifier chars and leading digits are fixed up; keywords get a suffix.
    assert _py_ident("weird-name!") == "weird_name_"
    assert _py_ident("class") == "class_"
    assert _py_ident("3d")[0] == "_"


def test_projection_collision_falls_back_to_full_name() -> None:
    # Two servers expose a same-leaf tool → the second disambiguates to the
    # sanitized full name so both remain importable.
    specs = [_spec("mcp__slack__send"), _spec("mcp__email__send")]
    proj = Projection(specs)
    leaves = set(proj.leaves())
    assert "send" in leaves
    assert "email_send" in leaves or "slack_send" in leaves
    assert len(leaves) == 2


def test_catalog_entries_and_search() -> None:
    specs = [
        _spec("mcp__slack__post_message", risk="write"),
        _spec("mcp__danger__wipe", risk="destructive", code_mode_allowed=False),
    ]
    proj = Projection(specs)

    entries = dict(proj.entries())
    assert "risk: write" in entries["post_message"]
    # code_mode_allowed=False is surfaced in the catalog line.
    assert "JSON-path only" in entries["wipe"]

    hits = [leaf for leaf, _ in proj.search("message")]
    assert "post_message" in hits

    docs = proj.docs("post_message")
    assert docs is not None and docs.startswith("def post_message(")
    # docs are reachable by full tool name too
    assert proj.docs("mcp__slack__post_message") is not None
    assert proj.docs("nonexistent") is None


def test_sources_are_all_compilable() -> None:
    specs = [_spec("mcp__slack__post_message"), _spec("mcp__x__y")]
    for leaf, src in Projection(specs).sources().items():
        compile(src, f"<{leaf}>", "exec")  # raises on any malformed stub
