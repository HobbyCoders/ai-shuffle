"""Seed data for a dev-loop instance, written INSIDE the app process.

Started by ``app.preview_run.seed_dev_instance`` at the startup point after
owner setup, only on a launcher-marked disposable preview (the same guard as
``initialize_preview_access``). Everything goes through the app's own service
and DB functions (the provider create/fetch-models/curate handlers, project
creation, the branch-session path, the event log), never HTTP on the owner port.

Runs at most once per data directory. The seed itself is not idempotent (it
creates chats), so ``dev_seed:<name>`` records ``started`` before any write
and ``done`` (with the result) or ``failed`` after. A later app start skips a
done seed and never re-runs a started or failed one: it reports that the data
is partial (``run down then up``) instead of piling duplicates on top. The
outcome of every start is written to ``SHUFFLE_DEV_SEED_FILE`` for the
launcher's status file.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEV_AGENT_ID = "dev-agent"
PROVIDER_NAME = "Fake AI (dev)"
MAIN_PROJECT = "dev-sandbox"
OTHER_PROJECT = "dev-other"
SECOND_BRANCH = "feature/second"
LONG_TURNS = 70  # 4 transcript rows each (prompt, text, tool card, answer) = 280


def ensure_dev_agent() -> str:
    """The seeded agent. Created before the access session so that session
    (and the picker) uses it; bound to the provider by :func:`seed`."""
    from app.db.profiles import create_profile, get_profile

    if get_profile(DEV_AGENT_ID) is None:
        create_profile(
            DEV_AGENT_ID, "Dev agent", "Seeded by the dev loop. Runs on the Fake AI provider.",
            {"system_prompt": {"type": "preset", "preset": "claude_code"}},
            if_absent=True,
        )
    return DEV_AGENT_ID


# ─── git ────────────────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> str:
    from fluid.tools.child_env import scrub_child_env

    env = {**scrub_child_env(), "GIT_AUTHOR_NAME": "Dev Seed", "GIT_AUTHOR_EMAIL": "dev@localhost",
           "GIT_COMMITTER_NAME": "Dev Seed", "GIT_COMMITTER_EMAIL": "dev@localhost",
           "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", *args],
        cwd=cwd, env=env, check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip()


def make_repository(path: Path, name: str, *, second_branch: bool) -> None:
    """A small real repository: a few commits on main, optionally a branch."""
    if (path / ".git").exists():
        return
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    files = [
        ("README.md", f"# {name}\n\nA throwaway repository seeded by the AI Shuffle dev loop.\n"),
        ("src/app.py", "def greet(name: str) -> str:\n    return f\"Hello, {name}!\"\n"),
        ("src/math_utils.py", "def add(a: int, b: int) -> int:\n    return a + b\n"),
        ("tests/test_app.py", ("from src.app import greet\n\n\ndef test_greet():\n"
                               "    assert greet('dev') == 'Hello, dev!'\n")),
    ]
    for index, (relative, content) in enumerate(files, 1):
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _git(path, "add", relative)
        _git(path, "commit", "-q", "-m", f"Add {relative} (seed commit {index})")
    if second_branch:
        _git(path, "checkout", "-q", "-b", SECOND_BRANCH)
        (path / "src/feature.py").write_text("FEATURE_FLAG = True\n", encoding="utf-8")
        _git(path, "add", "src/feature.py")
        _git(path, "commit", "-q", "-m", "Add the second-branch feature flag")
        _git(path, "checkout", "-q", "main")


# ─── transcripts ────────────────────────────────────────────────────────────


_TOOLS = [
    ("Bash", {"command": "pytest -q tests", "description": "Run the tests"},
     "1 passed in 0.02s", False),
    ("Read", {"file_path": "src/app.py"},
     "1\tdef greet(name: str) -> str:\n2\t    return f\"Hello, {name}!\"", False),
    ("Grep", {"pattern": "def ", "path": "src"},
     ("src/app.py:1:def greet(name: str) -> str:\n"
      "src/math_utils.py:1:def add(a: int, b: int) -> int:"), False),
    ("Edit", {"file_path": "src/math_utils.py", "old_string": "return a + b",
              "new_string": "return int(a) + int(b)"},
     "The file src/math_utils.py has been updated.", False),
    ("Bash", {"command": "cat missing.txt", "description": "Show a file that is not there"},
     "cat: missing.txt: No such file or directory", True),
    # The native engine's default (code mode): work tools called from Python.
    ("run_python",
     {"code": ("from tools.Bash import Bash\n"
               "log = Bash(command='git log --oneline -3')\nprint(log)")},
     ("a1b2c3d Add tests/test_app.py (seed commit 4)\n"
      "d4e5f6a Add src/math_utils.py (seed commit 3)\n"
      "0718293 Add src/app.py (seed commit 2)"), False),
]


class Transcript:
    """Turns built from the same contract events the live recorder writes."""

    def __init__(self, session_id: str, profile_id: str, model: str) -> None:
        self.session_id, self.profile_id, self.model = session_id, profile_id, model
        self.events: list[dict] = []

    def _event(self, cls: type, turn_id: str, **fields: Any) -> None:
        from app.core.turn_recorder import _build_event

        self.events.append(_build_event(cls, self.session_id, turn_id=turn_id, **fields))

    def turn(self, prompt: str, *, reply: str = "", tools: list[tuple] = (),
             answer: str = "", finished: bool = True) -> None:
        from app.contract import events as contract

        turn_id = str(uuid.uuid4())
        self._event(contract.TurnStarted, turn_id, profile_id=self.profile_id, source="user")
        self._event(contract.MessageUser, turn_id, content=prompt, profile_id=self.profile_id)
        if reply:
            self._event(contract.MessageAssistant, turn_id, content=reply, model=self.model)
        for name, tool_input, result, is_error in tools:
            call_id = "toolu_seed_" + uuid.uuid4().hex[:16]
            self._event(contract.ToolUse, turn_id, tool_use_id=call_id, name=name,
                        input=tool_input)
            if result is not None:
                self._event(contract.ToolResult, turn_id, tool_use_id=call_id,
                            is_error=is_error, content_preview=result)
        if not finished:
            return
        if answer:
            self._event(contract.MessageAssistant, turn_id, content=answer, model=self.model)
        self._event(contract.TurnCompleted, turn_id, stop_reason="end_turn", usage={
            "tokens_in": 1800, "tokens_out": 240, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "total_cost_usd": 0.0, "duration_ms": 2400,
            "num_turns": 1 + len(tools)})

    def save(self) -> int:
        from app.db.session_events import append_events

        if self.events:
            append_events(self.session_id, self.events)
        return len(self.events)


def _long_transcript(transcript: Transcript) -> None:
    for number in range(1, LONG_TURNS + 1):
        name, tool_input, result, is_error = _TOOLS[number % len(_TOOLS)]
        answer = (f"Step {number} is done. "
                  + ("The command failed as expected, so nothing changed. " if is_error else "")
                  + "Here is a short summary with `inline code` and a list:\n\n"
                  "- checked the file\n- ran the tool\n- reported back")
        if number % 10 == 0:
            answer += (f"\n\n```python\ndef step_{number}(value: int) -> int:\n"
                       f"    return value * {number}\n```")
        transcript.turn(f"Seeded request {number}: please check step {number}.",
                        reply=f"Looking at step {number} with {name}.",
                        tools=[(name, tool_input, result, is_error)], answer=answer)


# ─── provider ───────────────────────────────────────────────────────────────


async def _fake_provider(fake_url: str) -> tuple[str, str]:
    """Create, discover and curate the Fake AI row through the provider API's
    own handlers (the Settings flow: add, Refresh models, save).

    An existing row is reused only once it is curated; one left without a
    default model (cut off between create and save) is discovered and saved
    again rather than handing an empty model to the agent and chats.
    """
    from app.api import model_providers as api
    from app.db.model_providers import get_all_model_providers

    existing = [row for row in get_all_model_providers() if row.get("name") == PROVIDER_NAME]
    if existing and existing[0].get("default_model"):
        return existing[0]["id"], existing[0]["default_model"]
    if existing:
        provider_id = existing[0]["id"]
    else:
        provider_id = (await api.create_provider(api.ModelProviderCreate(
            name=PROVIDER_NAME, provider_type="custom", api_dialect="openai", base_url=fake_url,
            auth_token="dev-fake-key-not-secret",
        ), token="dev-seed")).id
    fetched = await api._fetch_models(provider_id)
    if not fetched.models:
        raise RuntimeError(f"Fake AI at {fake_url} listed no models")
    model = fetched.models[0]["id"]
    await api.update_provider(provider_id, api.ModelProviderUpdate(
        models=fetched.models, default_model=model, small_fast_model=model,
    ), token="dev-seed")
    return provider_id, model


# ─── chats ──────────────────────────────────────────────────────────────────


def _project(project_id: str) -> tuple[str | None, str | None]:
    from app.api.common import require_origin_access

    access = require_origin_access(api_user=None, project_id=project_id)
    return (access.get("legacy") or {}).get("id"), (access.get("project_v2") or {}).get("id")


def _chat(title: str, composite: str, project_id: str | None = None) -> str:
    from app.db import database

    session_id = str(uuid.uuid4())
    legacy, v2 = _project(project_id) if project_id else (None, None)
    database.create_session(session_id=session_id, profile_id=DEV_AGENT_ID, project_id=legacy,
                            project_v2_id=v2, title=title, is_active=True, model=composite)
    return session_id


async def seed_default() -> dict:
    from app.api.projects import create_project_record
    from app.api.sessions_helpers import CreateWorktreeSessionRequest
    from app.api.sessions_worktree import _create_v2_branch_session
    from app.core.config import settings
    from app.core.models import ProjectCreate
    from app.db import database
    from app.db.async_database import run_sync
    from app.db.profiles import get_profile, update_profile

    fake_url = os.environ.get("SHUFFLE_DEV_FAKE_AI_URL")
    if not fake_url:
        raise RuntimeError("SHUFFLE_DEV_FAKE_AI_URL is not set")
    provider_id, model = await _fake_provider(fake_url)
    composite = f"prov:{provider_id}:{model}"

    ensure_dev_agent()
    config = dict((get_profile(DEV_AGENT_ID) or {}).get("config") or {})
    config.update(provider=provider_id, model=composite)
    update_profile(DEV_AGENT_ID, config=config)

    workspace = Path(settings.effective_workspace_dir).resolve()
    projects = {}
    for project_id, name, branch in ((MAIN_PROJECT, "Dev sandbox", True),
                                     (OTHER_PROJECT, "Dev other", False)):
        await run_sync(make_repository, workspace / project_id, name, second_branch=branch)
        if database.get_project(project_id) is None:
            await create_project_record(ProjectCreate(id=project_id, name=name))
        projects[project_id] = str(workspace / project_id)

    chats: dict[str, str] = {}

    def transcript(key: str, title: str, project: str | None = None) -> Transcript:
        chats[key] = _chat(title, composite, project)
        return Transcript(chats[key], DEV_AGENT_ID, model)

    long = transcript("long", "Long transcript (seeded)", MAIN_PROJECT)
    _long_transcript(long)
    long_events = await run_sync(long.save)

    running = transcript("running", "Tool call in progress (seeded)", MAIN_PROJECT)
    running.turn("Run the tests please.", reply="Running them now.",
                 tools=[_TOOLS[0][:3] + (False,)], answer="All tests pass.")
    running.turn("Now run the slow integration suite.", reply="Starting the slow suite.",
                 tools=[("Bash", {"command": "sleep 600 && pytest -q integration",
                                  "description": "Run the slow integration suite"}, None, False)],
                 finished=False)
    await run_sync(running.save)

    cleared = transcript("cleared", "Cleared chat (seeded)", MAIN_PROJECT)
    cleared.turn("This conversation gets cleared.", answer="Understood.")
    await run_sync(cleared.save)
    await run_sync(database.clear_session_conversation, chats["cleared"])

    other = transcript("other_project", "Other project chat (seeded)", OTHER_PROJECT)
    other.turn("What is in this repository?", reply="Let me look.",
               tools=[("Bash", {"command": "ls", "description": "List files"},
                       "README.md\nsrc\ntests", False)],
               answer="A README, a `src` package and a `tests` folder.")
    await run_sync(other.save)

    loose = transcript("no_project", "Quick question (seeded)")
    loose.turn("What is 2 + 2?", answer="4.")
    await run_sync(loose.save)

    # The composer's "new branch session" path: the deterministic checkout of
    # an existing branch under the project's .fluid/worktrees.
    _legacy, v2 = await run_sync(_project, MAIN_PROJECT)
    created = await _create_v2_branch_session(
        CreateWorktreeSessionRequest(project_id=MAIN_PROJECT, branch_name=SECOND_BRANCH,
                                     base_branch="main", profile_id=DEV_AGENT_ID,
                                     create_new_branch=False, overrides={"model": composite}),
        await run_sync(database.get_project, MAIN_PROJECT), None,
        await run_sync(_project_v2_row, v2),
    )
    chats["worktree"] = created.session_id
    await run_sync(database.update_session, created.session_id, title="Worktree chat (seeded)")
    worktree = Transcript(created.session_id, DEV_AGENT_ID, model)
    worktree.turn("Which branch is this?", reply="Checking.",
                  tools=[("Bash", {"command": "git branch --show-current",
                                   "description": "Show the branch"}, SECOND_BRANCH, False)],
                  answer=f"This chat works on `{SECOND_BRANCH}` in its own checkout.")
    await run_sync(worktree.save)

    return {
        "provider_id": provider_id, "model": model, "agent_id": DEV_AGENT_ID,
        "projects": projects, "worktree_path": created.worktree_path, "chats": chats,
        "long_chat_events": long_events,
    }


def _project_v2_row(project_v2_id: str | None) -> dict | None:
    if not project_v2_id:
        return None
    from app.db import projects_v2

    return projects_v2.get_project_v2(project_v2_id)


SEEDS = {"default": seed_default}


def _write_result(result: dict) -> None:
    path = os.environ.get("SHUFFLE_DEV_SEED_FILE")
    if path:
        Path(path).write_text(json.dumps(result, indent=2), encoding="utf-8")


#: ``seed.json`` error for a seed an earlier start began but never finished.
PARTIAL_SEED = ("partial seed from an interrupted start; run `down` then `up` for a fresh "
                "instance")


def _record(name: str) -> dict | None:
    from app.db import database

    raw = database.get_system_setting(f"dev_seed:{name}")
    if not raw:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        record = None
    return record if isinstance(record, dict) else {"state": "started"}


def _save_record(name: str, record: dict) -> None:
    from app.db import database

    database.set_system_setting(f"dev_seed:{name}", json.dumps(record))


async def seed(name: str) -> None:
    """Run seed ``name`` at most once per data directory; record the outcome."""
    from app.db.async_database import run_sync

    if name not in SEEDS:
        _write_result({"ok": False, "seed": name,
                       "error": f"unknown seed {name!r}; known: {', '.join(sorted(SEEDS))}"})
        logger.error("Unknown dev seed %r", name)
        return
    record = await run_sync(_record, name)
    if record is not None:
        state = record.get("state")
        if state == "done":
            result = record.get("result") or {}
            _write_result({"ok": True, "seed": name, "reused": True, **result})
            logger.info("Dev seed %r already applied to this data directory", name)
            return
        error = PARTIAL_SEED
        if state == "failed":
            error = (f"the seed failed on an earlier start ({record.get('error')}); run `down` "
                     "then `up` for a fresh instance")
        _write_result({"ok": False, "seed": name, "error": error})
        logger.error("Dev seed %r not re-run: %s", name, error)
        return
    # Before any write: a start cut off past here leaves "started" behind,
    # which the next start reports instead of seeding a second copy.
    await run_sync(_save_record, name, {"state": "started"})
    try:
        result = await SEEDS[name]()
    except Exception as exc:
        logger.exception("Dev seed %r failed", name)
        error = f"{type(exc).__name__}: {exc}"
        await run_sync(_save_record, name, {"state": "failed", "error": error})
        _write_result({"ok": False, "seed": name, "error": error})
        return
    await run_sync(_save_record, name, {"state": "done", "result": result})
    _write_result({"ok": True, "seed": name, **result})
    logger.info("Dev seed %r applied: %s", name, json.dumps(result))
