"""The host's ``Worker:`` text in fluid's environment section.

fluid cannot know the host's worker network, so the host passes the text and
fluid renders it verbatim in the session-stable half of ``# Environment``.
"""

from fluid.context import environment as env_mod
from fluid.context.compiler import PromptInputs, compile_prompt
from fluid.context.environment import environment_block, environment_note

WORKER = (
    "Worker: Unraid (device dev_aaaaaaaaaaaaaaaaaaaaaaaa, in a container). Your "
    "shell, files and tools run on this worker.\n"
    "Other workers in the owner's network: DesktopWorker."
)


def test_worker_text_renders_after_the_shell_line(tmp_path):
    block = environment_block(tmp_path, include_volatile=False, worker=WORKER)
    assert WORKER in block
    assert block.index("Shell:") < block.index("Worker:") < block.index("Git repository:")


def test_no_worker_text_renders_no_line(tmp_path):
    for worker in (None, "", "   "):
        block = environment_block(tmp_path, include_volatile=False, worker=worker)
        assert "Worker:" not in block
        assert block == environment_block(tmp_path, include_volatile=False)


def test_worker_line_is_byte_identical_across_turns(tmp_path, monkeypatch):
    monkeypatch.setattr(env_mod, "_today", lambda: "2026-01-01")
    one = environment_block(tmp_path, include_volatile=False, worker=WORKER)
    monkeypatch.setattr(env_mod, "_today", lambda: "2026-01-02")
    two = environment_block(tmp_path, include_volatile=False, worker=WORKER)
    assert one.encode() == two.encode()
    # The per-turn note is the volatile half; the worker is not in it.
    assert "Worker:" not in environment_note(tmp_path)


def test_compiler_threads_the_worker_into_the_environment_section(tmp_path):
    compiled = compile_prompt(PromptInputs(base="AGENT", cwd=tmp_path, worker=WORKER,
                                           include_skills=False, include_agents=False))
    (env_section,) = compiled.of_kind("environment")
    assert WORKER in env_section.text
    # Off with the environment section itself.
    bare = compile_prompt(PromptInputs(base="AGENT", cwd=tmp_path, worker=WORKER,
                                       include_env=False, include_skills=False,
                                       include_agents=False))
    assert "Worker:" not in (bare.text or "")


def test_moved_session_compiles_the_new_worker(tmp_path):
    before = compile_prompt(PromptInputs(cwd=tmp_path, worker="Worker: Unraid (device a).",
                                         include_skills=False, include_agents=False))
    after = compile_prompt(PromptInputs(cwd=tmp_path, worker="Worker: DesktopWorker (device b).",
                                        include_skills=False, include_agents=False))
    assert "Worker: Unraid" in before.text and "Worker: DesktopWorker" in after.text
    assert "Worker: Unraid" not in after.text
