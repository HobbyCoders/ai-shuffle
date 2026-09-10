"""Unit tests for the blocking-DB scanner (scripts/scan_blocking_db.py).

The scanner is the only thing standing between a careless `db.get_x()` in an
`async def` and a stalled event loop in production, so its detection rules get
their own fixtures rather than being trusted against the live tree.
"""

import textwrap

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
