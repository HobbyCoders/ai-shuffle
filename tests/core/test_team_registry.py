"""TeamRegistry: tool_use/tool_result ingestion, filesystem fallback, kill and cleanup.

Hermetic: HOME and the Claude projects dir point into tmp_path, and os.kill is
monkeypatched, so no real process is ever signalled.
"""

import json
import signal

import pytest

from app.core import team_registry as tr
from app.core.config import settings
from app.core.jsonl_parser import get_project_dir_name
from app.core.team_registry import TeammateRecord, TeamRegistry

SID = "11111111-2222-3333-4444-555555555555"
SID2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    projects = tmp_path / ".claude" / "projects"
    projects.mkdir(parents=True)
    monkeypatch.setattr(settings, "claude_projects_dir", projects, raising=False)
    return tmp_path


@pytest.fixture
def reg():
    return TeamRegistry()


def _team_dir(home, name):
    d = home / ".claude" / "teams" / name
    d.mkdir(parents=True)
    return d


# ---------------------------------------------------------------------------
# tool_use ingestion
# ---------------------------------------------------------------------------


async def test_non_team_tool_and_missing_team_name_are_ignored(reg):
    await reg.handle_team_tool_use(
        parent_session_id="p", tool_name="Bash", tool_input={"team_name": "t"}, working_dir=None
    )
    await reg.handle_team_tool_use(
        parent_session_id="p", tool_name="Task", tool_input={"team_name": "  "}, working_dir=None
    )
    assert await reg.list_teams("p") == []


async def test_task_send_and_delete_track_teammate_status(reg):
    await reg.handle_team_tool_use(
        parent_session_id="p", tool_name="TeamCreate", tool_input={"team_name": " t "},
        working_dir=None,
    )
    await reg.handle_team_tool_use(
        parent_session_id="p", tool_name="Task",
        tool_input={"team": "t", "teammate": "scout", "prompt": "go"}, working_dir="/w",
    )
    m = await reg.get_teammate("p", "t", "scout")
    assert m.last_prompt == "go" and m.status == "running" and m.working_dir == "/w"

    # A later Task with no prompt keeps the previous one.
    await reg.handle_team_tool_use(
        parent_session_id="p", tool_name="Task",
        tool_input={"team": "t", "teammate": "scout"}, working_dir="/w",
    )
    assert (await reg.get_teammate("p", "t", "scout")).last_prompt == "go"

    await reg.handle_team_tool_use(
        parent_session_id="p", tool_name="SendMessage",
        tool_input={"team_name": "t", "to": "helper"}, working_dir=None,
    )
    assert await reg.get_teammate("p", "t", "helper") is not None

    await reg.handle_team_tool_use(
        parent_session_id="p", tool_name="TeamDelete", tool_input={"team_name": "t"},
        working_dir=None,
    )
    statuses = {m.teammate_name: m.status for m in await reg.all_teammates("p")}
    assert statuses == {"scout": "killed", "helper": "killed"}

    # The team's working_dir is filled in by the first call that knows it.
    teams = reg._teams["p"]
    assert teams["t"].working_dir == "/w"
    assert (await reg.list_teams("p"))[0]["team_name"] == "t"


async def test_get_teammate_unknown_team(reg):
    assert await reg.get_teammate("p", "nope", "x") is None


# ---------------------------------------------------------------------------
# tool_result ingestion
# ---------------------------------------------------------------------------


async def test_tool_result_merges_output_and_locates_jsonl(reg, home):
    cwd = "/work/repo"
    proj = settings.claude_projects_dir / get_project_dir_name(cwd)
    proj.mkdir(parents=True)
    (proj / f"{SID}.jsonl").write_text("{}\n")

    seen = []

    async def cb(parent, team, rec):
        seen.append((parent, team, rec.teammate_name))

    reg.set_update_callback(cb)
    changed = await reg.handle_team_tool_result(
        parent_session_id="p", tool_name="TeamCreate", tool_input={},
        tool_output=json.dumps({
            "team_name": "t",
            "teammates": [
                {"name": "scout", "session_id": SID, "pid": 4242, "status": "running"},
                {"name": "ghost"},
                "not-a-dict",
            ],
        }),
        working_dir=cwd,
    )
    names = {m.teammate_name for m in changed}
    assert names == {"scout", "ghost"}
    scout = await reg.get_teammate("p", "t", "scout")
    assert scout.teammate_session_id == SID
    assert scout.pid == 4242
    assert scout.jsonl_path == proj / f"{SID}.jsonl"
    assert ("p", "t", "scout") in seen


async def test_tool_result_bool_pid_is_rejected(reg, home):
    """A JSON ``true`` pid must never become pid 1 (then SIGTERM'd by kill)."""
    await reg.handle_team_tool_result(
        parent_session_id="p", tool_name="Task", tool_input={"team_name": "t"},
        tool_output={"teammates": [{"name": "scout", "pid": True}]},
        working_dir=None,
    )
    m = await reg.get_teammate("p", "t", "scout")
    assert m.pid is None
    assert await reg.kill_teammate("p", "t", "scout") is False


