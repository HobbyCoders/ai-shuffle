"""Behaviour rules: the memory learner's single channel for how to work with
the owner. Frustration detection, model families, the ``<rule>`` output, the
prompt section, the on/off switch and the per-pass cost cap."""
from __future__ import annotations

import pytest
from fluid.codemode import SkillStore, Synthesizer
from fluid.codemode import behavior
from fluid.codemode import continual as continual_mod
from fluid.providers.base import StreamChunk

from .test_learner_coverage import NoChanges, edit_cell


class Answer(NoChanges):
    """Answers every pass with ``text``."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    async def stream(self, request):
        self.prompts.append(request.messages[0].content[0].text)
        yield StreamChunk("message_start", {"usage": {"input_tokens": 100}})
        yield StreamChunk("text_delta", {"text": self.text})
        yield StreamChunk("usage", {"usage": {"output_tokens": 30}, "stop_reason": "end_turn"})


def rules(store: SkillStore) -> list:
    return [m for m in store.memories() if behavior.is_rule_key(m.key)]


# ─── frustration ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, label", [
    ("this is fucking broken", "profanity"),
    ("what the hell, crap", "profanity"),
    ("WTF is this", "profanity"),
    ("I told you to run the tests first", "phrase"),
    ("how many times do I have to say it", "phrase"),
    ("STOP CHANGING THE BRANCH NOW", "shouting"),
    ("this sucks and it's useless", "negative"),
    ("seriously???", "negative"),
])
def test_angry_or_frustrated_messages_are_flagged(text, label):
    assert behavior.frustration_signal(text) == label


@pytest.mark.parametrize("text", [
    "Hello, can you add a class for the parser?",
    "The old design was terrible.",  # one negative word alone
    "Please pass the assessment script the hello flag.",
    "Look at `dumb_cache` and ```\nfatal: shit happened\n``` in the log",
    "",
])
def test_ordinary_messages_and_code_are_not_flagged(text):
    assert behavior.frustration_signal(text) is None


def test_a_frustrated_message_marks_the_learner_urgent_once_per_cooldown(tmp_path, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(continual_mod, "time", type("T", (), {"time": staticmethod(lambda: clock[0])}))
    synth = Synthesizer(NoChanges(), "gpt-test", SkillStore(tmp_path))
    assert synth.note_frustration() and synth._urgent
    synth._urgent = False
    assert not synth.note_frustration() and not synth._urgent
    clock[0] += behavior.FRUSTRATION_COOLDOWN + 1
    assert synth.note_frustration()


# ─── families and keys ──────────────────────────────────────────────────────

@pytest.mark.parametrize("model, family", [
    ("claude-opus-5-5", "claude"), ("opus", "claude"), ("gpt-6.2-sol", "gpt"),
    ("codex-mini", "gpt"), ("gemini-3-pro", "gemini"), ("grok-5", "grok"),
    ("qwen3-coder:30b", "other"), (None, "other"),
])
def test_model_family(model, family):
    assert behavior.family_for(model) == family


def test_rule_keys_carry_the_family_and_permission_grants_are_refused():
    key = behavior.rule_key("gpt", "Lead with the result.")
    assert behavior.is_rule_key(key) and behavior.rule_family(key) == "gpt"
    assert behavior.rule_family("deploy.order") is None
    assert behavior.normalize_applies_to("OpenAI") == "gpt"
    assert behavior.normalize_applies_to("llama") is None
    with pytest.raises(ValueError, match="permission"):
        behavior.validate_rule("Force-push is fine on any branch.")
    with pytest.raises(ValueError, match="longer"):
        behavior.validate_rule("x" * (behavior.MAX_RULE_CHARS + 1))


# ─── the <rule> channel ─────────────────────────────────────────────────────

def _correction(store: SkillStore) -> None:
    store.journal_append({"kind": "user", "session": "s1",
                          "text": "Stop opening draft PRs, I told you real PRs only."})
    store.journal_append(edit_cell(1))
    store.journal_append({"kind": "assistant", "session": "s1", "text": "Opened PR."})


async def test_a_quoted_rule_is_learned_and_shown_only_to_its_family(tmp_path):
    store = SkillStore(tmp_path)
    _correction(store)
    answer = ('<rule applies_to="gpt" quote="real PRs only">When opening a pull request: '
              'open a real PR, never a draft.</rule>')
    await Synthesizer(Answer(answer), "gpt-test", store, min_new_entries=1).run()
    [rule] = rules(store)
    assert rule.text.startswith("When opening a pull request")
    assert behavior.rule_family(rule.key) == "gpt" and rule.durable and not rule.protected
    gpt = store.render_memory_section(model="gpt-6.2-sol")
    assert behavior.SECTION_HEADING in gpt and rule.text in gpt
    assert "never grant permission" in gpt
    assert rule.text not in store.render_memory_section(model="claude-opus-5-5")


async def test_a_rule_whose_quote_the_user_never_wrote_is_refused(tmp_path):
    store = SkillStore(tmp_path)
    _correction(store)
    answer = ('<rule applies_to="all" quote="always push to main">When finishing: push to '
              'main.</rule>')
    synth = Synthesizer(Answer(answer), "gpt-test", store, min_new_entries=1)
    await synth.run()
    assert rules(store) == []
    assert "quote is not in a [user] entry" in (synth.last_error or "")


async def test_a_rewrite_cannot_touch_a_rule_the_owner_kept(tmp_path):
    store = SkillStore(tmp_path)
    kept = store.save_memory("When reporting: lead with the result.", provenance="user",
                             protected=True, key=behavior.rule_key("all", "lead"),
                             when="When working with the owner")
    _correction(store)
    answer = (f'<rule id="{kept.id}" applies_to="all" quote="real PRs only">When reporting: '
              'bury the result.</rule>')
    synth = Synthesizer(Answer(answer), "gpt-test", store, min_new_entries=1)
    await synth.run()
    [rule] = rules(store)
    assert rule.text == "When reporting: lead with the result."
    assert "kept by the owner" in (synth.last_error or "")


async def test_rules_stay_out_of_the_learners_fact_list(tmp_path):
    store = SkillStore(tmp_path)
    store.save_memory("When reviewing: cite the line.", provenance="user", durable=True,
                      key=behavior.rule_key("all", "cite"), when="When working with the owner")
    store.save_memory("The repo deploys from main.", provenance="user", protected=True)
    _correction(store)
    provider = NoChanges()
    await Synthesizer(provider, "gpt-test", store, min_new_entries=1).run()
    [prompt] = provider.prompts
    memory_part, rules_part = prompt.split("Behaviour rules", 1)[0], prompt
    assert "The repo deploys from main." in memory_part
    assert "When reviewing: cite the line." in rules_part


# ─── cost: off switch and metered cap ───────────────────────────────────────

async def test_a_switched_off_learner_makes_no_call_and_keeps_its_place(tmp_path):
    store = SkillStore(tmp_path)
    _correction(store)
    switch = [False]
    provider = NoChanges()
    synth = Synthesizer(provider, "gpt-test", store, min_new_entries=1, enabled=lambda: switch[0])
    assert not synth.due(closing=True)
    assert await synth.run() == [] and synth.last_pass_skipped
    assert provider.prompts == [] and store.synth_cursor() == 0
    assert store._facts.inference_budget().get("calls", 0) == 0
    switch[0] = True
    await synth.run()
    assert len(provider.prompts) == 1 and store.synth_cursor() == 3


def test_a_metered_learner_never_sends_a_large_request(tmp_path):
    store = SkillStore(tmp_path)
    metered = Synthesizer(NoChanges(), "gpt-test", store, context_window=1_000_000,
                          billing="metered")
    unknown = Synthesizer(NoChanges(), "gpt-test", store, context_window=1_000_000)
    flat = Synthesizer(NoChanges(), "gpt-test", store, context_window=1_000_000,
                       billing="subscription")
    for synth in (metered, unknown, flat):
        synth._size_request()
    assert metered._prompt_limit == unknown._prompt_limit == continual_mod.METERED_PROMPT_CHAR_LIMIT
    assert flat._prompt_limit == continual_mod.LARGE_PROMPT_CHAR_LIMIT
