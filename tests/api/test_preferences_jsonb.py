"""Preference HTTP round-trips with PostgreSQL's JSONB driver semantics."""

import json
import re
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import preferences
from app.db import settings
from tests.pg_shim import ShimConnection, ShimCursor


@pytest.fixture
def jsonb_client(mock_db, monkeypatch):
    # The shared SQLite shim returns raw JSON text. Psycopg decodes JSONB
    # before the app sees it, including JSON strings such as Fast mode's "1".
    class JsonbCursor(ShimCursor):
        def execute(self, sql, params=None):
            self.value_is_text = bool(
                re.search(r"CAST\(value AS TEXT\)", sql, re.IGNORECASE)
            )
            return super().execute(sql, params)

        def decode(self, row):
            if row and "value" in row and not self.value_is_text:
                row["value"] = json.loads(row["value"])
            return row

        def fetchone(self):
            return self.decode(super().fetchone())

        def fetchall(self):
            return [self.decode(row) for row in super().fetchall()]

    class JsonbConnection(ShimConnection):
        def cursor(self):
            return JsonbCursor(self._conn.cursor(), serialize=True)

    @contextmanager
    def get_db():
        conn = JsonbConnection(mock_db)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    monkeypatch.setattr(settings, "get_db", get_db)
    app = FastAPI()
    app.include_router(preferences.router)
    app.dependency_overrides[preferences.require_auth] = lambda: "authenticated"
    with (
        patch.object(
            preferences, "get_user_identity", return_value=("account", "fast-mode-test")
        ),
        TestClient(app) as client,
    ):
        yield client


@pytest.mark.parametrize(
    "value",
    [
        "1",
        "0",
        "true",
        "false",
        "null",
        '"quoted"',
        "[1]",
        '{"a":1}',
        "",
        "dark",
        1,
        0,
        True,
        False,
        None,
        [1],
        {"fastMode": True},
    ],
)
def test_preferences_preserve_json_value_types(jsonb_client, value):
    key = "composer.fastMode"
    url = f"/api/v1/preferences/{key}"

    def assert_value(response):
        assert response.status_code == 200
        actual = response.json()["value"]
        assert actual == value
        assert type(actual) is type(value)

    # Creation, migration-conflict readback, optimistic update, and beacon POST.
    saved = jsonb_client.put(
        url, json={"key": key, "value": value, "expected_version": None}
    )
    assert_value(saved)
    assert_value(
        jsonb_client.put(
            url + "?only_if_missing=true", json={"key": key, "value": "ignored"}
        )
    )
    assert_value(
        jsonb_client.put(
            url,
            json={
                "key": key,
                "value": value,
                "expected_version": saved.json()["version"],
            },
        )
    )
    assert_value(jsonb_client.post(url, json={"key": key, "value": value}))
    assert_value(jsonb_client.get(url))
    all_values = jsonb_client.get("/api/v1/preferences/all").json()["preferences"]
    assert all_values[key] == value
    assert type(all_values[key]) is type(value)