async def test_tool_result_team_name_from_text_and_no_name(reg, home):
    assert await reg.handle_team_tool_result(
        parent_session_id="p", tool_name="Bash", tool_input={}, tool_output="x", working_dir=None
    ) == []
    assert await reg.handle_team_tool_result(
        parent_session_id="p", tool_name="TeamCreate", tool_input={}, tool_output=42,
        working_dir=None,
    ) == []
    # Free text: team name from the "Team 'x'" phrase, uuid scraped but nameless -> dropped.
    changed = await reg.handle_team_tool_result(
        parent_session_id="p", tool_name="TeamCreate", tool_input={},
        tool_output=f"Team 'crew' created; member {SID}", working_dir=None,
    )
    assert changed == []
    assert [t["team_name"] for t in await reg.list_teams("p")] == ["crew"]


async def test_filesystem_fallback_reads_team_dir(reg, home):
    d = _team_dir(home, "t")
    (d / "scout.json").write_text(json.dumps({"session_id": SID2, "pid": 77}))
    (d / "roster.json").write_text(json.dumps({"members": [{"name": "helper", "sessionId": SID}]}))
    (d / "bad.json").write_text("{not json")
    (d / "list.json").write_text("[1, 2]")
    (d / "notes.txt").write_text("ignored")
    (d / "sub.json").mkdir()

    changed = await reg.handle_team_tool_result(
        parent_session_id="p", tool_name="TeamCreate", tool_input={"team_name": "t"},
        tool_output=None, working_dir=None,
    )
    by_name = {m.teammate_name: m for m in changed}
    assert set(by_name) == {"scout", "helper"}
    assert by_name["scout"].pid == 77 and by_name["scout"].teammate_session_id == SID2
    assert by_name["helper"].teammate_session_id == SID
    # No transcript on disk anywhere -> no path.
    assert by_name["scout"].jsonl_path is None


@pytest.mark.parametrize("name", ["..", ".", "../escape", "/abs", "a\\b", "x\0y"])
def test_team_dir_scan_is_confined_to_teams_root(home, name):
    """A model-authored team name must never read outside ~/.claude/teams."""
    outside = home / "escape"
    outside.mkdir()
    (outside / "victim.json").write_text(json.dumps({"session_id": SID, "pid": 1}))
    (home / ".claude" / "teams").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "teams" / "leak.json").write_text(json.dumps({"pid": 1}))
    assert tr._scan_team_dir_for_teammates(name) == []


@pytest.mark.parametrize("name", ["D:x", "C:..", "C:", "x:stream"])
def test_team_dir_scan_refuses_windows_drive_names(home, name):
    """R13-10: on Windows ``teams/D:x`` is the drive-relative ``D:x`` and
    ``teams/C:..`` climbs out. Refused on every platform, even where (as here)
    the name would be an ordinary folder inside the teams root."""
    d = _team_dir(home, name)
    (d / "victim.json").write_text(json.dumps({"session_id": SID, "pid": 1}))
    assert tr._team_dir(name) is None
    assert tr._scan_team_dir_for_teammates(name) == []


def test_team_dir_scan_refuses_a_link_out_of_the_teams_root(home):
    """R13-10: a name whose resolved folder is outside ~/.claude/teams."""
    outside = home / "escape"
    outside.mkdir()
    (outside / "victim.json").write_text(json.dumps({"session_id": SID, "pid": 1}))
    root = home / ".claude" / "teams"
    root.mkdir(parents=True, exist_ok=True)
    (root / "linked").symlink_to(outside, target_is_directory=True)
    assert tr._scan_team_dir_for_teammates("linked") == []
    # A plain team folder still scans.
    (_team_dir(home, "plain") / "scout.json").write_text(json.dumps({"session_id": SID2}))
    assert [r["teammate_session_id"] for r in tr._scan_team_dir_for_teammates("plain")] == [SID2]


def test_team_dir_scan_missing_dir(home):
    assert tr._scan_team_dir_for_teammates("absent") == []


def test_locate_jsonl_scans_other_project_dirs(home):
    other = settings.claude_projects_dir / "-some-other-cwd"
    other.mkdir()
    (settings.claude_projects_dir / "stray-file").write_text("")
    (other / f"{SID}.jsonl").write_text("")
    assert tr._locate_jsonl(SID, "/not/the/cwd") == other / f"{SID}.jsonl"
    assert tr._locate_jsonl(SID2, None) is None
    assert tr._locate_jsonl("", "/x") is None


