"""E10 learner-yield report against an offline fixture.

The fixture is two replicas shaped like the review's store (verify/learner-yield):
the desktop replica ``4a1f44`` with its local memories.json and pass log, and
a peer ``74c828`` whose facts and log arrive through Postgres and a copied log.
Postgres is the test suite's SQLite shim, reached through ``app.db.base.get_db``
exactly as the report reaches the real database.
"""

from __future__ import annotations

import json
from datetime import datetime

from app.db.base import get_db
from scripts.experiments import e10_learner_yield as e10

DAY1 = datetime(2026, 9, 21, 12).timestamp()
DAY2 = datetime(2026, 9, 22, 12).timestamp()
DAY3 = datetime(2026, 9, 23, 12).timestamp()
SINCE, UNTIL = "2026-09-21", "2026-09-24"


def remember(fid, text, session, at, key):
    return {"id": fid, "origin": "4a1f44", "text": text, "protected": True, "provenance": "session",
            "key": key, "source_session": session, "created_at": at, "revision": 1}


def learner(fid, text, at, origin):
    return {"id": fid, "origin": origin, "text": text, "protected": False, "provenance": "user",
            "created_at": at, "revision": 1}


LOCAL = {
    "schema": 2, "owner": "local", "store_id": "4a1f44", "next_id": 80,
    "entries": [
        remember("m1", "The deploy script lives at scripts/deploy.ps1 and needs admin rights.",
                 "sess-r1", DAY1, "deploy.script"),
        remember("m2", "Staging database backups rotate every Tuesday night at midnight.",
                 "sess-r2", DAY1 + 60, "backup.rotation"),
        remember("m3", "The user prefers tabs over spaces in every Makefile they own.",
                 "sess-missing", DAY1 + 120, "style.tabs"),
        {"id": "m5", "origin": "4a1f44", "text": "python is 3.12", "protected": False,
         "provenance": "tool", "key": "env.python", "created_at": DAY2},
        learner("m74", "Test runs need PYTHONPATH pointing at the fluid package directory.", DAY3, "4a1f44"),
        # The peer's facts, merged into this replica: counted once, for the peer.
        learner("74c828:m24", "The web app keeps the theme under the localStorage key aih.theme.",
                DAY3, "74c828"),
    ],
    "archived": [],
    "tombstones": [{"id": "74c828:m25", "origin": "74c828", "revision": 2, "key": None,
                    "scope": "global", "deleted_at": DAY3, "fingerprints": []}],
}

PEER = {
    "schema": 2, "owner": "local", "store_id": "74c828",
    "entries": [
        learner("m24", "The web app keeps the theme under the localStorage key aih.theme.", DAY3, "74c828"),
        learner("m26", "Session titles are generated after the first completed turn only.", DAY3 + 30, "74c828"),
        learner("4a1f44:m74", "Test runs need PYTHONPATH pointing at the fluid package directory.",
                DAY3, "4a1f44"),
    ],
    "tombstones": [],
}


def log_rows(ts0, rows):
    return "\n".join(json.dumps({"ts": ts0 + i * 60, **r}) for i, r in enumerate(rows))


