"""The projects / notes / tags / templates / preferences routers must not do
blocking work on the event loop.

Every synchronous psycopg2 call (and every ``stat``/``open``/``rmtree``) made
directly from an ``async def`` handler freezes EVERY other request, WebSocket
frame and SSE stream served by the same worker for its whole duration. These
tests pin the fix by identity rather than by wire shape: each one records the
thread the blocking callable actually ran on and requires it to differ from the
loop thread — and, just as importantly, requires each handler to use ONE thread
hop, since dozens of individual ``to_thread`` wrappers would trade the stall for
scheduler churn and pool contention on every request.

The single-hop assertions double as the guard on the read-decide-write
sequences (create/update/link, note edits, session-tag replacement): if a
handler's existence check and its write ever land in separate hops, the hop
count here goes up and the test fails.
"""

import asyncio
import shutil
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import notes as notes_module
from app.api import preferences as preferences_module
from app.api import projects as projects_module
from app.api import tags as tags_module
from app.api import templates as templates_module
from app.api.auth import require_admin, require_auth

# =============================================================================
# Helpers
# =============================================================================


class OffloadTracker:
    """Wrap a router module's ``run_sync`` and record where work ran.

    ``loop_threads`` collects the thread each offload was *requested* from (the
    event loop); ``worker_threads`` collects the thread the synchronous body
    actually *ran* on. ``calls`` names the offloaded callables, so a handler's
    hop count is observable.
    """

    def __init__(self, module):
        self._module = module
        self.loop_threads: list[int] = []
        self.worker_threads: list[int] = []
        self.calls: list[str] = []

    def __enter__(self):
        # A module that never imported ``run_sync`` does all its work inline:
        # leave it unpatched so the assertions below report "nothing was
        # offloaded" rather than blowing up on a missing attribute.
        real_run_sync = getattr(self._module, "run_sync", None)
        if real_run_sync is None:
            self._patch = None
            return self

        async def tracking_run_sync(fn, *args, **kwargs):
            self.loop_threads.append(threading.get_ident())
            self.calls.append(getattr(fn, "__name__", repr(fn)))

            def recording(*a, **k):
                self.worker_threads.append(threading.get_ident())
                return fn(*a, **k)

            return await real_run_sync(recording, *args, **kwargs)

        self._patch = patch.object(self._module, "run_sync", tracking_run_sync)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        if self._patch is not None:
            self._patch.stop()
        return False

    # -- assertions ---------------------------------------------------------

    def assert_off_loop(self):
        assert self.worker_threads, "no synchronous work was offloaded at all"
        assert set(self.worker_threads).isdisjoint(self.loop_threads), (
            "blocking work ran on the event loop thread"
        )

    def assert_single_hop(self):
        self.assert_off_loop()
        assert len(self.calls) == 1, (
            f"expected ONE offload for this handler, saw {self.calls}"
        )
        assert len(set(self.worker_threads)) == 1, (
            f"expected one worker thread, saw {set(self.worker_threads)}"
        )


def recorder(sink, value=None, factory=None):
    """A mock side effect that records the thread it ran on."""

    def _call(*args, **kwargs):
        sink.append(threading.get_ident())
        return factory(*args, **kwargs) if factory is not None else value

    return _call


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def temp_dir():
    tmpdir = tempfile.mkdtemp()
    yield Path(tmpdir)
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def sample_project():
    return {
        "id": "test-project",
        "name": "Test Project",
        "description": "A test project",
        "path": "test-project",
        "settings": {},
        "created_at": "2024-01-15T10:30:00",
        "updated_at": "2024-01-15T10:30:00",
    }


@pytest.fixture
def sample_note():
    return {
        "id": "note-abc123def456",
        "title": "My Note",
        "content": "Some content",
        "tags": [],
        "is_pinned": False,
        "file_path": None,
        "created_at": "2024-01-15T10:30:00",
        "updated_at": "2024-01-15T10:30:00",
    }


@pytest.fixture
def projects_client(temp_dir):
    mock_settings = MagicMock()
    mock_settings.workspace_dir = temp_dir
    mock_settings.effective_workspace_dir = temp_dir
    mock_settings.effective_instance_id = "inst-1"

    with patch("app.api.projects.database") as mock_db, \
         patch("app.api.projects.get_api_user_from_request") as api_user, \
         patch("app.api.common.get_api_user_from_request", new=api_user), \
         patch("app.api.projects.settings", mock_settings), \
         patch("app.core.project_utils.settings", mock_settings):
        api_user.return_value = None
        app = FastAPI()
        app.include_router(projects_module.router)
        app.dependency_overrides[require_auth] = lambda: "t"
        app.dependency_overrides[require_admin] = lambda: "t"
        with TestClient(app) as client:
            yield client, mock_db


