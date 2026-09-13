"""Transcript payload shaping: pagination, tool-input caps, screenshot URLs.

Covers finding B1 (backend performance review 2026-08): session open returned
the entire transcript, with untruncated ``toolInput`` (a Write call carries the
whole file) and inline base64 screenshot data URIs.
"""

import base64
import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import sessions_helpers
from app.api.sessions import router
from app.api.auth import require_auth


# =============================================================================
# Unit: keyset pagination
# =============================================================================

def _msgs(n):
    return [{"id": i, "role": "user", "content": f"m{i}"} for i in range(n)]


class TestPaginateMessages:
    def test_default_returns_everything_for_an_ordinary_transcript(self):
        messages = _msgs(50)
        page, meta = sessions_helpers.paginate_messages(messages)
        assert page == messages
        assert meta["total"] == 50
        assert meta["returned"] == 50
        assert meta["has_more"] is False
        assert meta["next_before_seq"] is None

    def test_newest_window_first(self):
        page, meta = sessions_helpers.paginate_messages(_msgs(10), limit=3)
        assert [m["content"] for m in page] == ["m7", "m8", "m9"]
        assert meta["first_seq"] == 7
        assert meta["last_seq"] == 9
        assert meta["has_more"] is True
        assert meta["next_before_seq"] == 7

    def test_cursor_walks_backwards_without_gaps_or_repeats(self):
        messages = _msgs(10)
        seen = []
        cursor = None
        for _ in range(4):
            page, meta = sessions_helpers.paginate_messages(
                messages, limit=3, before_seq=cursor
            )
            seen = [m["content"] for m in page] + seen
            cursor = meta["next_before_seq"]
            if not meta["has_more"]:
                break
        assert seen == [f"m{i}" for i in range(10)]
        assert cursor is None

    def test_oldest_page_reports_no_more(self):
        page, meta = sessions_helpers.paginate_messages(_msgs(10), limit=4, before_seq=4)
        assert [m["content"] for m in page] == ["m0", "m1", "m2", "m3"]
        assert meta["has_more"] is False
        assert meta["has_more_after"] is True

    def test_out_of_range_cursor_is_clamped(self):
        page, meta = sessions_helpers.paginate_messages(_msgs(5), limit=10, before_seq=999)
        assert len(page) == 5
        assert meta["has_more"] is False

    def test_empty_transcript(self):
        page, meta = sessions_helpers.paginate_messages([], limit=10)
        assert page == []
        assert meta == {
            "total": 0, "returned": 0, "limit": 10,
            "first_seq": None, "last_seq": None,
            "has_more": False, "next_before_seq": None, "has_more_after": False,
        }

    def test_limit_is_clamped_to_the_maximum(self):
        _, meta = sessions_helpers.paginate_messages(_msgs(3), limit=10 ** 9)
        assert meta["limit"] == sessions_helpers.MAX_MESSAGE_PAGE_LIMIT

    def test_unset_query_default_means_default_limit(self):
        """The endpoint functions are also called directly (tests, internals)."""
        from fastapi import Query

        assert sessions_helpers.resolve_message_page_limit(Query(None)) == \
            sessions_helpers.DEFAULT_MESSAGE_PAGE_LIMIT


# =============================================================================
# Unit: tool-input capping
# =============================================================================