def test_parse_output_shapes():
    single = tr._parse_output_for_teammates({"sessionId": SID, "teammate": "solo", "pid": 3})
    assert single == [{"teammate_name": "solo", "teammate_session_id": SID, "pid": 3, "status": None}]
    listed = tr._parse_output_for_teammates(json.dumps([{"agents": [{"teammate": "a", "id": SID}]}, 5]))
    assert listed[0]["teammate_name"] == "a" and listed[0]["teammate_session_id"] == SID
    assert tr._parse_output_for_teammates(f"see {SID} and {SID2}") == [
        {"teammate_session_id": SID, "teammate_name": None},
        {"teammate_session_id": SID2, "teammate_name": None},
    ]
    assert tr._parse_output_for_teammates({"teammates": [{}]}) == []
    assert tr._parse_output_for_teammates(None) == []


def test_extract_team_name_variants():
    assert tr._extract_team_name(None) is None
    assert tr._extract_team_name({"name": "n"}) == "n"
    assert tr._extract_team_name_from_output({"team": "d"}) == "d"
    assert tr._extract_team_name_from_output('{"team_name": "j"}') == "j"
    assert tr._extract_team_name_from_output("[1]") is None
    assert tr._extract_team_name_from_output('Created team "q"') == "q"
    assert tr._extract_team_name_from_output("nothing here") is None
    assert tr._extract_team_name_from_output(3.5) is None


# ---------------------------------------------------------------------------
# updates, kill, cleanup
# ---------------------------------------------------------------------------


async def _seed(reg, pid=4242):
    await reg.handle_team_tool_result(
        parent_session_id="p", tool_name="Task", tool_input={"team_name": "t"},
        tool_output={"teammates": [{"name": "scout", "pid": pid}]}, working_dir=None,
    )


async def test_update_teammate_ignores_unknown_fields_and_none(reg, home):
    await _seed(reg)
    rec = await reg.update_teammate("p", "t", "scout", status="completed", bogus=1, pid=None)
    assert rec.status == "completed" and rec.pid == 4242 and not hasattr(rec, "bogus")
    assert await reg.update_teammate("p", "t", "nobody", status="x") is None
    assert await reg.update_teammate("p", "nope", "scout", status="x") is None


async def test_update_callback_failure_is_swallowed(reg, home):
    async def boom(*_a):
        raise RuntimeError("callback broke")

    reg.set_update_callback(boom)
    await _seed(reg)
    assert (await reg.update_teammate("p", "t", "scout", status="error")).status == "error"


async def test_kill_teammate_paths(reg, home, monkeypatch):
    await _seed(reg)
    calls = []
    monkeypatch.setattr(tr.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    assert await reg.kill_teammate("p", "t", "scout") is True
    assert calls == [(4242, signal.SIGTERM)]
    assert (await reg.get_teammate("p", "t", "scout")).status == "killed"

    def raiser(exc):
        def _kill(pid, sig):
            raise exc
        return _kill

    # Already gone still counts as killed.
    monkeypatch.setattr(tr.os, "kill", raiser(ProcessLookupError()))
    assert await reg.kill_teammate("p", "t", "scout") is True
    monkeypatch.setattr(tr.os, "kill", raiser(PermissionError()))
    assert await reg.kill_teammate("p", "t", "scout") is False
    monkeypatch.setattr(tr.os, "kill", raiser(OSError("nope")))
    assert await reg.kill_teammate("p", "t", "scout") is False

    assert await reg.kill_teammate("p", "missing", "scout") is False
    assert await reg.kill_teammate("p", "t", "missing") is False


async def test_kill_teammate_without_pid(reg):
    await reg.handle_team_tool_use(
        parent_session_id="p", tool_name="Task", tool_input={"team": "t", "to": "x"},
        working_dir=None,
    )
    assert await reg.kill_teammate("p", "t", "x") is False


async def test_remove_team_and_session(reg, home):
    await _seed(reg)
    await reg.handle_team_tool_use(
        parent_session_id="p", tool_name="TeamCreate", tool_input={"team_name": "u"},
        working_dir=None,
    )
    await reg.remove_team("p", "t")
    assert [t["team_name"] for t in await reg.list_teams("p")] == ["u"]
    removed = await reg.remove_session("p")
    assert [t.team_name for t in removed] == ["u"]
    assert await reg.list_teams("p") == []
    # Removing the last team drops the parent entry; unknown parents are no-ops.
    await _seed(reg)
    await reg.remove_team("p", "t")
    assert "p" not in reg._teams
    await reg.remove_team("ghost", "t")
    assert await reg.remove_session("ghost") == []


def test_public_dicts():
    m = TeammateRecord(team_name="t", teammate_name="a", pid=1, usage={"in": 2})
    assert m.to_public_dict() == {
        "team_name": "t", "teammate_name": "a", "teammate_session_id": None,
        "pid": 1, "status": "running", "usage": {"in": 2},
    }
