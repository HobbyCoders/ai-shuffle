"""SessionSearch answers about the project you are in, not every project.

The audit's P11: the session store is one flat global directory, so recall in
one repo happily surfaced another repo's internals. Sessions now record the
project root they ran in, and SessionSearch scopes to it by default.

The rule that matters most is the one for sessions stored *before* the field
existed: they have no project, and dropping them would quietly retire every
memory older than this change. They stay in the results and rank after
confirmed local hits — recall gets narrower, never shorter.
"""

from pathlib import Path

from fluid.messages import Message, Text
from fluid.store.base import (
    MemoryStore,
    SessionNode,
    SessionSummary,
    project_root_for,
    same_project,
)
from fluid.store.jsonl import JsonlStore
from fluid.tools.builtins import register_builtins
from fluid.tools.registry import ToolCtx, ToolRegistry


def _repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    return root


async def _seed(
    store: JsonlStore, texts: list[tuple[str, str]], cwd: str | None
) -> str:
    meta = await store.create_session(cwd=cwd)
    parent = None
    for i, (role, text) in enumerate(texts):
        node = SessionNode(
            node_id=f"node_{meta.session_id}_{i}",
            parent_id=parent,
            message=Message(role=role, content=[Text(text=text)]),
        )
        await store.append(meta.session_id, node)
        parent = node.node_id
    return meta.session_id


def _tool_ctx(store: JsonlStore, cwd: Path, session_id: str = "sess_cur") -> ToolCtx:
    return ToolCtx(cwd=str(cwd), session_id=session_id, extra={
        "session_search": store.search,
        "session_recent": store.recent_sessions,
    })


async def _search(store: JsonlStore, cwd: Path, **input):
    registry = ToolRegistry()
    register_builtins(registry)
    return await registry.dispatch("SessionSearch", input, _tool_ctx(store, cwd))


# ─── project identity ────────────────────────────────────────────────────


def test_project_root_is_the_enclosing_repo(tmp_path) -> None:
    repo = _repo(tmp_path, "alpha")
    nested = repo / "src" / "deep"
    nested.mkdir(parents=True)
    assert project_root_for(nested) == str(repo)
    # No repo anywhere above: the directory is its own project.
    plain = tmp_path / "loose"
    plain.mkdir()
    assert project_root_for(plain) == str(plain.resolve())
    assert project_root_for(None) is None


def test_a_linked_worktree_resolves_to_its_repository(tmp_path) -> None:
    repo = _repo(tmp_path, "alpha")
    worktree = tmp_path / "wt" / "feature"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(
        f"gitdir: {repo / '.git' / 'worktrees' / 'feature'}\n", encoding="utf-8"
    )
    assert project_root_for(worktree) == str(repo)
    # A .git file pointing somewhere that is not a worktree (a submodule) is
    # left alone rather than mis-resolved.
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / ".git").write_text("gitdir: ../.git/modules/sub\n", encoding="utf-8")
    assert project_root_for(sub) == str(sub.resolve())


def test_same_project_needs_two_known_roots(tmp_path) -> None:
    assert same_project(str(tmp_path), str(tmp_path))
    assert not same_project(None, str(tmp_path))
    assert not same_project(str(tmp_path), None)


# ─── the store records it ────────────────────────────────────────────────


async def test_stores_record_the_project_root(tmp_path) -> None:
    repo = _repo(tmp_path, "alpha")
    store = JsonlStore(tmp_path / "store")
    meta = await store.create_session(cwd=str(repo / "src"))
    assert meta.project_root == str(repo)

    # It survives a reload from disk, and an omitted cwd stays legacy-shaped.
    reloaded = JsonlStore(tmp_path / "store")
    assert (await reloaded.get_session(meta.session_id)).project_root == str(repo)
    assert (await store.create_session()).project_root is None
    assert (await MemoryStore().create_session(cwd=str(repo))).project_root == str(repo)


# ─── SessionSearch scoping ───────────────────────────────────────────────