class TestToolInputCap:
    def test_long_string_field_is_truncated_with_a_marker(self):
        big = "x" * 50_000
        capped, truncated = sessions_helpers.cap_tool_input(
            {"file_path": "/a.py", "content": big}
        )
        assert truncated is True
        assert capped["file_path"] == "/a.py"  # short fields untouched
        assert len(capped["content"]) < 3000
        assert capped["content"].startswith("x" * 100)
        assert "truncated" in capped["content"]

    def test_short_input_is_returned_unchanged_and_unwrapped(self):
        original = {"pattern": "TODO"}
        capped, truncated = sessions_helpers.cap_tool_input(original)
        assert truncated is False
        assert capped is original

    def test_nested_edits_are_capped(self):
        original = {"edits": [{"old_string": "y" * 9000, "new_string": "ok"}]}
        capped, truncated = sessions_helpers.cap_tool_input(original)
        assert truncated is True
        assert len(capped["edits"][0]["old_string"]) < 3000
        assert capped["edits"][0]["new_string"] == "ok"
        # The original payload is never mutated.
        assert len(original["edits"][0]["old_string"]) == 9000

    def test_compact_marks_the_message_and_rewrites_both_key_spellings(self):
        payload = {"content": "z" * 20_000}
        message = {
            "role": "assistant",
            "type": "tool_use",
            "toolInput": payload,
            "tool_input": payload,
        }
        sessions_helpers.compact_messages_for_display([message])

        assert message["metadata"]["toolInputTruncated"] is True
        assert message["metadata"]["tool_input_truncated"] is True
        assert message["toolInput"] is message["tool_input"]
        assert len(message["toolInput"]["content"]) < 3000

    def test_compact_is_idempotent(self):
        """A second pass must not stack a second truncation marker."""
        message = {"toolInput": {"content": "z" * 20_000}}
        sessions_helpers.compact_messages_for_display([message])
        once = message["toolInput"]["content"]
        sessions_helpers.compact_messages_for_display([message])
        assert message["toolInput"]["content"] == once

    def test_existing_metadata_is_preserved(self):
        message = {
            "toolInput": {"content": "z" * 20_000},
            "metadata": {"timestamp": "2026-08-28T00:00:00Z"},
        }
        sessions_helpers.compact_messages_for_display([message])
        assert message["metadata"]["timestamp"] == "2026-08-28T00:00:00Z"
        assert message["metadata"]["toolInputTruncated"] is True

    def test_message_without_a_tool_input_is_untouched(self):
        message = {"role": "user", "content": "hi"}
        sessions_helpers.compact_messages_for_display([message])
        assert message == {"role": "user", "content": "hi"}


# =============================================================================
# Unit: screenshots served by artifact URL
# =============================================================================

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-screenshot-bytes"
PNG_DIGEST = hashlib.sha256(PNG_BYTES).hexdigest()
PNG_DATA_URI = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()


class TestScreenshotArtifactUrl:
    def test_stored_screenshot_becomes_an_artifact_url(self):
        message = {"type": "tool_use", "toolScreenshot": PNG_DATA_URI}
        with patch("app.core.artifact_store.artifact_path",
                   side_effect=lambda h: Path("/tmp") / h if h == PNG_DIGEST else None):
            sessions_helpers.compact_messages_for_display([message])
        assert message["toolScreenshot"] == f"/api/v1/artifacts/{PNG_DIGEST}"

    def test_unstored_screenshot_keeps_its_inline_data_uri(self):
        """Never drop pixels we cannot serve another way."""
        message = {"type": "tool_use", "toolScreenshot": PNG_DATA_URI}
        with patch("app.core.artifact_store.artifact_path", return_value=None):
            sessions_helpers.compact_messages_for_display([message])
        assert message["toolScreenshot"] == PNG_DATA_URI

    def test_a_url_screenshot_is_left_alone(self):
        message = {"toolScreenshot": f"/api/v1/artifacts/{PNG_DIGEST}"}
        sessions_helpers.compact_messages_for_display([message])
        assert message["toolScreenshot"] == f"/api/v1/artifacts/{PNG_DIGEST}"

    def test_a_malformed_data_uri_is_left_alone(self):
        message = {"toolScreenshot": "data:image/png;base64,!!!not-base64!!!"}
        with patch("app.core.artifact_store.artifact_path", return_value=Path("/tmp/x")):
            sessions_helpers.compact_messages_for_display([message])
        assert message["toolScreenshot"].startswith("data:image/png;base64,")


# =============================================================================
# Endpoint: GET /sessions/{id} and /live
# =============================================================================

