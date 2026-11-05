"""Unit tests for the blocking-DB scanner (scripts/scan_blocking_db.py).

The scanner is the only thing standing between a careless `db.get_x()` in an
`async def` and a stalled event loop in production, so its detection rules get
their own fixtures rather than being trusted against the live tree.
"""

import textwrap

import pytest

from scripts.scan_blocking_db import scan_source


def _calls(src: str) -> list[str]:
    return [f["call"] for f in scan_source(textwrap.dedent(src), "fixture.py")]


def test_module_alias_import_is_detected():
    """`from app.db import database as db` — the alias, not the module name."""
    assert _calls(
        """
        from app.db import database as db

        async def handler():
            return db.get_session("s1")
        """
    ) == ["db.get_session"]


def test_second_alias_style_is_detected():
    """`from app.db import notifications as notifications_db`."""
    assert _calls(
        """
        from app.db import notifications as notifications_db

        async def handler():
            return notifications_db.list_notifications()
        """
    ) == ["notifications_db.list_notifications"]


def test_bare_function_import_is_detected():
    """`from app.db.error_logs import record_error` — no module alias at all."""
    assert _calls(
        """
        from app.db.error_logs import record_error

        async def handler():
            record_error("boom")
        """
    ) == ["record_error()"]


def test_renamed_bare_function_import_is_detected():
    assert _calls(
        """
        from app.db.checkouts import archive_checkout_by_path as archive

        async def handler():
            archive("/tmp/x")
        """
    ) == ["archive()"]


def test_import_module_as_alias_is_detected():
    assert _calls(
        """
        import app.db.turns as turns_db

        async def handler():
            turns_db.get_turn(1)
        """
    ) == ["turns_db.get_turn"]


def test_async_database_alias_is_not_flagged():
    """adb.* is already the offloaded facade."""
    assert (
        _calls(
            """
            from app.db import async_database as adb

            async def handler():
                return await adb.get_session("s1")
            """
        )
        == []
    )


def test_run_sync_call_site_is_not_flagged():
    assert (
        _calls(
            """
            from app.db import database as db
            from app.db.async_database import run_sync

            async def handler():
                return await run_sync(db.get_session, "s1")
            """
        )
        == []
    )


def test_call_inside_sync_closure_is_not_flagged():
    """The innermost enclosing function wins; run_sync's closure body is sync."""
    assert (
        _calls(
            """
            from app.db import database as db
            from app.db.async_database import run_sync

            async def handler():
                def _work():
                    return db.get_session("s1")

                return await run_sync(_work)
            """
        )
        == []
    )


def test_call_inside_run_sync_lambda_is_not_flagged():
    """A lambda is a sync scope too — `run_sync(lambda: db.f())` is offloaded."""
    assert (
        _calls(
            """
            from app.db import database as db
            from app.db.async_database import run_sync

            async def handler():
                return await run_sync(
                    lambda: db.create_thing(1, 2)
                )
            """
        )
        == []
    )


def test_placeholder_helper_is_not_flagged():
    """app.db.compat.ph returns a string; it is not a query."""
    assert (
        _calls(
            """
            from app.db.compat import ph

            async def handler():
                return f"SELECT 1 WHERE x = {ph()}"
            """
        )
        == []
    )


def test_sync_def_is_not_flagged():
    assert (
        _calls(
            """
            from app.db import database as db

            def handler():
                return db.get_session("s1")
            """
        )
        == []
    )


def test_allowlisted_helpers_are_not_flagged():
    assert (
        _calls(
            """
            from app.db import database as db
            from app.db.mobile_hub import public_device_id

            async def handler():
                public_device_id()
                return db.pool_stats()
            """
        )
        == []
    )


def test_classes_and_constants_are_not_flagged():
    assert (
        _calls(
            """
            from app.db.base import WorktreeStatus
            from app.db.sessions import SessionRow

            async def handler():
                WorktreeStatus("active")
                return SessionRow()
            """
        )
        == []
    )


def test_non_db_imports_are_ignored():
    assert (
        _calls(
            """
            from app.core import database

            async def handler():
                return database.get_session("s1")
            """
        )
        == []
    )


