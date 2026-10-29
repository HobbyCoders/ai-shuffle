"""#790 on disposable PostgreSQL: two first-time inserts of a project's
git_repositories row race; both succeed and the table keeps one row."""
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401  (pg_schema: autouse fixture)

pytestmark = requires_real_pg


@pytest.fixture
def project():
    from app.db import base, database
    project_id = f"pg-git-race-{uuid4().hex[:12]}"
    database.create_project(project_id, "Git race", "", f"/tmp/{project_id}", {})
    yield project_id
    with base.get_db() as conn:
        conn.cursor().execute("DELETE FROM projects WHERE id = %s", (project_id,))


def _rows(project_id):
    from app.db import base
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM git_repositories WHERE project_id = %s", (project_id,))
        return [row["id"] for row in cursor.fetchall()]


def test_concurrent_first_inserts_keep_one_row(project):
    from app.db import database
    both_ready = threading.Barrier(2, timeout=10)

    def create(index):
        both_ready.wait()
        return database.create_git_repository(f"repo-{uuid4().hex[:12]}", project,
                                              default_branch=f"branch-{index}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))

    stored = _rows(project)
    assert len(stored) == 1
    assert [row["id"] for row in results] == stored * 2
