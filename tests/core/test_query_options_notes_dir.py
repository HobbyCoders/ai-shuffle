"""
Regression tests: the notes directory is always an accessible directory.

Notes are materialized to the data dir (e.g. /data/notes in Docker), which lives
OUTSIDE every possible agent working directory — the workspace, a project, and a
worktree all sit under the workspace root, never under the data dir. The SDK's
file tools can only touch the cwd plus ``add_dirs``, so an @-referenced note path
like ``/data/notes/foo.md`` is unreachable unless we register the notes dir.

These tests pin that wiring: build_options_from_profile must include the notes
dir in ``add_dirs`` for every session, merged with any profile-provided dirs and
without duplicating it.

build_options_from_profile makes one unavoidable DB call (get_security_instructions);
we patch that single boundary and let the rest run against on-disk config.
"""

from unittest.mock import patch

import app.core.query_options as qo
from app.core.note_files import get_notes_dir
from tests.core.helpers import query_profile


def test_notes_dir_added_when_profile_has_no_add_dirs():
    notes_dir = str(get_notes_dir())
    with patch.object(qo, "get_security_instructions", return_value=""):
        opts, _ = qo.build_options_from_profile(profile=query_profile())
    assert opts.add_dirs is not None
    assert notes_dir in [str(d) for d in opts.add_dirs]


def test_notes_dir_merged_with_profile_add_dirs():
    notes_dir = str(get_notes_dir())
    with patch.object(qo, "get_security_instructions", return_value=""):
        opts, _ = qo.build_options_from_profile(
            profile=query_profile({"add_dirs": ["/workspace/some-project"]})
        )
    dirs = [str(d) for d in opts.add_dirs]
    assert "/workspace/some-project" in dirs
    assert notes_dir in dirs


def test_notes_dir_not_duplicated_when_already_present():
    notes_dir = str(get_notes_dir())
    with patch.object(qo, "get_security_instructions", return_value=""):
        opts, _ = qo.build_options_from_profile(
            profile=query_profile({"add_dirs": [notes_dir]})
        )
    dirs = [str(d) for d in opts.add_dirs]
    assert dirs.count(notes_dir) == 1