@pytest.fixture
def notes_client(temp_dir):
    with patch("app.api.notes.database") as mock_db, \
         patch("app.core.note_files.settings") as mock_settings:
        mock_settings.effective_data_dir = temp_dir
        app = FastAPI()
        app.include_router(notes_module.router)
        app.dependency_overrides[require_auth] = lambda: "t"
        with TestClient(app) as client:
            yield client, mock_db


@pytest.fixture
def tags_client():
    with patch("app.api.tags.database") as mock_db, \
         patch("app.api.tags.check_session_access", MagicMock()):
        app = FastAPI()
        app.include_router(tags_module.router)
        app.dependency_overrides[require_auth] = lambda: "t"
        with TestClient(app) as client:
            yield client, mock_db


@pytest.fixture
def templates_client():
    with patch("app.api.templates.database") as mock_db:
        app = FastAPI()
        app.include_router(templates_module.router)
        app.dependency_overrides[require_auth] = lambda: "t"
        with TestClient(app) as client:
            yield client, mock_db


@pytest.fixture
def preferences_client():
    with patch("app.api.preferences.database") as mock_db, \
         patch("app.api.preferences.get_user_identity", return_value=("admin", "admin")):
        app = FastAPI()
        app.include_router(preferences_module.router)
        app.dependency_overrides[require_auth] = lambda: "t"
        with TestClient(app) as client:
            yield client, mock_db


# =============================================================================
# preferences — hit on nearly every page load
# =============================================================================


class TestPreferencesOffload:
    def test_get_all_preferences_is_one_off_loop_hop(self, preferences_client):
        client, mock_db = preferences_client
        threads: list[int] = []
        mock_db.get_all_user_preferences.side_effect = recorder(
            threads, [{"key": "openTabs", "value": ["a"]}]
        )

        with OffloadTracker(preferences_module) as tracker:
            response = client.get("/api/v1/preferences/all")

        assert response.status_code == 200
        assert response.json() == {"preferences": {"openTabs": ["a"]}}
        tracker.assert_single_hop()
        assert threads and set(threads).isdisjoint(tracker.loop_threads)

    def test_set_preference_is_one_off_loop_hop(self, preferences_client):
        client, mock_db = preferences_client
        mock_db.set_user_preference.return_value = {
            "key": "theme", "value": "dark", "updated_at": None
        }

        with OffloadTracker(preferences_module) as tracker:
            response = client.put(
                "/api/v1/preferences/theme", json={"key": "theme", "value": "dark"}
            )

        assert response.status_code == 200
        assert response.json()["value"] == "dark"
        tracker.assert_single_hop()


async def test_preferences_read_does_not_stall_the_event_loop():
    """The point of the offload: a slow preferences query must not freeze the
    other coroutines on the worker. A 150 ms read must not stop a 10 ms ticker."""
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    def slow_read(user_type, user_id):
        time.sleep(0.15)
        return [{"key": "openTabs", "value": []}]

    request = SimpleNamespace(state=SimpleNamespace(api_user=None))
    with patch.object(preferences_module.database,
                      "get_all_user_preferences", slow_read), \
         patch.object(preferences_module, "get_user_identity",
                      return_value=("admin", "admin")):
        task = asyncio.create_task(ticker())
        result = await preferences_module.get_all_preferences(request=request, token="t")
        task.cancel()

    assert result.preferences == {"openTabs": []}
    assert ticks >= 5, f"the event loop was stalled during the read ({ticks} ticks)"


# =============================================================================
# projects — file listing (a stat per directory entry) and the mutations
# =============================================================================