@pytest.fixture
def api_client():
    """Sessions router with auth + the DB stubbed for a transcript read."""
    with patch("app.api.sessions.database") as mock_db, \
            patch("app.api.sessions_helpers.database", mock_db), \
            patch("app.api.sessions_export.database", mock_db), \
            patch("app.api.sessions_fork.database", mock_db), \
            patch("app.api.sessions_worktree.database", mock_db), \
            patch("app.api.common.database", mock_db), \
            patch("app.api.sessions.get_api_user_from_request", return_value=None), \
            patch("app.api.sessions_helpers.get_api_user_from_request", return_value=None), \
            patch("app.api.common.get_api_user_from_request", return_value=None):
        mock_db.get_installs_map.return_value = {}
        mock_db.get_project.return_value = None
        mock_db.get_session_tags.return_value = []
        mock_db.session_has_forks.return_value = False
        mock_db.get_all_session_usage.return_value = []
        mock_db.get_session_context_usage_snapshot.return_value = None

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_auth] = lambda: "test-token"
        with TestClient(app) as client:
            yield client, mock_db


SESSION_ROW = {
    "id": "sess-1",
    "title": "Long session",
    "profile_id": "default",
    "project_id": None,
    "status": "active",
    "is_active": False,
    "sdk_session_id": "sdk-1",
    "created_at": "2026-08-28T10:00:00",
    "updated_at": "2026-08-28T10:00:00",
}


def _store_transcript(count):
    return [
        {"id": f"m{i}", "role": "user" if i % 2 == 0 else "assistant",
         "content": f"body-{i}", "type": "text", "metadata": {}}
        for i in range(count)
    ], {}


class TestGetSessionPagination:
    def _get(self, api_client, query=""):
        client, mock_db = api_client
        mock_db.get_session.return_value = dict(SESSION_ROW)
        with patch("app.core.message_resolver.resolve_working_dir", return_value="/w"), \
                patch("app.core.cost_utils.get_cached_profile_map", return_value={}), \
                patch("app.core.cost_utils.get_cached_user_metadata_map", return_value={}), \
                patch("app.core.cost_utils.attach_cost_metadata"), \
                patch("app.api.sessions.parse_session_history_via_store",
                      return_value=_store_transcript(10)):
            return client.get(f"/api/v1/sessions/sess-1{query}")

    def test_default_returns_the_whole_transcript(self, api_client):
        """Backward compatible: existing clients see exactly what they saw."""
        response = self._get(api_client)
        assert response.status_code == 200
        assert len(response.json()["messages"]) == 10
        assert response.headers["X-Message-Total"] == "10"
        assert response.headers["X-Message-Has-More"] == "false"
        assert "X-Message-Next-Before-Seq" not in response.headers

    def test_limit_returns_the_newest_window_with_a_cursor(self, api_client):
        response = self._get(api_client, "?message_limit=4")
        assert response.status_code == 200
        contents = [m["content"] for m in response.json()["messages"]]
        assert contents == ["body-6", "body-7", "body-8", "body-9"]
        assert response.headers["X-Message-Total"] == "10"
        assert response.headers["X-Message-Returned"] == "4"
        assert response.headers["X-Message-Has-More"] == "true"
        assert response.headers["X-Message-Next-Before-Seq"] == "6"

    def test_cursor_returns_the_previous_page(self, api_client):
        response = self._get(api_client, "?message_limit=4&before_seq=6")
        contents = [m["content"] for m in response.json()["messages"]]
        assert contents == ["body-2", "body-3", "body-4", "body-5"]
        assert response.headers["X-Message-Next-Before-Seq"] == "2"

    def test_invalid_limit_is_rejected(self, api_client):
        assert self._get(api_client, "?message_limit=0").status_code == 422

    def test_tool_input_and_screenshot_are_capped_on_the_detail_wire(self, api_client):
        """A Write call's whole-file input never reaches the client verbatim."""
        client, mock_db = api_client
        mock_db.get_session.return_value = dict(SESSION_ROW)
        transcript = [{
            "id": "m0", "role": "assistant", "type": "tool_use",
            "content": "", "toolName": "Write",
            "toolInput": {"file_path": "/big.py", "content": "print(1)\n" * 20_000},
            "toolScreenshot": PNG_DATA_URI,
            "metadata": {},
        }]
        with patch("app.core.message_resolver.resolve_working_dir", return_value="/w"), \
                patch("app.core.cost_utils.get_cached_profile_map", return_value={}), \
                patch("app.core.cost_utils.get_cached_user_metadata_map", return_value={}), \
                patch("app.core.cost_utils.attach_cost_metadata"), \
                patch("app.core.artifact_store.artifact_path",
                      side_effect=lambda h: Path("/tmp") / h if h == PNG_DIGEST else None), \
                patch("app.api.sessions.parse_session_history_via_store",
                      return_value=(transcript, {})):
            response = client.get("/api/v1/sessions/sess-1")

        assert response.status_code == 200
        message = response.json()["messages"][0]
        assert len(message["toolInput"]["content"]) < 3000
        assert message["toolInput"]["file_path"] == "/big.py"
        assert message["tool_input"] == message["toolInput"]  # both spellings capped
        assert message["metadata"]["toolInputTruncated"] is True
        assert message["toolScreenshot"] == f"/api/v1/artifacts/{PNG_DIGEST}"
        # The parser's cached transcript is never mutated by the response shaping.
        assert len(transcript[0]["toolInput"]["content"]) == len("print(1)\n") * 20_000
        assert transcript[0]["toolScreenshot"] == PNG_DATA_URI