def test_finding_records_line_and_handler():
    findings = scan_source(
        textwrap.dedent(
            """
            from app.db import database as db

            async def my_handler():
                return db.get_session("s1")
            """
        ),
        "fixture.py",
    )
    assert len(findings) == 1
    assert findings[0]["handler"] == "my_handler"
    assert findings[0]["line"] == 5
    assert findings[0]["file"] == "fixture.py"


@pytest.mark.parametrize(("imports", "decoder"), [
    ("from app.db.base import parse_jsonb", "parse_jsonb"),
    ("from app.db.base import parse_jsonb as decode", "decode"),
    ("from app.db import base", "base.parse_jsonb"),
    ("from app.db import base as db", "db.parse_jsonb"),
    ("import app.db.base as db", "db.parse_jsonb"),
    ("import app.db.base", "app.db.base.parse_jsonb"),
])
def test_only_the_known_pure_jsonb_decoder_is_exempt(imports, decoder):
    # An adjacent real query must remain visible, even renamed parse_jsonb.
    # Keeping it in the same fixture also exercises fully-qualified calls.
    source = (f"{imports}\nfrom app.db.turns import get_turn as read_turn\n\n"
              f"async def handler():\n    value = {decoder}('null')\n    read_turn(1)\n")
    assert _calls(source) == ["read_turn()"]


@pytest.mark.parametrize(("imports", "call", "expected"), [
    ("from app.db.turns import get_turn as parse_jsonb", "parse_jsonb(1)", "parse_jsonb()"),
    ("from app.db.turns import parse_jsonb as decode", "decode('null')", "decode()"),
    ("from app.db import turns as db", "db.parse_jsonb('null')", "db.parse_jsonb"),
    ("import app.db.turns as db", "db.parse_jsonb('null')", "db.parse_jsonb"),
    ("from app.db import base as db", "db.get_db()", "db.get_db"),
])
def test_jsonb_exception_does_not_hide_queries_or_other_modules(imports, call, expected):
    source = f"{imports}\n\nasync def handler():\n    {call}\n"
    assert _calls(source) == [expected]


def test_real_query_renamed_parse_jsonb_next_to_the_pure_decoder_still_flags():
    assert _calls("""
        from app.db.base import parse_jsonb as decode
        from app.db.background_tasks import get_stop_order_proofs as parse_jsonb

        async def handler():
            decode('null')
            parse_jsonb('s1', ['task'])
    """) == ["parse_jsonb()"]


def test_ambiguous_module_alias_does_not_gain_the_pure_helper_exception():
    # Import collection is scope-agnostic; do not guess an alias's provenance.
    assert _calls("""
        from app.db import turns as db

        async def handler():
            db.parse_jsonb('null')

        def elsewhere():
            from app.db import base as db
    """) == ["db.parse_jsonb"]


@pytest.mark.parametrize(("raw", "expected"), [
    ('{"epoch": 1}', {"epoch": 1}),
    ('[1, null]', [1, None]),
    ('"text"', "text"),
    ("null", None),
    ("invalid json", None),
    ("", None),
    (None, None),
    ({"epoch": 1}, {"epoch": 1}),
    ([1, None], [1, None]),
    (42, 42),
])
def test_actual_jsonb_decoder_is_pure_and_preserves_passthrough(raw, expected, monkeypatch):
    from app.db import base

    def no_db(*args, **kwargs):
        pytest.fail("the exempt JSON decoder must not access the database")
    monkeypatch.setattr(base, "get_db", no_db)
    decoded = base.parse_jsonb(raw)
    assert decoded == expected
    if not isinstance(raw, str):
        assert decoded is raw


def test_rebound_decoder_reports_the_actual_sync_query_call():
    findings = scan_source(
        "from app.db.base import parse_jsonb\n"
        "from app.db.turns import get_turn\n"
        "parse_jsonb = get_turn\n"
        "async def handler():\n"
        "    parse_jsonb('turn-id')\n",
        "rebinding.py",
    )
    assert findings == [{"file": "rebinding.py", "line": 5,
                         "call": "parse_jsonb()", "handler": "handler"}]


