"""Session-list enrichment: identical output, and a fixed number of queries.

Covers finding B2 (backend performance review 2026-08). The enrichment
pipeline runs on every sidebar poll, so:

* it must produce exactly the same rows it did before the batching work
  (the golden test below pins the full annotated shape), and
* its query count must not grow with the size of the page — no
  ``get_api_user`` per user, no ``get_project`` per distinct project, no full
  ``projects_v2`` scan.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.sessions as sessions_module
import app.db.projects as projects_db
from app.api import sessions_helpers
from app.db.projects import get_projects_by_ids
from app.api.auth import require_auth


@pytest.fixture
def install_settings():
    """Settings whose instance id owns 'me-install'."""
    mock_settings = MagicMock()
    mock_settings.effective_instance_id = "me-install"
    return mock_settings


def _sessions(count, *, project_prefix="proj", instance="me-install"):
    return [
        {
            "id": f"s{i}",
            "instance_id": instance,
            "project_id": f"{project_prefix}-{i}",
            "api_user_id": f"user-{i}",
        }
        for i in range(count)
    ]


def _enrich(sessions, install_settings, *, users=None, tags=None, forks=None):
    """Run the real enrichment pipeline with only the leaf reads stubbed."""
    with patch("app.api.sessions_helpers.get_tags_for_sessions",
               return_value=tags or {}) as mock_tags, \
            patch("app.api.sessions_helpers.get_fork_info_for_sessions",
                  return_value=forks or {}) as mock_forks, \
            patch("app.api.sessions_helpers.get_api_users_by_ids",
                  return_value=users or {}) as mock_users, \
            patch("app.core.config.settings", install_settings), \
            patch.object(sessions_helpers.database, "get_installs_map",
                         return_value={"me-install": {"device_name": "Mine", "online": True}}):
        result = sessions_helpers.enrich_sessions_with_tags(sessions)
    return result, mock_tags, mock_forks, mock_users


class TestEnrichmentOutputIsUnchanged:
    """Golden shape: every field the sessions board reads, byte for byte."""

    def test_golden_annotated_session(self, install_settings, mock_db):
        from app.db import database

        database.create_project(
            project_id="proj-0",
            name="Zero",
            description=None,
            path="zero",
        )
        sessions = [{
            "id": "s0",
            "instance_id": "me-install",
            "project_id": "proj-0",
            "api_user_id": "user-0",
        }]

        result, _, _, _ = _enrich(
            sessions,
            install_settings,
            users={"user-0": {"id": "user-0", "name": "Ada"}},
            tags={"s0": [{"id": "t1", "name": "Bug"}]},
            forks={"s0": {"has_forks": True}},
        )

        assert result is sessions  # enriched in place, returned for chaining
        assert result[0] == {
            "id": "s0",
            "instance_id": "me-install",
            "project_id": "proj-0",
            "api_user_id": "user-0",
            "api_user_name": "Ada",
            "has_running_tasks": False,
            "tags": [{"id": "t1", "name": "Bug"}],
            "has_forks": True,
            "is_remote": False,
            "is_pinned": False,
            "degraded": False,
            "device_name": "Mine",
            "device_online": True,
            "controllable": True,
            "project_name": "Zero",
        }

    def test_remote_session_keeps_its_device_annotation(self, install_settings, mock_db):
        sessions = [{"id": "s0", "instance_id": "other-install"}]
        with patch("app.api.sessions_helpers.get_tags_for_sessions", return_value={}), \
                patch("app.api.sessions_helpers.get_fork_info_for_sessions", return_value={}), \
                patch("app.core.config.settings", install_settings), \
                patch.object(sessions_helpers.database, "get_installs_map",
                             return_value={"other-install": {"device_name": "Worker", "online": True}}):
            sessions_helpers.enrich_sessions_with_tags(sessions)

        assert sessions[0]["is_remote"] is True
        assert sessions[0]["device_name"] == "Worker"
        assert sessions[0]["controllable"] is True

    def test_empty_list_short_circuits(self, install_settings):
        with patch("app.api.sessions_helpers.get_tags_for_sessions") as mock_tags:
            assert sessions_helpers.enrich_sessions_with_tags([]) == []
        mock_tags.assert_not_called()


class TestNoNPlusOne:
    """Query count is a function of the pipeline, never of the page size."""

    def _statements(self, raw_conn, sessions, install_settings):
        """Every SQL statement one enrichment executes against the test DB."""
        traced: list = []
        raw_conn.set_trace_callback(traced.append)
        try:
            _enrich(sessions, install_settings)
        finally:
            raw_conn.set_trace_callback(None)
        return [
            stmt for stmt in traced
            if stmt.strip().upper().startswith(("SELECT", "INSERT", "UPDATE", "DELETE"))
        ]

    def test_query_count_is_flat_in_page_size(self, install_settings, mock_db):
        """3 sessions/3 projects and 12 sessions/12 projects cost the same."""
        small = self._statements(mock_db, _sessions(3), install_settings)
        large = self._statements(mock_db, _sessions(12), install_settings)
        assert len(small) == len(large), (
            f"enrichment ran {len(large)} statements for 12 sessions vs "
            f"{len(small)} for 3 — a per-row query crept back in:\n"
            + "\n".join(large)
        )

    def test_projects_are_resolved_by_one_in_query(self, install_settings, mock_db):
        """Exactly ONE projects read for the whole page, whatever its size."""
        statements = self._statements(mock_db, _sessions(9), install_settings)
        project_reads = [s for s in statements if "FROM projects " in s]
        assert len(project_reads) == 1, statements
        assert " IN (" in project_reads[0]

    def test_projects_are_never_fetched_one_by_one(self, install_settings, mock_db):
        """``get_project`` (the old N+1) must not be called at all — neither
        through the API-layer facade nor inside the relocated DB helper."""
        with patch.object(sessions_helpers.database, "get_project",
                          side_effect=AssertionError("per-project N+1")) as mock_facade, \
                patch.object(projects_db, "get_project",
                             side_effect=AssertionError("per-project N+1")) as mock_db_layer:
            _enrich(_sessions(5), install_settings)
        mock_facade.assert_not_called()
        mock_db_layer.assert_not_called()

    def test_api_users_are_fetched_in_one_batch(self, install_settings, mock_db):
        """One ``IN (...)`` read for the page, never ``get_api_user`` per row."""
        sessions = _sessions(4)
        with patch.object(sessions_helpers.database, "get_api_user",
                          side_effect=AssertionError("per-user N+1")):
            _, _, _, mock_users = _enrich(sessions, install_settings)

        mock_users.assert_called_once()
        assert sorted(mock_users.call_args[0][0]) == ["user-0", "user-1", "user-2", "user-3"]

    def test_projects_v2_is_not_scanned_whole(self, install_settings, mock_db):
        """The full ``list_projects_v2()`` scan is replaced by targeted reads."""
        with patch("app.db.projects_v2.list_projects_v2",
                   side_effect=AssertionError("full projects_v2 scan")) as mock_scan:
            _enrich(_sessions(3), install_settings)
        mock_scan.assert_not_called()

    def test_batched_project_lookup_resolves_every_row(self, install_settings, mock_db):
        """The batched read returns the same rows the per-id lookup did."""
        from app.db import database

        for i in range(3):
            database.create_project(
                project_id=f"proj-{i}", name=f"P{i}", description=None, path=f"p{i}",
            )
        resolved = get_projects_by_ids(["proj-0", "proj-1", "proj-2", "missing"])
        assert sorted(resolved) == ["proj-0", "proj-1", "proj-2"]
        assert resolved["proj-1"]["name"] == "P1"

    def test_batched_project_lookup_falls_back_when_the_query_fails(self, install_settings):
        """A failed batch read degrades to the per-id path, never to no data."""
        with patch.object(projects_db, "get_db", side_effect=RuntimeError("no pool")), \
                patch.object(projects_db, "get_project",
                             return_value={"id": "p1", "name": "Fallback"}):
            resolved = get_projects_by_ids(["p1"])
        assert resolved["p1"]["name"] == "Fallback"


class TestClaimableAndPlacementAreBatched:
    """The two read-path N+1s the degraded pill used to pay per page."""

    def _run(self, sessions, install_settings, claimable):
        """Enrich, counting installs reads and capturing the claimable call."""
        calls = {"installs": 0}
        registry = {
            "me-install": {
                "id": "me-install", "device_name": "Mine",
                "online": True, "capabilities": {},
            },
        }

        def _read_installs():
            calls["installs"] += 1
            return registry

        claimable_mock = MagicMock(side_effect=lambda **kw: list(claimable))
        with patch("app.api.sessions_helpers.get_tags_for_sessions", return_value={}), \
                patch("app.api.sessions_helpers.get_fork_info_for_sessions", return_value={}), \
                patch("app.api.sessions_helpers.get_api_users_by_ids", return_value={}), \
                patch("app.core.config.settings", install_settings), \
                patch("app.db.turns.list_claimable_sessions", claimable_mock), \
                patch.object(sessions_helpers.database, "get_installs_map",
                             _read_installs):
            sessions_helpers.enrich_sessions_with_tags(sessions)
        return calls, claimable_mock

    def test_claimable_read_is_targeted_at_the_page(self, install_settings, mock_db):
        """ONE claimable read, narrowed to this page's ids — not the full
        ``turns ⋈ runs`` DISTINCT scan it used to run on every listing."""
        sessions = _sessions(6)
        _, claimable_mock = self._run(sessions, install_settings, claimable=[])

        claimable_mock.assert_called_once()
        assert claimable_mock.call_args.args == ()
        assert claimable_mock.call_args.kwargs["session_ids"] == [
            f"s{i}" for i in range(6)
        ]

    def test_one_installs_read_regardless_of_degraded_candidate_count(
        self, install_settings, mock_db
    ):
        """``resolve_placement`` is called once per claimable row; the registry
        must still be read exactly ONCE for the whole page (it used to be twice
        per candidate)."""
        few = _sessions(2)
        many = _sessions(20)

        calls_few, _ = self._run(
            few, install_settings, claimable=[s["id"] for s in few],
        )
        calls_many, _ = self._run(
            many, install_settings, claimable=[s["id"] for s in many],
        )

        assert calls_few["installs"] == 1
        assert calls_many["installs"] == 1, (
            f"installs registry read {calls_many['installs']} times for 20 "
            "candidates — the placement N+1 is back"
        )

    def test_degraded_verdicts_are_unchanged_by_the_preloaded_map(
        self, install_settings, mock_db
    ):
        """Same answer as the resolver reading the registry itself."""
        sessions = _sessions(3)
        self._run(sessions, install_settings, claimable=[s["id"] for s in sessions])
        # Owner "me-install" is online+capable in the stub registry, so the
        # implicit pin resolves to a runner: not degraded.
        assert [s["degraded"] for s in sessions] == [False, False, False]

        offline = [
            {"id": "s9", "instance_id": "ghost-install", "project_id": None}
        ]
        self._run(offline, install_settings, claimable=["s9"])
        assert offline[0]["degraded"] is True


class TestOneThreadHop:
    """The listing query and the enrichment run in a single offloaded call."""

    def test_fetch_and_enrich_passes_the_query_result_through(self, install_settings, mock_db):
        fetch = MagicMock(return_value=[{"id": "s0"}])
        with patch("app.api.sessions_helpers.enrich_sessions_with_tags") as mock_enrich:
            result = sessions_helpers.fetch_and_enrich_sessions(fetch, limit=10)

        fetch.assert_called_once_with(limit=10)
        mock_enrich.assert_called_once_with(result)
        assert result == [{"id": "s0"}]

    def test_fetch_and_enrich_handles_tuple_returning_queries(self, install_settings, mock_db):
        """``get_sessions_paginated`` / ``_cursor`` return tuples; enrich item 0."""
        rows = [{"id": "s0"}]
        fetch = MagicMock(return_value=(rows, 7))
        with patch("app.api.sessions_helpers.enrich_sessions_with_tags") as mock_enrich:
            result = sessions_helpers.fetch_and_enrich_sessions(fetch)

        assert result == (rows, 7)
        mock_enrich.assert_called_once_with(rows)

    def test_search_results_wrapper_uses_the_search_enricher(self, install_settings, mock_db):
        rows = [{"id": "s0"}]
        fetch = MagicMock(return_value=(rows, 1))
        with patch("app.api.sessions_helpers.enrich_search_results_with_tags") as mock_enrich:
            sessions_helpers.fetch_and_enrich_search_results(fetch, query="q")
        mock_enrich.assert_called_once_with(rows)


class TestListEndpointsOffloadTheWholePipeline:
    """Each list endpoint does its DB work in exactly ONE thread hop."""

    @pytest.fixture
    def client(self, request):
        with patch("app.api.sessions.database") as mock_db, \
                patch("app.api.sessions_helpers.database", mock_db), \
                patch("app.api.sessions_export.database", mock_db), \
                patch("app.api.sessions_fork.database", mock_db), \
                patch("app.api.sessions_worktree.database", mock_db), \
                patch("app.api.common.database", mock_db), \
                patch("app.api.sessions.get_api_user_from_request", return_value=None), \
                patch("app.api.sessions_helpers.get_api_user_from_request", return_value=None):
            row = {
                "id": "s1",
                "parked_task_kind": getattr(request, "param", None),
                "created_at": "2026-08-28T10:00:00",
                "updated_at": "2026-08-28T10:00:00",
            }
            mock_db.get_sessions.return_value = [dict(row)]
            mock_db.get_sessions_paginated.return_value = ([dict(row)], 1)
            mock_db.get_sessions_cursor.return_value = ([dict(row)], None, None, False)
            mock_db.search_sessions_paginated.return_value = (
                [dict(row, match_type="title")], 1
            )
            mock_db.get_installs_map.return_value = {}
            mock_db.get_project.return_value = None

            app = FastAPI()
            app.include_router(sessions_module.router)
            app.dependency_overrides[require_auth] = lambda: "test-token"
            with TestClient(app) as test_client:
                yield test_client

    @pytest.mark.parametrize("path", [
        "/api/v1/sessions",
        "/api/v1/sessions/paginated",
        "/api/v1/sessions/cursor",
        "/api/v1/sessions/search/paginated?q=hello",
    ])
    def test_one_run_sync_call_per_listing(self, client, path):
        """The enrichment used to run on the event loop; now it rides the
        SAME hop as the listing query rather than a second (or tenth) one."""
        offloaded = []
        real_run_sync = sessions_module.run_sync

        async def tracking_run_sync(fn, *args, **kwargs):
            offloaded.append(fn.__name__)
            return await real_run_sync(fn, *args, **kwargs)

        with patch.object(sessions_module, "run_sync", tracking_run_sync):
            response = client.get(path)

        assert response.status_code == 200, response.text
        assert offloaded in (
            ["fetch_and_enrich_sessions"],
            ["fetch_and_enrich_search_results"],
        ), offloaded

    @pytest.mark.parametrize("client", ["agent", "shell", None], indirect=True)
    @pytest.mark.parametrize("path", [
        "/api/v1/sessions",
        "/api/v1/sessions/paginated",
        "/api/v1/sessions/cursor",
        "/api/v1/sessions/search/paginated?q=hello",
    ])
    def test_listing_preserves_parked_marker(self, client, path, request):
        response = client.get(path)

        assert response.status_code == 200, response.text
        data = response.json()
        rows = data if isinstance(data, list) else data["items"]
        assert rows[0]["parked_task_kind"] == request.node.callspec.params["client"]