def make_fixture(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    (root / "memories.json").write_text(json.dumps(LOCAL), encoding="utf-8")
    silent = [{"kind": "synthesis", "changed": [], "error": "synthesis returned no text"}] * 4
    (root / ".synthesis-log.jsonl").write_text(
        log_rows(DAY1, silent) + "\n"
        + log_rows(DAY3, [{"kind": "synthesis", "changed": ["memory:m74"]},
                          {"kind": "consolidation", "changed": []}]),
        encoding="utf-8")
    peer_log = tmp_path / "peer-synthesis-log.jsonl"
    # Rows stamped with store_id are credited by it, whatever the file label.
    peer_log.write_text(log_rows(DAY3, [
        {"kind": "synthesis", "changed": ["memory:m24", "memory:m25"], "store_id": "74c828"},
        {"kind": "synthesis", "changed": ["memory:m26"], "store_id": "74c828"},
        {"kind": "synthesis", "changed": [], "store_id": "74c828"},
    ]), encoding="utf-8")

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO network_memory_records (id, kind, scope, content, content_hash, deleted) "
                    "VALUES (%s, %s, %s, %s, %s, FALSE)",
                    ("memory-v2:peerdevice:ownerhash", "fact", "global", json.dumps(PEER), "h1"))
        cur.execute("INSERT INTO network_memory_records (id, kind, scope, content, content_hash, deleted) "
                    "VALUES (%s, %s, %s, %s, %s, TRUE)",
                    ("memory-v2:olddevice:ownerhash", "fact", "global",
                     json.dumps({"store_id": "dead", "entries": [learner("m1", "gone", DAY2, "dead")]}), "h2"))
        nodes = [
            # m1: the user said it, with the memory snapshot appended.
            ("sess-r1", "n1", DAY1 - 30, {"role": "user", "content": [{"kind": "text", "text":
                "Remember that the deploy script lives at scripts/deploy.ps1, it needs admin rights."
                "\n\n<system-reminder>\n# Memory\n- m9 unrelated</system-reminder>"}]}),
            # m2: only the reminder echo mentions it, cut off before its closing tag.
            ("sess-r2", "n2", DAY1, {"role": "user", "content": [{"kind": "text", "text":
                "Please keep going.\n\n<system-reminder>\n# Memory\n- Staging database backups rotate "
                "every Tuesday night at midnight"}]}),
            ("sess-r2", "n3", DAY1 + 10, {"role": "user", "content": [{"kind": "toolresultblock",
                "tool_use_id": "t", "content": [{"kind": "text",
                "text": "Staging database backups rotate every Tuesday night at midnight."}]}]}),
            # Later assistant turns: one cites the peer's localStorage key.
            ("sess-late", "n4", DAY3 + 3600, {"role": "assistant", "content": [{"kind": "text", "text":
                "I reset the theme by clearing aih.theme in localStorage."}]}),
            ("sess-late", "n5", DAY3 + 3700, {"role": "assistant", "content": [{"kind": "tooluseblock",
                "id": "x", "name": "Bash", "input": {"command": "PYTHONPATH=fluid pytest"}}]}),
            # Before the fact existed: not a citation.
            ("sess-early", "n6", DAY1, {"role": "assistant", "content": [{"kind": "text", "text":
                "Session titles are generated after the first completed turn only."}]}),
        ]
        for session_id, node_id, ts, message in nodes:
            cur.execute("INSERT INTO fluid_session_nodes (session_id, node_id, parent_id, ts, message) "
                        "VALUES (%s, %s, NULL, %s, %s)", (session_id, node_id, ts, json.dumps(message)))
    return root, peer_log


def run(tmp_path, *extra):
    root, peer_log = make_fixture(tmp_path)
    out = tmp_path / "report.json"
    printed = []
    code = e10.main(["--since", SINCE, "--until", UNTIL, "--skills-root", str(root),
                     "--log", f"peer={peer_log}", "--json", str(out), *extra],
                    emit=printed.append, get_db=get_db)
    assert code == 0
    return json.loads(out.read_text()), printed[0]


def test_facts_written_per_replica_per_day(tmp_path):
    report, _ = run(tmp_path)
    written = report["facts_written"]
    assert written["local"]["2026-09-21"] == {"remember": 3}
    assert written["local"]["2026-09-22"] == {"harvest": 1}
    assert written["local"]["2026-09-23"] == {"learner": 1}
    # The peer's facts are counted once although both snapshots carry them.
    assert written["device:peerdevice"]["2026-09-23"] == {"learner": 2}
    assert report["learner_facts_tombstoned"] == {"device:peerdevice": 1}
    # A deleted replica record is not read.
    assert all("dead" not in w for w in written)


def test_passes_run_and_passes_that_wrote(tmp_path):
    report, _ = run(tmp_path)
    local = report["passes"]["local"]
    assert local["2026-09-21"] == {"synthesis_passes": 4, "passes": 4, "errors": 4}
    assert local["2026-09-23"]["passes_that_wrote"] == 1
    assert local["2026-09-23"]["consolidation_passes"] == 1
    peer = report["passes"]["device:peerdevice"]["2026-09-23"]
    assert peer == {"synthesis_passes": 3, "passes": 3, "passes_that_wrote": 2, "memory_writes": 3}
    assert report["silence"]["local"]["longest_silent_synthesis_streak"] == 4
    assert report["silence"]["local"]["last_write"] == "2026-09-23"