class TestProjectsOffload:
    def test_list_project_files_walks_the_directory_off_the_loop(
        self, projects_client, temp_dir, sample_project
    ):
        """The listing's ``iterdir`` + per-entry ``stat`` is the most expensive
        thing this endpoint does, and the scanner cannot see it — it is not a DB
        call. It must share the project read's single hop."""
        client, mock_db = projects_client
        project_dir = temp_dir / "test-project"
        project_dir.mkdir()
        (project_dir / "a.txt").write_text("hello")
        (project_dir / "sub").mkdir()

        threads: list[int] = []
        mock_db.get_project.side_effect = recorder(threads, sample_project)

        with OffloadTracker(projects_module) as tracker:
            response = client.get("/api/v1/projects/test-project/files")

        assert response.status_code == 200
        names = [f["name"] for f in response.json()["files"]]
        assert names == ["sub", "a.txt"]
        tracker.assert_single_hop()
        assert threads and set(threads).isdisjoint(tracker.loop_threads)

    def test_create_project_keeps_read_decide_write_in_one_hop(
        self, projects_client, sample_project
    ):
        """The duplicate-id probe and the INSERT are one decision: split across
        hops, a concurrent create could claim the id in between."""
        client, mock_db = projects_client
        threads: list[int] = []
        mock_db.get_project.side_effect = recorder(threads, None)
        mock_db.create_project.side_effect = recorder(threads, sample_project)

        with OffloadTracker(projects_module) as tracker:
            response = client.post(
                "/api/v1/projects",
                json={"id": "test-project", "name": "Test Project"},
            )

        assert response.status_code == 201
        tracker.assert_single_hop()
        # probe and write ran back to back in the SAME worker thread
        assert len(threads) == 2 and len(set(threads)) == 1
        assert set(threads).isdisjoint(tracker.loop_threads)

    def test_delete_project_keeps_read_decide_write_in_one_hop(
        self, projects_client, sample_project
    ):
        client, mock_db = projects_client
        threads: list[int] = []
        mock_db.get_project.side_effect = recorder(threads, sample_project)
        mock_db.delete_project.side_effect = recorder(threads, True)

        with OffloadTracker(projects_module) as tracker:
            response = client.delete("/api/v1/projects/test-project")

        assert response.status_code == 204
        tracker.assert_single_hop()
        assert len(threads) == 2 and len(set(threads)) == 1

    def test_link_project_keeps_both_reads_and_all_writes_in_one_hop(
        self, projects_client, sample_project
    ):
        """Two reads decide the group id that four writes then apply."""
        client, mock_db = projects_client
        other = dict(sample_project, id="other-project", path="other-project")
        rows = {"test-project": sample_project, "other-project": other}
        threads: list[int] = []
        mock_db.get_project.side_effect = recorder(
            threads, factory=lambda pid: rows.get(pid)
        )
        mock_db.get_project_session_count.side_effect = recorder(threads, 0)
        mock_db.update_project.side_effect = recorder(threads, None)
        mock_db.set_project_link_group.side_effect = recorder(threads, None)

        with OffloadTracker(projects_module) as tracker:
            response = client.post(
                "/api/v1/projects/test-project/link",
                json={"target_project_id": "other-project"},
            )

        assert response.status_code == 200
        tracker.assert_single_hop()
        assert len(set(threads)) == 1
        assert set(threads).isdisjoint(tracker.loop_threads)

    def test_delete_file_offloads_the_filesystem_removal(
        self, projects_client, temp_dir, sample_project
    ):
        """The DB read and the ``unlink`` are both blocking; the second hop
        exists only so ``api_error_handler`` keeps wrapping the removal alone."""
        client, mock_db = projects_client
        project_dir = temp_dir / "test-project"
        project_dir.mkdir()
        (project_dir / "gone.txt").write_text("x")
        mock_db.get_project.return_value = sample_project

        with OffloadTracker(projects_module) as tracker:
            response = client.delete(
                "/api/v1/projects/test-project/files", params={"path": "gone.txt"}
            )

        assert response.status_code == 200
        assert not (project_dir / "gone.txt").exists()
        tracker.assert_off_loop()
        assert tracker.calls == ["_resolve", "_remove"]


# =============================================================================
# notes — DB rows backed by markdown files on disk
# =============================================================================


