"""The counter observes real router -> run_sync -> shim SQL and streamed bytes."""
import asyncio
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from tests.performance.metrics import RequestMeasurements, count_statements, measure_sql


def test_existing_trace_counter_is_shared():
    connection = sqlite3.connect(":memory:")
    with count_statements(connection) as statements:
        connection.execute("CREATE TABLE example (id INTEGER)")
        connection.execute("INSERT INTO example VALUES (1)")
        connection.execute("SELECT * FROM example").fetchall()
    connection.execute("SELECT 2")
    assert len(statements) == 2
    connection.close()


def test_real_request_counts_threaded_sql_and_stream_bytes(mock_db):
    from app.db import base
    from app.db.async_database import run_sync

    app = FastAPI()
    mock_db.execute("CREATE TABLE perf_rows (value INTEGER)")

    def read():
        with base.get_db() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 AS value")
                assert cursor.fetchone()["value"] == 1
                cursor.executemany("INSERT INTO perf_rows VALUES (%s)", ((i,) for i in range(3)))

    @app.get("/measured")
    async def measured():
        await run_sync(read)

        async def chunks():
            yield "\u00e9".encode()
            yield b"event: caught_up\n\n"
        return StreamingResponse(chunks(), media_type="text/event-stream")

    wrapper = RequestMeasurements(app)
    with measure_sql(), TestClient(wrapper) as client:
        read()  # outside a request, not counted
        response = client.get("/measured")
        second = client.get("/measured")
    assert response.status_code == second.status_code == 200
    assert len(wrapper.requests) == 2
    for cost in wrapper.requests:
        assert cost.sql_count == 4
        assert cost.response_bytes == len(response.content) == 20
        assert cost.complete and not cost.active


def test_concurrent_requests_and_failure_cleanup():
    class Cursor:
        def execute(self, sql):
            if sql == "BROKEN":
                raise ValueError("failed query")
            return self

    class Connection:
        def cursor(self):
            return Cursor()

    async def scenario():
        one_started, two_started = asyncio.Event(), asyncio.Event()

        async def app(scope, receive, send):
            if scope["path"] == "/one":
                Connection().cursor().execute("SELECT one")
                one_started.set()
                await two_started.wait()
                Connection().cursor().execute("SELECT two")
                body = b"one"
            else:
                await one_started.wait()
                with pytest.raises(ValueError):
                    Connection().cursor().execute("BROKEN")
                two_started.set()
                body = b"two-two"
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": body})

        wrapper = RequestMeasurements(app)

        async def send(message):
            pass

        with measure_sql((Connection,)):
            await asyncio.gather(wrapper({"type": "http", "path": "/one"}, None, send),
                                 wrapper({"type": "http", "path": "/two"}, None, send))
            Connection().cursor().execute("SELECT outside")
        assert [cost.sql_count for cost in wrapper.requests] == [2, 1]
        assert [cost.response_bytes for cost in wrapper.requests] == [3, 7]
        assert Connection.cursor.__qualname__ == "test_concurrent_requests_and_failure_cleanup.<locals>.Connection.cursor"

    asyncio.run(scenario())


def test_final_send_failure_is_not_a_completed_response():
    async def scenario():
        async def app(scope, receive, send):
            await send({"type": "http.response.body", "body": b"last"})

        async def send(message):
            raise RuntimeError("disconnected")

        wrapper = RequestMeasurements(app)
        with pytest.raises(RuntimeError, match="disconnected"):
            await wrapper({"type": "http"}, None, send)
        assert wrapper.requests[0].response_bytes == 4
        assert not wrapper.requests[0].complete
        assert not wrapper.requests[0].active

    asyncio.run(scenario())


def test_disconnect_keeps_partial_count_and_non_http_bypasses():
    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        raise RuntimeError("disconnect")

    async def scenario():
        async def send(message):
            pass
        wrapper = RequestMeasurements(app)
        await wrapper({"type": "websocket"}, None, send)
        with pytest.raises(RuntimeError, match="disconnect"):
            await wrapper({"type": "http"}, None, send)
        assert len(wrapper.requests) == 1
        cost = wrapper.requests[0]
        assert cost.response_bytes == 7 and not cost.complete and not cost.active

    asyncio.run(scenario())
