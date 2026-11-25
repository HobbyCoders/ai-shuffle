"""tools/research/memory_trigger_replay.py reads transcripts like the app does.

Every record and fact is invented; the transcript is written to tmp_path.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "research" / "memory_trigger_replay.py"
spec = importlib.util.spec_from_file_location("memory_trigger_replay", SCRIPT)
replay_tool = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = replay_tool  # dataclasses look their module up
spec.loader.exec_module(replay_tool)

CALIBRATE = "Calibrate the gizmo sensor array on the bench rig."


def _user(uuid, parent, content, **extra):
    return {"type": "user", "uuid": uuid, "parentUuid": parent, "message": {"role": "user", "content": content},
            **extra}


RECORDS = [
    _user("u1", None, "Export the widget archives to the gizmo store tonight.", cwd="/nowhere/svc"),
    {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Bash",
         "input": {"command": "cat > notes.md <<EOF\nEOF\ngit push origin topic"}},
        {"type": "tool_use", "id": "t2", "name": "Bash", "input": None},
        "not a block"]}},
    None,
    [1, 2],
    _user("m1", "a1", CALIBRATE, isMeta=True),
    _user("s1", "a1", CALIBRATE, isSidechain=True),
    # The boundary's logical parent was never written: the walk goes on from
    # the record before it (the sidechain record is not one).
    {"type": "system", "subtype": "compact_boundary", "uuid": "b1", "parentUuid": None,
     "logicalParentUuid": "never-written"},
    _user("c1", "b1", "Summary: " + CALIBRATE, isCompactSummary=True),
    {"type": "pr-link", "uuid": "p1"},  # app metadata; the resumed session parents on it
    _user("x1", "c1", CALIBRATE),  # rewound: abandoned for u2
    {"type": "assistant", "uuid": "x2", "parentUuid": "x1", "message": "not an object"},
    _user("u2", "p1", [{"type": "text", "text": "Thanks, that is all."}, {"type": "image"}]),
]

FACTS = {"store_id": "this-store", "entries": [
    {"id": "push", "when": "Before every push", "text": "Run the checks first."},
    {"id": "widget", "when": "When exporting widget archives to the gizmo store", "text": "Use the flag."},
    {"id": "bench", "when": "When calibrating the gizmo sensor array on the bench rig", "text": "Zero it."},
    {"id": "elsewhere", "when": "Before every push", "text": "Other worker's path.", "worker": "other-store"},
]}


@pytest.fixture
def corpus(tmp_path):
    transcripts = tmp_path / "projects"
    folder = transcripts / "svc"
    folder.mkdir(parents=True)
    lines = [json.dumps(r) for r in RECORDS] + ["", "{truncated"]
    (folder / "session.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    facts = tmp_path / "memories.json"
    facts.write_text(json.dumps(FACTS), encoding="utf-8")
    return facts, transcripts


def test_session_keeps_the_active_chain_and_skips_harness_records(corpus):
    _, transcripts = corpus
    session = replay_tool.load_session(transcripts / "svc" / "session.jsonl")
    assert session.cwd == "/nowhere/svc"
    turns = [e[1] for e in session.events if e[0] == "turn"]
    assert turns == ["Export the widget archives to the gizmo store tonight.", "Thanks, that is all."]
    assert [e[1][0] for e in session.events if e[0] == "tool"] == ["Bash", "Bash"]


def test_replay_delivers_heredoc_push_and_words_but_not_other_workers_facts(corpus):
    facts, transcripts = corpus
    result, picked = replay_tool.replay(facts, transcripts, 30, sample=10)
    assert result["sessions_replayed"] == 1 and result["unscoped_sessions"] == 1
    assert result["candidate_facts"] == 3  # "elsewhere" belongs to another worker
    assert {d["fact_id"]: d["kind"] for d in picked} == {"push": "command", "widget": "words"}
    push = next(d for d in picked if d["fact_id"] == "push")
    assert push["snippet"] == "Bash: git push origin topic"


def test_outputs_never_overwrite_inputs(corpus, tmp_path):
    facts, transcripts = corpus
    base = ["--facts", str(facts), "--transcripts", str(transcripts)]
    for extra in (["--json", str(facts)], ["--json", str(tmp_path / "." / "memories.json")],
                  ["--sample", "5", "--out", str(transcripts / "svc" / "sample.jsonl")]):
        with pytest.raises(SystemExit):
            replay_tool.main(base + extra)
    link = tmp_path / "link.json"
    link.symlink_to(facts)
    with pytest.raises(SystemExit):
        replay_tool.main(base + ["--json", str(link)])
    assert json.loads(facts.read_text(encoding="utf-8")) == FACTS


def test_outputs_never_overwrite_a_transcript_linked_from_outside(corpus, tmp_path):
    facts, transcripts = corpus
    outside = tmp_path / "outside.jsonl"
    original = (transcripts / "svc" / "session.jsonl").read_text(encoding="utf-8")
    outside.write_text(original, encoding="utf-8")
    try:
        (transcripts / "svc" / "linked.jsonl").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    base = ["--facts", str(facts), "--transcripts", str(transcripts)]
    for extra in (["--json", str(outside)], ["--sample", "5", "--out", str(outside)]):
        with pytest.raises(SystemExit):
            replay_tool.main(base + extra)
    assert outside.read_text(encoding="utf-8") == original


def test_project_map_scopes_a_directory(corpus):
    facts, transcripts = corpus
    document = json.loads(facts.read_text(encoding="utf-8"))
    document["entries"].append({"id": "scoped", "when": "Before every push", "text": "x",
                                "scope": "project:pv2-example"})
    facts.write_text(json.dumps(document), encoding="utf-8")
    plain, _ = replay_tool.replay(facts, transcripts, 30)
    mapped, _ = replay_tool.replay(facts, transcripts, 30, project_map={"/nowhere": "project:pv2-example"})
    assert plain["unscoped_sessions"] == 1 and mapped["unscoped_sessions"] == 0
    assert plain["facts_in_scope"] == 3 and mapped["facts_in_scope"] == 4
