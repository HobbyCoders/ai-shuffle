"""M5 — projection: tools → compilable typed Python stubs + discovery catalog."""

from fluid.codemode.projection import (
    Projection,
    _annotation,
    _py_ident,
    func_name,
    stub_source,
)
from fluid.tools.registry import ToolSpec


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


def test_docstring_carries_full_description_and_parameters() -> None:
    spec = ToolSpec(
        name="Grep",
        description=(
            "Search file contents.\n"
            "\n"
            "Usage notes:\n"
            "- full regex syntax is supported.\n"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The regex to search for."},
                "output_mode": {
                    "type": "string",
                    "description": "What to print.",
                    "enum": ["content", "files_with_matches", "count"],
                    "default": "files_with_matches",
                },
                "head_limit": {"type": "integer"},
            },
            "required": ["pattern"],
        },
        risk="safe",
    )
    src = stub_source(spec, "Grep")
    compile(src, "<stub>", "exec")

    # every description line survives, not just the first
    assert "Search file contents." in src
    assert "Usage notes:" in src
    assert "- full regex syntax is supported." in src

    # the Parameters block documents every property in signature order
    assert "Parameters:" in src
    assert "pattern (str, required): The regex to search for." in src
    assert "output_mode (str, optional): What to print." in src
    assert "one of: content, files_with_matches, count" in src
    assert "default: 'files_with_matches'" in src
    assert "head_limit (int, optional):" in src
    doc = src.index("Parameters:")
    assert src.index("pattern (str", doc) < src.index("output_mode (str", doc)

    # risk (and the JSON-path note) still come last
    assert src.index("[risk: safe]") > src.index("Parameters:")

    # and the rendered docstring is the description verbatim, not an escaped copy
    ns: dict = {"__host_call__": lambda *a: "ok"}
    exec(compile(src, "<stub>", "exec"), ns)
    assert "Search file contents.\n" in ns["Grep"].__doc__


def test_stub_compiles_with_hostile_description_characters() -> None:
    spec = ToolSpec(
        name="mcp__x__quote",
        description=(
            'Docstring hazard: """ inside the text.\n'
            "\n"
            "A Windows path C:\\Users\\me and a regex \\d+\\s*\n"
            'Ends with a quote: "'
        ),
        input_schema={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "description": "Which mode.",
                    "enum": ["fast", "slow"],
                },
            },
            "required": ["mode"],
        },
    )
    src = stub_source(spec, "quote")
    ns: dict = {"__host_call__": lambda *a: "ok"}
    exec(compile(src, "<stub>", "exec"), ns)  # raises if the escaping is wrong

    doc = ns["quote"].__doc__
    assert '"""' in doc
    assert "C:\\Users\\me" in doc
    assert "\\d+\\s*" in doc
    assert 'Ends with a quote: "' in doc
    assert "mode (str, required): Which mode. (one of: fast, slow)" in doc


def test_deferred_tools_keep_stubs_but_collapse_to_one_liners() -> None:
    """Deferral is a PROMPT-cost decision: the deferred tool is flagged in the
    catalog and its docs are fetched on demand, but the stub is identical and
    the tool stays callable."""
    loaded = _spec("mcp__slack__post_message", risk="write")
    deferred = _spec("mcp__appcfg__list_providers", risk="safe")
    proj = Projection([loaded, deferred], deferred={"mcp__appcfg__list_providers"})

    assert proj.is_deferred("list_providers")
    assert proj.is_deferred("mcp__appcfg__list_providers")  # by full name too
    assert not proj.is_deferred("post_message")
    assert proj.deferred_leaves() == ["list_providers"]
    assert not proj.is_deferred("nonexistent")

    entries = dict(proj.entries())
    assert Projection.DEFERRED_NOTE.strip() in entries["list_providers"]
    assert "read_tool_docs" in entries["list_providers"]
    assert "read_tool_docs" not in entries["post_message"]

    # one_liner is the raw summary a host inlines — no marker, no schema.
    one = proj.one_liner("list_providers")
    assert one.startswith("Post a message to a channel.")
    assert "read_tool_docs" not in one
    assert "(str, required)" not in one  # parameter docs stay out of it
    assert "\n" not in one

    # ...and the full docs are one read_tool_docs away, identical to a loaded
    # tool's, with a compilable stub in the VFS either way.
    docs = proj.docs("list_providers")
    assert docs is not None and "channel (str, required)" in docs
    for leaf, src in proj.sources().items():
        compile(src, f"<{leaf}>", "exec")
    assert "list_providers" in proj.sources()


def test_projection_without_deferred_set_treats_everything_as_loaded() -> None:
    proj = Projection([_spec("mcp__slack__post_message")])
    assert proj.deferred_leaves() == []
    assert not proj.is_deferred("post_message")
    assert "read_tool_docs" not in dict(proj.entries())["post_message"]


def test_sources_are_all_compilable() -> None:
    specs = [_spec("mcp__slack__post_message"), _spec("mcp__x__y")]
    for leaf, src in Projection(specs).sources().items():
        compile(src, f"<{leaf}>", "exec")  # raises on any malformed stub