def test_later_citations_of_learner_facts(tmp_path):
    report, _ = run(tmp_path)
    assert report["citations"]["device:peerdevice"] == {"learner_facts": 2, "learner_cited": 1}
    # PYTHONPATH on its own is not a distinctive identifier for m74.
    assert report["citations"]["local"]["learner_facts"] == 1
    assert report["citations"]["local"].get("learner_cited", 0) == 0
    cited = {c["fact"] for c in report["cited_facts"]}
    assert "74c828:m24" in cited and "74c828:m26" not in cited


def test_remember_facts_a_user_message_supports(tmp_path):
    report, text = run(tmp_path)
    support = report["remember_support"]
    verdicts = {f["fact"]: f["verdict"] for f in support["facts"]}
    assert verdicts == {"4a1f44:m1": "supported", "4a1f44:m2": "unsupported",
                        "4a1f44:m3": "session_missing"}
    assert (support["total"], support["supported"]) == (3, 1)
    assert "Remember facts a user message supports: 1 of 3" in text


def test_files_only_mode_reads_no_database(tmp_path):
    root, _ = make_fixture(tmp_path)
    printed = []

    def no_db():
        raise AssertionError("the database must not be read with --no-db")

    assert e10.main(["--since", SINCE, "--until", UNTIL, "--skills-root", str(root), "--no-db"],
                    emit=printed.append, get_db=no_db) == 0
    assert "Replica local:" in printed[0]


def test_dry_run_reads_only_the_files_named(tmp_path, monkeypatch):
    """--dry-run touches neither Postgres nor the app's configuration: without
    --skills-root there is no local replica, only the stores and logs given."""
    from app.core import fluid_memory

    root, peer_log = make_fixture(tmp_path)

    def forbidden(what):
        def _raise(*_args, **_kwargs):
            raise AssertionError(f"{what} in --dry-run")
        return _raise

    monkeypatch.setattr(e10, "load_db_snapshots", forbidden("Postgres read"))
    monkeypatch.setattr(e10, "load_db_nodes", forbidden("Postgres read"))
    monkeypatch.setattr(fluid_memory, "skills_root", forbidden("app configuration read"))
    out = tmp_path / "dry.json"
    printed = []
    assert e10.main(["--since", SINCE, "--until", UNTIL, "--dry-run", "--log", f"peer={peer_log}",
                     "--store", f"desktop={root / 'memories.json'}", "--json", str(out)],
                    emit=printed.append) == 0
    report = json.loads(out.read_text())
    assert "local" not in report["replicas"]
    # The store file is credited by its label; the log rows by their store_id.
    assert report["facts_written"]["desktop"]["2026-09-23"] == {"learner": 1}
    assert report["passes"]["74c828"]["2026-09-23"]["passes"] == 3
    # No transcripts were read, so nothing can be cited.
    assert not any(v.get("learner_cited") for v in report["citations"].values())
    # --dry-run with --skills-root still reads that root, and nothing else.
    printed.clear()
    assert e10.main(["--since", SINCE, "--until", UNTIL, "--dry-run", "--skills-root", str(root)],
                    emit=printed.append) == 0
    assert "Replica local:" in printed[0]


def test_reminder_stripping_covers_unterminated_blocks():
    text = "keep going\n<system-reminder>\n# Memory\n- a fact that was echoed"
    assert e10.strip_reminders(text).strip() == "keep going"
    assert e10.message_text({"content": [{"kind": "text", "text": "<task-notification>done"}]},
                            role="user") == ""


def test_citation_heuristic():
    fact = "The web app keeps the theme under the localStorage key aih.theme."
    assert e10.cites(fact, "cleared aih.theme")
    assert e10.cites(fact, "so the web app keeps the theme under a key")
    assert not e10.cites(fact, "the theme is dark")
    assert e10.cites("anything", "use style.tabs here", key="style.tabs")
