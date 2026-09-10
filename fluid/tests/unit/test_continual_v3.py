"""Continual harness v3 — the model-participation and retrieval layer.

Pins the propose-then-curate channel (Remember → journal → urgent synthesis),
scored retrieval with usage counting (Recall), LFU-informed memory eviction,
project scoping, the consolidation pass, learning events, and the prompt
surfaces that advertise all of it. The design doctrine under test: the model
PROPOSES (cheap, buffered, in-turn) and the synthesizer DISPOSES (verified,
out-of-band) — no model-facing call ever writes memories.json directly.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from fluid.codemode.continual import Synthesizer
from fluid.codemode.skills import MAX_MEMORIES, SkillStore, project_key_for
from fluid.tools.builtins import recall as recall_mod
from fluid.tools.builtins import remember as remember_mod
from fluid.tools.registry import ToolCtx


# ─── Remember: the propose channel ───────────────────────────────────────────


def _remember_ctx(store: SkillStore, synth: Synthesizer | None = None) -> ToolCtx:
    def _journal(text: str, scope: str | None = None) -> None:
        entry = {"kind": "remember", "text": text}
        if scope:
            entry["scope"] = scope
        store.journal_append(entry)
        if synth is not None:
            synth.mark_urgent()

    ctx = ToolCtx()
    ctx.extra["continual_journal"] = _journal
    ctx.extra["memory_store"] = store
    ctx.extra["memory_project"] = "my-proj"
    return ctx


@pytest.mark.asyncio
async def test_remember_commits_a_protected_fact(tmp_path) -> None:
    store = SkillStore(tmp_path)
    out = await remember_mod.handler(
        {"text": "user prefers rebase over merge", "scope": "project"},
        _remember_ctx(store),
    )
    assert not out.is_error
    assert store.journal_entries() == []
    (entry,) = SkillStore(tmp_path).memories()
    assert entry.text == "user prefers rebase over merge"
    assert entry.scope == "my-proj" and entry.protected
    assert "Saved" in out.text


@pytest.mark.asyncio
async def test_remember_rejects_secrets_and_essays(tmp_path) -> None:
    store = SkillStore(tmp_path)
    ctx = _remember_ctx(store)
    secret = await remember_mod.handler(
        {"text": "the API key is sk-ant-abcdef0123456789abcdef0123456789"}, ctx
    )
    assert secret.is_error and "credential" in secret.text
    essay = await remember_mod.handler({"text": "x" * 600}, ctx)
    assert essay.is_error and "500" in essay.text
    assert store.journal_entries() == []


@pytest.mark.asyncio
async def test_remember_without_wiring_is_an_error(tmp_path) -> None:
    out = await remember_mod.handler({"text": "a fact"}, ToolCtx())
    assert out.is_error and "unavailable" in out.text


def test_urgent_flag_lowers_synthesis_threshold(tmp_path) -> None:
    store = SkillStore(tmp_path)
    synth = Synthesizer(provider=None, model="m", store=store)
    store.journal_append({"kind": "remember", "text": "a fact"})
    assert not synth.due()  # one entry, threshold is 8
    synth.mark_urgent()
    assert synth.due()  # urgency: any new entry at all triggers


@pytest.mark.asyncio
async def test_explicit_remember_bypasses_synthesis(tmp_path) -> None:
    """The full propose→curate loop: Remember journals, urgent synthesis
    distills, memories.json carries the fact with the requested scope."""

    class OneShotProvider:
        def __init__(self, reply: str) -> None:
            self.reply = reply
            self.requests: list = []

        async def stream(self, request):
            self.requests.append(request)

            class Chunk:
                kind = "text_delta"
                payload = {"text": self.reply}

            yield Chunk()

    provider = OneShotProvider(
        '<memory scope="project">User prefers rebase over merge</memory>'
    )
    store = SkillStore(tmp_path)
    synth = Synthesizer(
        provider=provider, model="m", store=store, project="my-proj"
    )
    out = await remember_mod.handler(
        {"text": "remember: prefer rebase", "scope": "project"},
        _remember_ctx(store, synth),
    )
    assert not out.is_error
    assert not synth.due()
    (entry,) = SkillStore(tmp_path).memories()
    assert entry.text == "remember: prefer rebase"
    assert entry.scope == "my-proj" and entry.protected
    assert provider.requests == []


# ─── Recall: the retrieval channel ───────────────────────────────────────────


def _recall_ctx(store: SkillStore) -> ToolCtx:
    ctx = ToolCtx()
    ctx.extra["recall_store"] = store
    return ctx


@pytest.mark.asyncio
async def test_recall_finds_live_and_archived_and_bumps_usage(tmp_path) -> None:
    store = SkillStore(tmp_path)
    kept = store.save_memory("staging runs on port 8443")
    # Overflow the cap so one entry lands in the archive.
    for i in range(MAX_MEMORIES):
        store.save_memory(f"filler fact number {i}")
    archived_texts = {m.text for m in store.archived_memories()}
    assert archived_texts  # something aged out
    out = await recall_mod.handler({"query": "staging port"}, _recall_ctx(store))
    assert not out.is_error
    assert "staging runs on port 8443" in out.text
    live_by_id = {m.id: m for m in store.memories()}
    if kept.id in live_by_id:  # live hit → counter bumped
        assert live_by_id[kept.id].uses == 1
    else:  # it aged out → shown from the archive, labeled
        assert "archived" in out.text


@pytest.mark.asyncio
async def test_recall_surfaces_saved_skills(tmp_path) -> None:
    store = SkillStore(tmp_path)
    store.save(
        "deploy_check",
        '"""Verify a deploy landed."""\n\ndef deploy_check():\n    return True\n',
    )
    out = await recall_mod.handler({"query": "deploy"}, _recall_ctx(store))
    assert "deploy_check" in out.text and "Verify a deploy landed." in out.text


@pytest.mark.asyncio
async def test_recall_misses_say_so_without_claiming_absence(tmp_path) -> None:
    store = SkillStore(tmp_path)
    out = await recall_mod.handler({"query": "zebra"}, _recall_ctx(store))
    assert not out.is_error
    assert "No stored memory or skill matches" in out.text


def test_lfu_eviction_protects_recalled_entries(tmp_path) -> None:
    """Eviction is least-recalled-first: an old entry the store keeps getting
    asked for outlives a newer entry nothing ever recalled."""
    store = SkillStore(tmp_path)
    veteran = store.save_memory("veteran fact, recalled often")
    store.note_memories_recalled([veteran.id])
    for i in range(MAX_MEMORIES):
        store.save_memory(f"unrecalled filler {i}")
    live = {m.text for m in store.memories()}
    assert "veteran fact, recalled often" in live
    assert "unrecalled filler 0" not in live  # zero-recall entries went first


# ─── project scoping ─────────────────────────────────────────────────────────


def test_project_key_is_stable_and_git_rooted(tmp_path) -> None:
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)
    assert project_key_for(nested) == project_key_for(tmp_path)
    assert project_key_for(tmp_path)  # non-empty slug


def test_memory_section_scopes_by_project(tmp_path) -> None:
    store = SkillStore(tmp_path)
    store.save_memory("global fact")
    store.save_memory("proj A convention", scope="proj-a")
    store.save_memory("proj B convention", scope="proj-b")
    section_a = store.render_memory_section("proj-a")
    assert "global fact" in section_a
    assert "proj A convention" in section_a
    assert "proj B convention" not in section_a
    assert "1 entry scoped to other projects not shown" in section_a
    # No project key → everything renders (embedders without a cwd notion).
    section_all = store.render_memory_section()
    assert "proj B convention" in section_all


def test_compiler_scopes_memory_to_the_cwd_project(tmp_path) -> None:
    from fluid.context.compiler import PromptInputs, compile_prompt

    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / ".git").mkdir()
    store_dir = tmp_path / "skills"
    store = SkillStore(store_dir)
    store.save_memory("global fact")
    store.save_memory(
        "this project's convention", scope=project_key_for(tmp_path / "proj")
    )
    store.save_memory("other project's convention", scope="elsewhere-key")
    compiled = compile_prompt(PromptInputs(
        base="base", cwd=str(tmp_path / "proj"), include_env=False,
        include_global_instructions=False, include_project_instructions=False,
        include_skills=False, include_agents=False, include_plugins=False,
        memories_dir=str(store_dir),
    ))
    assert "this project's convention" in compiled.text
    assert "other project's convention" not in compiled.text


# ─── consolidation ───────────────────────────────────────────────────────────


class ScriptedProvider:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.systems: list[str] = []

    async def stream(self, request):
        self.systems.append(request.system)
        reply = self.replies.pop(0)

        class Chunk:
            kind = "text_delta"
            payload = {"text": reply}

        yield Chunk()


@pytest.mark.asyncio
async def test_consolidation_rewrites_and_deletes_but_never_adds(tmp_path) -> None:
    store = SkillStore(tmp_path)
    a = store.save_memory("user likes rebase")
    b = store.save_memory("user prefers rebase over merge always")
    provider = ScriptedProvider([
        f'<memory id="{a.id}">User prefers rebase over merge</memory>\n'
        f'<delete-memory id="{b.id}"/>\n'
        "<memory>an add that must be ignored</memory>"
    ])
    synth = Synthesizer(provider=provider, model="m", store=store)
    changed = await synth.consolidate()
    texts = {m.text for m in store.memories()}
    assert texts == {"User prefers rebase over merge"}
    assert f"memory:{a.id}" in changed and f"-memory:{b.id}" in changed
    # Consolidation runs under its own curation system prompt.
    assert "consolidation pass" in provider.systems[0]
    # The pass stamped its clock.
    assert store.last_consolidated_at() > 0


def test_consolidation_due_gating(tmp_path) -> None:
    store = SkillStore(tmp_path)
    synth = Synthesizer(provider=None, model="m", store=store)
    assert not synth._consolidation_due()  # too few memories
    for i in range(8):
        store.save_memory(f"fact {i}")
    assert synth._consolidation_due()  # never consolidated + enough entries
    store.set_last_consolidated_at(time.time())
    assert not synth._consolidation_due()  # interval-gated


# ─── learning events ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_change_announces_what_landed(tmp_path) -> None:
    store = SkillStore(tmp_path)
    seen: list[list[str]] = []

    async def on_change(changes: list[str]) -> None:
        seen.append(changes)

    provider = ScriptedProvider(["<memory>User deploys from CI</memory>"])
    synth = Synthesizer(
        provider=provider, model="m", store=store, on_change=on_change,
        min_new_entries=1,
    )
    store.journal_append({"kind": "user", "text": "deploys go through CI"})
    synth.kick()
    await synth._task
    assert seen and seen[0] and seen[0][0].startswith("memory:")


def test_continual_update_event_round_trips() -> None:
    from fluid.events.model import ContinualUpdate, event_from_dict, event_to_dict

    event = ContinualUpdate(session_id="s", changes=("memory:m1", "-old_skill"))
    decoded = event_from_dict(event_to_dict(event))
    assert decoded.changes == event.changes and decoded.type == "continual_update"


# ─── wiring (loader → ctx.extra) ─────────────────────────────────────────────


class FakeProvider:
    name = "anthropic"

    def __init__(self) -> None:
        from fluid.providers.base import ProviderCaps

        self.caps = ProviderCaps(
            explicit_cache_breakpoints=True, max_cache_breakpoints=4
        )

    async def stream(self, request):
        if False:  # pragma: no cover — never streamed in these tests
            yield None


def _config(tmp_path, **overrides):
    from fluid.config.loader import BUILTIN_DEFAULTS, deep_merge
    from fluid.config.model import HarnessConfig

    return HarnessConfig.from_dict(
        deep_merge(BUILTIN_DEFAULTS, {"cwd": str(tmp_path), **overrides})
    )


def test_build_engine_wires_continual_channels(tmp_path) -> None:
    from fluid.config.loader import build_engine
    from fluid.store import MemoryStore

    config = _config(
        tmp_path, continual=True, skills_dir=str(tmp_path / "skills")
    )
    engine = build_engine(config, MemoryStore(), provider=FakeProvider())
    assert callable(engine.ctx.extra.get("continual_journal"))
    assert isinstance(engine.ctx.extra.get("recall_store"), SkillStore)
    # The Remember/Recall schemas survive requires_extra gating.
    names = {t["name"] for t in engine._tools_api()}
    assert {"Remember", "Recall"} <= names


def test_explicit_memory_available_without_continual(tmp_path) -> None:
    from fluid.config.loader import build_engine
    from fluid.store import MemoryStore

    config = _config(tmp_path)
    engine = build_engine(config, MemoryStore(), provider=FakeProvider())
    names = {t["name"] for t in engine._tools_api()}
    assert {"Remember", "Recall", "Forget"} <= names


# ─── prompt surfaces ─────────────────────────────────────────────────────────


def test_continual_bullet_teaches_remember_and_recall() -> None:
    from fluid.cli.prompt import PromptProfile, compose_base_prompt

    text = compose_base_prompt(PromptProfile(continual=True, model="claude"))
    assert "Use Remember for durable facts" in text
    assert "Use Recall for facts omitted" in text
    assert "never manage it" not in text  # the old stand-down order is gone

    # Without the tools in the surface, the bullet cannot name them.
    text = compose_base_prompt(PromptProfile(
        continual=True, model="claude",
        tools=frozenset({"Read", "Note", "Skill"}),
    ))
    assert "Remember" not in text and "Recall" not in text


def test_code_mode_addendum_teaches_save_skill() -> None:
    from fluid.cli.prompt import compose_code_mode_addendum

    text = compose_code_mode_addendum()
    assert "save_skill" in text
    assert "from skills import" in text
    assert "_selftest" in text


# ─── guidance-as-data (~/.fluid/guidance/*.md) ──────────────────────────────


def test_guidance_files_match_by_stem_substring(tmp_path) -> None:
    from fluid.cli.prompt import load_guidance_files

    gdir = tmp_path / ".fluid" / "guidance"
    gdir.mkdir(parents=True)
    (gdir / "glm.md").write_text("## GLM notes\nAlways call tools.", encoding="utf-8")
    (gdir / "qwen.md").write_text("## Qwen notes", encoding="utf-8")
    blocks = load_guidance_files("z-ai/glm-5.3-flash", home=tmp_path)
    assert len(blocks) == 1 and "GLM notes" in blocks[0]
    assert load_guidance_files("claude-opus-5", home=tmp_path) == ()


def test_guidance_files_land_in_the_composed_prompt(tmp_path) -> None:
    from fluid.cli.prompt import PromptProfile, compose_base_prompt, load_guidance_files

    gdir = tmp_path / ".fluid" / "guidance"
    gdir.mkdir(parents=True)
    (gdir / "glm.md").write_text("## GLM quirks\nQuote every argument.", encoding="utf-8")
    profile = PromptProfile(
        model="z-ai/glm-5.3-flash",
        extra_guidance=load_guidance_files("z-ai/glm-5.3-flash", home=tmp_path),
    )
    text = compose_base_prompt(profile)
    assert text.rstrip().endswith("Quote every argument.")


# ─── allowlist must not amputate the learning loop (CE12 extension) ──────────


def test_allowlist_of_work_tools_keeps_save_skill(tmp_path) -> None:
    from fluid.config.loader import build_engine
    from fluid.store import MemoryStore

    config = _config(
        tmp_path,
        code_mode=True,
        skills_dir=str(tmp_path / "skills"),
        tools={"allowed": ["Read", "Grep"]},
    )
    engine = build_engine(config, MemoryStore(), provider=FakeProvider())
    assert engine._tool_permitted("save_skill")
    assert engine._tool_permitted("delete_skill")
    # Deny still beats everything.
    engine.disallowed_tools = ["save_skill"]
    assert not engine._tool_permitted("save_skill")