class TestLiveEndpointPayload:
    def _live(self, api_client, query="", session_fields=None):
        client, mock_db = api_client
        mock_db.get_session.return_value = {**SESSION_ROW, **(session_fields or {})}
        transcript, _ = _store_transcript(6)
        transcript[1]["toolInput"] = {"content": "w" * 30_000}
        with patch("app.core.message_resolver.resolve_working_dir", return_value="/w"), \
                patch("app.core.cost_utils.get_cached_profile_map", return_value={}), \
                patch("app.core.cost_utils.get_cached_user_metadata_map", return_value={}), \
                patch("app.core.cost_utils.attach_cost_metadata"), \
                patch("app.api.sessions.parse_session_history_via_store",
                      return_value=(transcript, {})):
            return client.get(f"/api/v1/sessions/sess-1/live{query}")

    def test_default_ships_the_whole_transcript_with_a_window_descriptor(self, api_client):
        response = self._live(api_client)
        assert response.status_code == 200
        body = response.json()
        assert len(body["messages"]) == 6
        assert body["message_pagination"]["total"] == 6
        assert body["message_pagination"]["has_more"] is False

    def test_window_and_cursor(self, api_client):
        body = self._live(api_client, "?message_limit=2").json()
        assert [m["content"] for m in body["messages"]] == ["body-4", "body-5"]
        assert body["message_pagination"]["next_before_seq"] == 4
        assert body["message_pagination"]["has_more"] is True

    def test_tool_input_is_capped_on_the_wire(self, api_client):
        body = self._live(api_client).json()
        tool_msg = [m for m in body["messages"] if m.get("toolInput")][0]
        assert len(tool_msg["toolInput"]["content"]) < 3000
        assert tool_msg["metadata"]["toolInputTruncated"] is True

    @pytest.mark.parametrize("project_id,project_v2_id", [
        ("ai-shuffle", "pv2-local"),
        (None, "pv2-remote"),
        (None, None),
    ])
    def test_metadata_only_snapshot_restores_project_identity(self, api_client, project_id, project_v2_id):
        response = self._live(api_client, "?include_messages=false", {
            "project_id": project_id,
            "project_v2_id": project_v2_id,
        })
        assert response.status_code == 200
        meta = response.json()["session_meta"]
        assert meta["project_id"] == project_id
        assert meta["project_v2_id"] == project_v2_id

    def test_metadata_only_snapshot_still_reports_a_window(self, api_client):
        body = self._live(api_client, "?include_messages=false").json()
        assert body["messages"] == []
        assert body["message_pagination"]["total"] == 0
