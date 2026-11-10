"""app/db/compatibility.json is what lets an update skip its database copy, so
the level in it has to rise with every migration older code cannot run over."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("database_compatibility", ROOT / "scripts/ci/database_compatibility.py")
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)


def declaration(level=1, destructive="0" * 64):
    return {"format": 1, "level": level, "destructive": destructive}


def test_this_checkout_pins_its_destructive_migrations():
    current = check.read((ROOT / check.FILE).read_text())
    problems = check.errors(current, check.digest(check.statements()))
    assert not problems, "\n".join(problems)


def test_a_changed_pin_raised_the_level_since_the_base_branch():
    def git(*args):
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=30)

    base = git("merge-base", "HEAD", "origin/main")
    if base.returncode != 0 or not base.stdout.strip():
        pytest.skip("origin/main is not fetched in this checkout")
    before = check.base_declaration(base.stdout.strip())
    if before is None:
        pytest.skip("the base revision is from before database levels")
    current = check.read((ROOT / check.FILE).read_text())
    problems = check.errors(current, current["destructive"], before)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("sql", [
    "ALTER TABLE sessions DROP COLUMN title",
    "ALTER TABLE sessions DROP COLUMN IF EXISTS title",
    "DROP TABLE IF EXISTS sessions",
    "ALTER TABLE profiles RENAME TO agents",
    "ALTER TABLE sessions RENAME COLUMN a TO b",
    "ALTER TABLE sessions ALTER COLUMN a TYPE BIGINT",
    "ALTER TABLE sessions ALTER COLUMN a SET NOT NULL",
    "ALTER TABLE sessions ADD COLUMN owner TEXT NOT NULL",
    "TRUNCATE TABLE sessions",
    "TRUNCATE sessions",
])
def test_statements_older_code_cannot_run_over_are_counted(sql):
    assert check.statements_in(f"async def migrate(conn):\n    await conn.execute({sql!r})\n") == [sql]


@pytest.mark.parametrize("sql", [
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS title TEXT",
    "ALTER TABLE sessions ADD COLUMN owner TEXT NOT NULL DEFAULT ''",
    "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_title ON sessions (title)",
    "DROP INDEX IF EXISTS idx_sessions_title",
    "ALTER INDEX a RENAME TO b",
    "VACUUM (SKIP_LOCKED, TRUNCATE FALSE, PARALLEL 0) sessions",
    "Failed to truncate the file",
    "UPDATE sessions SET title = '' WHERE title IS NULL",
])
def test_additive_statements_and_ordinary_text_are_not_counted(sql):
    assert check.statements_in(f"async def migrate(conn):\n    await conn.execute({sql!r})\n") == []


def test_substituted_names_docstrings_and_comments():
    source = '''
def migrate(conn, table):
    """DROP TABLE mentioned in a docstring is not SQL the application runs."""
    # DROP TABLE in a comment is not either
    conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute("""
        ALTER TABLE notes
            DROP COLUMN IF EXISTS project_id
    """)
'''
    assert check.statements_in(source) == ["DROP TABLE IF EXISTS {}", "ALTER TABLE notes DROP COLUMN IF EXISTS project_id"]
    assert check.statements_in("def broken(:\n") == []


def test_the_digest_follows_the_statements_and_their_files():
    one = [("app/db/database.py", "DROP TABLE a")]
    assert check.digest(one) == check.digest(list(one))
    assert check.digest(one) != check.digest(one + [("app/db/database.py", "DROP TABLE b")])
    assert check.digest(one) != check.digest([("app/db/other.py", "DROP TABLE a")])
    assert check.digest(one + one) != check.digest(one), "a statement added a second time counts"


def test_statements_scans_the_application_and_skips_tests(tmp_path):
    for name, sql in [("app/db/database.py", "DROP TABLE a"), ("fluid/fluid/store.py", "DROP TABLE b"),
                      ("app/tests/test_x.py", "DROP TABLE c"), ("scripts/tool.py", "DROP TABLE d")]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"SQL = {sql!r}\n")
    assert check.statements(tmp_path) == [("app/db/database.py", "DROP TABLE a"), ("fluid/fluid/store.py", "DROP TABLE b")]


def test_errors_ask_for_a_pin_and_a_higher_level():
    pinned = declaration(destructive="a" * 64)
    assert check.errors(pinned, "a" * 64) == []
    assert "--pin" in check.errors(pinned, "b" * 64)[0]
    assert check.errors(pinned, "a" * 64, declaration(destructive="a" * 64)) == []
    assert check.errors(declaration(2, "b" * 64), "b" * 64, pinned) == []
    assert "Raise the level" in check.errors(declaration(1, "b" * 64), "b" * 64, pinned)[0]
    assert "never goes down" in check.errors(pinned, "a" * 64, declaration(2, "a" * 64))[0]
    assert check.errors(declaration(3, "a" * 64), "a" * 64, pinned) == [], "a level raised by hand is allowed"


@pytest.mark.parametrize("text", [
    "[]", "{}", json.dumps({"format": 2, "level": 1, "destructive": "0" * 64}),
    json.dumps({"format": 1, "level": 0, "destructive": "0" * 64}),
    json.dumps({"format": 1, "level": True, "destructive": "0" * 64}),
    json.dumps({"format": 1, "level": 1, "destructive": "abc"}),
])
def test_a_malformed_declaration_is_refused(text):
    with pytest.raises(ValueError):
        check.read(text)


def test_pin_raises_the_level_only_when_the_statements_changed(tmp_path, monkeypatch, capsys):
    (tmp_path / "app/db").mkdir(parents=True)
    (tmp_path / "fluid").mkdir()
    (tmp_path / "app/db/database.py").write_text("SQL = 'DROP TABLE a'\n")
    (tmp_path / check.FILE).write_text(json.dumps(declaration(4, check.digest(check.statements(tmp_path)))))
    monkeypatch.setattr(check, "ROOT", tmp_path)
    assert check.main(["--pin"]) == 0 and check.read((tmp_path / check.FILE).read_text())["level"] == 4
    assert check.main([]) == 0
    (tmp_path / "app/db/database.py").write_text("SQL = 'DROP TABLE a'\nMORE = 'DROP TABLE b'\n")
    assert check.main([]) == 1 and "--pin" in capsys.readouterr().err
    assert check.main(["--pin"]) == 0
    assert check.read((tmp_path / check.FILE).read_text()) == declaration(5, check.digest(check.statements(tmp_path)))
    assert check.main([]) == 0
