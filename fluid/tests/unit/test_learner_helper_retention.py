"""Read-only helper code must reach the learner and work in a fresh kernel."""
import pytest
from fluid.codemode import SkillStore, Synthesizer, SubprocessKernel
from fluid.codemode.continual import low_value_entry, _SYNTH_SYSTEM
from fluid.codemode.kernel import HostReply
from fluid.providers.base import StreamChunk

SOURCE = '"""Select nonempty rows."""\ndef nonempty(rows):\n    return [r for r in rows if r]\n\ndef _selftest():\n    assert nonempty(["a", "", "b"]) == ["a", "b"]\n'


class Learner:
    name = "fake"
    def __init__(self):
        self.prompts = []
    async def stream(self, request):
        self.prompts.append(request.messages[0].content[0].text)
        yield StreamChunk("text_delta", {"text": f'<skill name="nonempty">{SOURCE}</skill>'})
        yield StreamChunk("usage", {"usage": {"output_tokens": 50}, "stop_reason": "end_turn"})


@pytest.mark.parametrize("code", [
    "def helper(rows):\n    return rows", "async def helper():\n    pass",
    "class Helper: pass", "helper = lambda rows: rows", "incomplete(",
])
def test_read_only_definitions_are_not_discarded(code):
    assert not low_value_entry({"kind": "cell", "code": code, "tools": ["Read"]})
    assert low_value_entry({"kind": "cell", "code": "Read(file_path='data.txt')", "tools": ["Read"]})


async def test_journal_to_verified_skill_to_fresh_kernel(tmp_path):
    store = SkillStore(tmp_path)
    store.journal_append({"kind": "cell", "code": SOURCE + "Read(file_path='rows.txt')",
                          "tools": ["Read"], "host_calls": 1, "stdout": "['a', 'b']"})
    learner = Learner()
    changed = await Synthesizer(learner, "fake", store, min_new_entries=1).run()
    assert "def nonempty(rows)" in learner.prompts[0]
    assert "nonempty" in changed
    fresh = SkillStore(tmp_path)
    assert not fresh.needs_verification("nonempty")
    kernel = SubprocessKernel()
    await kernel.start()
    async def no_tools(name, kwargs):
        return HostReply(error="No tools allowed")
    try:
        await kernel.set_vfs({}, fresh.sources())
        result = await kernel.exec("from skills import nonempty\nnonempty(['x', '', 'y'])",
                                   timeout=5, on_host_call=no_tools)
        assert result.error is None, result.error
        assert result.value_repr == "['x', 'y']"
    finally:
        await kernel.stop()


def test_learner_knows_the_enforced_fact_length_and_scope():
    assert "1–500 characters" in _SYNTH_SYSTEM
    assert "task-specific choice is not automatically a general habit" in _SYNTH_SYSTEM