@pytest.mark.parametrize("binding", [
    "decode = get_turn",
    "decode: object = get_turn",
    "decode += get_turn",
    "decode, other = get_turn, None",
    "del decode",
    "for decode in [get_turn]: pass",
    "with manager() as decode: pass",
    "if (decode := get_turn): pass",
    "try: pass\nexcept Exception as decode: pass",
    "match value:\n    case {'query': decode}: pass",
    "match value:\n    case [*decode]: pass",
    "match value:\n    case {**decode}: pass",
    "def decode(): pass",
    "async def decode(): pass",
    "class decode: pass",
    "from app.core.other import decode",
    "import other as decode",
    "from other import *",
])
def test_pure_bare_binding_must_not_have_another_binding_or_deletion(binding):
    source = ("from app.db.base import parse_jsonb as decode\n"
              "from app.db.turns import get_turn\n" + binding +
              "\nasync def handler():\n    decode('turn-id')\n")
    assert _calls(source) == ["decode()"]


@pytest.mark.parametrize("arguments", [
    "decode", "decode=get_turn", "*decode", "**decode", "*, decode", "decode, /",
])
def test_parameter_shadowing_invalidates_pure_bare_binding(arguments):
    source = ("from app.db.base import parse_jsonb as decode\n"
              "from app.db.turns import get_turn\n"
              f"async def handler({arguments}):\n    decode('turn-id')\n")
    assert _calls(source) == ["decode()"]


def test_enclosing_parameter_and_comprehension_shadowing_are_not_pure():
    assert _calls("""
        from app.db.base import parse_jsonb as decode
        from app.db.turns import get_turn
        def factory(decode):
            async def handler():
                decode('turn-id')
    """) == ["decode()"]
    assert _calls("""
        from app.db.base import parse_jsonb as decode
        from app.db.turns import get_turn
        async def handler():
            return [decode('turn-id') for decode in [get_turn]]
    """) == ["decode()"]


@pytest.mark.parametrize("binding", [
    "db = other",
    "del db",
    "db.parse_jsonb = get_turn",
    "del db.parse_jsonb",
    "for db in modules: pass",
    "with manager() as db: pass",
    "if (db := other): pass",
    "from app.core import other as db",
    "import other as db",
    "from app.db import async_database as db",
])
def test_module_alias_uncertainty_invalidates_pure_export(binding):
    source = ("from app.db import base as db\n"
              "from app.db.turns import get_turn\n" + binding +
              "\nasync def handler():\n    db.parse_jsonb('turn-id')\n")
    assert _calls(source) == ["db.parse_jsonb"]


def test_module_alias_parameter_shadowing_is_not_pure():
    assert _calls("""
        import app.db.base as db
        async def handler(db):
            db.parse_jsonb('turn-id')
    """) == ["db.parse_jsonb"]


@pytest.mark.parametrize("binding", [
    "app = other",
    "app.db = other",
    "app.db.base = other",
    "app.db.base.parse_jsonb = get_turn",
    "del app.db.base.parse_jsonb",
    "import other as app",
])
def test_fully_qualified_decoder_requires_unchanged_package_binding(binding):
    source = ("import app.db.base\n" + binding +
              "\nasync def handler():\n    app.db.base.parse_jsonb('turn-id')\n")
    assert _calls(source) == ["app.db.base.parse_jsonb"]


def test_export_mutation_through_an_equivalent_alias_is_not_pure():
    assert _calls("""
        from app.db import base as changed
        import app.db.base as read
        changed.parse_jsonb = query
        async def handler():
            read.parse_jsonb('turn-id')
    """) == ["read.parse_jsonb"]
    assert _calls("""
        from app.db import base as changed
        import app.db.base
        changed.parse_jsonb = query
        async def handler():
            app.db.base.parse_jsonb('turn-id')
    """) == ["app.db.base.parse_jsonb"]


def test_repeated_canonical_imports_without_rebinding_stay_pure():
    assert _calls("""
        from app.db.base import parse_jsonb as decode
        import app.db.base as db
        async def handler():
            from app.db.base import parse_jsonb as decode
            import app.db.base as db
            decode('null')
            db.parse_jsonb('null')
    """) == []
