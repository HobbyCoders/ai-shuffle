"""Shared set-up for the opt-in ``tests/db/*_postgres.py`` suites.

A plain module: a suite opts in with
``from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401``
and ``pytestmark = requires_real_pg``. The imported ``pg_schema`` is a
module-scoped autouse fixture, so it builds the schema once per importing file.
"""
import os

import pytest

requires_real_pg = pytest.mark.skipif(os.environ.get("ALLOW_REAL_PG") != "1",
                                      reason="requires disposable TEST_POSTGRES_DSN")


@pytest.fixture(scope="module", autouse=True)
def pg_schema():
    """Open the pool on TEST_POSTGRES_DSN and create the schema for this module."""
    from app.db import base
    from app.db.schema_postgres import create_postgres_schema
    assert os.environ.get("TEST_POSTGRES_DSN")
    base.init_pool(min_conn=1, max_conn=6)
    try:
        with base.get_db() as conn:
            create_postgres_schema(conn.cursor())
        yield
    finally:
        base.close_pool()