async def test_in_project_hits_rank_ahead_of_legacy_ones(tmp_path) -> None:
    here, elsewhere = _repo(tmp_path, "here"), _repo(tmp_path, "elsewhere")
    store = JsonlStore(tmp_path / "store")
    # The legacy session scores higher, so only scoping can put the local one
    # on top.
    legacy = await _seed(store, [("user", "widget widget widget")], None)
    local = await _seed(store, [("user", "widget once")], str(here))
    await _seed(store, [("user", "widget widget widget widget")], str(elsewhere))

    outcome = await _search(store, here, query="widget")

    assert not outcome.is_error
    assert outcome.text.index(local) < outcome.text.index(legacy)
    assert "1 session in other projects hidden" in outcome.text


async def test_cross_project_sessions_are_excluded_by_default(tmp_path) -> None:
    here, elsewhere = _repo(tmp_path, "here"), _repo(tmp_path, "elsewhere")
    store = JsonlStore(tmp_path / "store")
    other = await _seed(store, [("user", "internals of the other app")], str(elsewhere))
    await _seed(store, [("user", "nothing relevant here")], str(here))

    outcome = await _search(store, here, query="internals")

    assert other not in outcome.text
    assert "no stored session matched" in outcome.text
    assert 'scope="all"' in outcome.text


async def test_scope_all_returns_every_project(tmp_path) -> None:
    here, elsewhere = _repo(tmp_path, "here"), _repo(tmp_path, "elsewhere")
    store = JsonlStore(tmp_path / "store")
    other = await _seed(store, [("user", "internals of the other app")], str(elsewhere))
    legacy = await _seed(store, [("user", "internals from long ago")], None)
    local = await _seed(store, [("user", "internals of this app")], str(here))

    outcome = await _search(store, here, query="internals", scope="all", limit=10)

    for sid in (other, legacy, local):
        assert sid in outcome.text
    assert "hidden" not in outcome.text


async def test_recent_listing_is_scoped_too(tmp_path) -> None:
    here, elsewhere = _repo(tmp_path, "here"), _repo(tmp_path, "elsewhere")
    store = JsonlStore(tmp_path / "store")
    local = await _seed(store, [("user", "local work"), ("assistant", "done")], str(here))
    legacy = await _seed(store, [("user", "old work")], None)
    # Newest, and from another project — without scoping it would lead.
    other = await _seed(store, [("user", "other work")], str(elsewhere))

    scoped = await _search(store, here)
    assert other not in scoped.text
    assert local in scoped.text and legacy in scoped.text
    assert scoped.text.index(local) < scoped.text.index(legacy)

    everything = await _search(store, here, scope="all", limit=10)
    assert other in everything.text


async def test_recent_listing_reaches_past_other_projects(tmp_path) -> None:
    """The backend's limit applies before scoping, so the tool over-fetches."""
    here, elsewhere = _repo(tmp_path, "here"), _repo(tmp_path, "elsewhere")
    store = JsonlStore(tmp_path / "store")
    local = await _seed(store, [("user", "the local one")], str(here))
    for i in range(6):  # six newer sessions, all from another project
        await _seed(store, [("user", f"other {i}")], str(elsewhere))

    outcome = await _search(store, here, limit=2)
    assert local in outcome.text


async def test_backends_without_project_roots_still_work(tmp_path) -> None:
    """A summary with no project_root at all is legacy, not "elsewhere"."""
    here = _repo(tmp_path, "here")
    rows = [SessionSummary(session_id="ses_x", title="old", last_ts=1.0, preview="hi")]
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = ToolCtx(cwd=str(here), extra={
        "session_search": lambda *a, **k: [],
        "session_recent": lambda **k: rows,
    })
    outcome = await registry.dispatch("SessionSearch", {}, ctx)
    assert "ses_x" in outcome.text
    assert "hidden" not in outcome.text


def test_scope_is_advertised_in_the_description() -> None:
    from fluid.tools.builtins.session_search import SPEC, describe

    assert SPEC.input_schema["properties"]["scope"]["enum"] == ["project", "all"]
    text = describe()
    assert "Scoped to the current project by default" in text
    assert "listed after the current project's hits" in text