class TestNotesOffload:
    def test_get_note_reads_and_reconciles_in_one_hop(self, notes_client, sample_note):
        """``_reconcile_note_with_disk`` stats and may re-read the markdown file
        and write the row back — it belongs in the read's hop, not on the loop."""
        client, mock_db = notes_client
        threads: list[int] = []
        mock_db.get_note.side_effect = recorder(threads, sample_note)

        with OffloadTracker(notes_module) as tracker:
            response = client.get(f"/api/v1/notes/{sample_note['id']}")

        assert response.status_code == 200
        assert response.json()["title"] == "My Note"
        tracker.assert_single_hop()
        assert threads and set(threads).isdisjoint(tracker.loop_threads)

    def test_create_note_writes_the_markdown_file_off_the_loop(
        self, notes_client, sample_note
    ):
        client, mock_db = notes_client
        threads: list[int] = []
        mock_db.get_all_note_filenames.side_effect = recorder(threads, [])
        mock_db.create_note.side_effect = recorder(threads, sample_note)

        with OffloadTracker(notes_module) as tracker:
            response = client.post(
                "/api/v1/notes", json={"title": "My Note", "content": "Some content"}
            )

        assert response.status_code == 201
        tracker.assert_single_hop()
        # filename read + INSERT (and the file write between them) share a thread
        assert len(threads) == 2 and len(set(threads)) == 1

    def test_update_note_keeps_read_decide_write_in_one_hop(
        self, notes_client, sample_note
    ):
        """The current row decides which file is renamed and what path is
        stored, so the read, the disk write and the UPDATE cannot be split."""
        client, mock_db = notes_client
        threads: list[int] = []
        mock_db.get_note.side_effect = recorder(threads, sample_note)
        mock_db.get_all_note_filenames.side_effect = recorder(threads, [])
        mock_db.update_note.side_effect = recorder(
            threads, dict(sample_note, title="Renamed")
        )

        with OffloadTracker(notes_module) as tracker:
            response = client.patch(
                f"/api/v1/notes/{sample_note['id']}", json={"title": "Renamed"}
            )

        assert response.status_code == 200
        assert response.json()["title"] == "Renamed"
        tracker.assert_single_hop()
        assert len(threads) == 3 and len(set(threads)) == 1


# =============================================================================
# tags / templates
# =============================================================================


class TestTagsOffload:
    def test_list_tags_is_off_the_loop(self, tags_client):
        client, mock_db = tags_client
        threads: list[int] = []
        mock_db.get_all_tags.side_effect = recorder(threads, [])

        with OffloadTracker(tags_module) as tracker:
            response = client.get("/api/v1/tags")

        assert response.status_code == 200
        tracker.assert_single_hop()
        assert threads and set(threads).isdisjoint(tracker.loop_threads)

    def test_set_session_tags_validates_and_writes_in_one_hop(self, tags_client):
        """A tag deleted between the validation and the replacement write would
        be a race this refactor must not introduce."""
        client, mock_db = tags_client
        tag = {"id": "tag-1", "name": "Bug", "color": "#ff0000",
               "created_at": "2024-01-15T10:30:00",
               "updated_at": "2024-01-15T10:30:00"}
        threads: list[int] = []
        mock_db.get_session.side_effect = recorder(threads, {"id": "sess-1"})
        mock_db.get_tag.side_effect = recorder(threads, tag)
        mock_db.set_session_tags.side_effect = recorder(threads, [tag])

        with OffloadTracker(tags_module) as tracker:
            response = client.put(
                "/api/v1/tags/session/sess-1", json={"tag_ids": ["tag-1"]}
            )

        assert response.status_code == 200
        tracker.assert_single_hop()
        assert len(threads) == 3 and len(set(threads)) == 1
        assert set(threads).isdisjoint(tracker.loop_threads)


class TestTemplatesOffload:
    def test_list_templates_is_off_the_loop(self, templates_client):
        client, mock_db = templates_client
        threads: list[int] = []
        mock_db.get_all_templates.side_effect = recorder(threads, [])

        with OffloadTracker(templates_module) as tracker:
            response = client.get("/api/v1/templates")

        assert response.status_code == 200
        tracker.assert_single_hop()
        assert threads and set(threads).isdisjoint(tracker.loop_threads)

    def test_update_template_keeps_guard_and_write_in_one_hop(self, templates_client):
        """Existence read, builtin guard and UPDATE are one decision."""
        client, mock_db = templates_client
        template = {
            "id": "tmpl-1", "name": "T", "description": None, "prompt": "p",
            "profile_id": None, "icon": None, "category": None,
            "is_builtin": False, "created_at": "2024-01-15T10:30:00",
            "updated_at": "2024-01-15T10:30:00",
        }
        threads: list[int] = []
        mock_db.get_template.side_effect = recorder(threads, template)
        mock_db.update_template.side_effect = recorder(
            threads, dict(template, name="T2")
        )

        with OffloadTracker(templates_module) as tracker:
            response = client.patch("/api/v1/templates/tmpl-1", json={"name": "T2"})

        assert response.status_code == 200
        tracker.assert_single_hop()
        assert len(threads) == 2 and len(set(threads)) == 1
