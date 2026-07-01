"""Regression tests: the (potentially uncached) seed transcript parse
``message_resolver.resolve_for_streaming_init`` must be offloaded to a thread
via ``run_sync`` at every async call site, never called directly on the event
loop.

In postgres_only mode ``resolve_for_streaming_init`` can trigger a full
synchronous Postgres store read (SELECT + fetchall + transform), which — if
invoked directly inside an ``async def`` — blocks the event loop on every turn
start. These tests assert the offload wiring stays in place.

We assert on the parsed source of the two async call sites rather than driving
the whole handler: the concrete regression is "a direct sync call snuck back
in", which an AST check catches precisely without mock-theater.
"""

import ast
import inspect
import textwrap

import app.api.chat_ws_handler as chat_ws_handler
import app.core.query_background as query_background


def _resolve_call_is_offloaded(source: str) -> bool:
    """Return True iff every ``resolve_for_streaming_init`` call in ``source``
    is the *function argument* of a ``run_sync(...)`` call (offloaded), and no
    call invokes ``resolve_for_streaming_init(...)`` directly."""
    tree = ast.parse(textwrap.dedent(source))

    def _attr_name(node: ast.AST) -> str | None:
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Name):
            return node.id
        return None

    saw_offloaded = False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _attr_name(node.func)

        # Direct call: resolve_for_streaming_init(...) — the regression.
        if callee == "resolve_for_streaming_init":
            return False

        # Offloaded: run_sync(<...>.resolve_for_streaming_init, ...)
        if callee == "run_sync" and node.args:
            first = node.args[0]
            if _attr_name(first) == "resolve_for_streaming_init":
                saw_offloaded = True

    return saw_offloaded


def test_chat_ws_run_query_offloads_seed_parse():
    src = inspect.getsource(chat_ws_handler.ChatWebSocketHandler.run_query)
    assert _resolve_call_is_offloaded(src), (
        "chat_ws_handler.run_query must offload resolve_for_streaming_init via "
        "run_sync; a direct sync call blocks the event loop in postgres_only mode"
    )


def test_start_background_query_offloads_seed_parse():
    src = inspect.getsource(query_background.start_background_query)
    assert _resolve_call_is_offloaded(src), (
        "query_background.start_background_query must offload "
        "resolve_for_streaming_init via run_sync; a direct sync call blocks the "
        "event loop in postgres_only mode"
    )
