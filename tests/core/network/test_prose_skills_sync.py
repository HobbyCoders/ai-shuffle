"""Prose skill folders (``~/.fluid/skills/<name>/SKILL.md``) replicate between workers."""
import json

from app.db import network as store
from app.network import engine, memory_records, prose_skills
from tests.core.network.harness import converge, join

SKILL = "---\nname: three-mockups\ndescription: Three UI mockups\n---\nDispatch three agents.\n"


def _root(worker):
    # tests/conftest.py pins the prose root next to the worker's code-mode root.
    return worker.data_dir / "fluid" / "prose-skills"


def _prose_ops(worker):
    return [o for o in store.ops_since({}) if o["collection"] == "memory"
            and o["record_id"].startswith(prose_skills.PREFIX) and o["origin_device_id"] == worker.device_id]


def _ids(worker):
    return [r["id"] for r in worker.sql("SELECT id FROM network_memory_records WHERE id LIKE 'prose-skill:%'")]


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode() if isinstance(text, str) else text)


def test_prose_skill_folder_round_trip_with_updates_and_deletes(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
        skill = _root(a) / "three-mockups"
        _write(skill / "SKILL.md", SKILL)
        _write(skill / "references" / "layout.md", "Use a grid.\r\nKeep CRLF.\n")
        _write(skill / "references" / "old.md", "retire me")
        _write(skill / "assets" / "logo.png", b"\x89PNG\r\n\x1a\n\xff\xfe")
        _write(skill / "big.md", "x" * (prose_skills.MAX_FILE_BYTES + 1))
        _write(skill / ".DS_Store", "junk")
        assert memory_records.publish() >= 1
        assert [o["record_id"] for o in _prose_ops(a)] == ["prose-skill:three-mockups"]
    join(a, b)
    with b.active():
        assert memory_records.apply_to_files(_ids(b)) == 1
        copy = _root(b) / "three-mockups"
        assert (copy / "SKILL.md").read_bytes() == SKILL.encode()
        assert (copy / "references" / "layout.md").read_bytes() == b"Use a grid.\r\nKeep CRLF.\n"
        assert not (copy / "assets").exists() and not (copy / "big.md").exists()
        assert not (copy / ".DS_Store").exists()
        # A local-only extra is never synced or touched by remote changes.
        _write(copy / "assets" / "local.bin", b"\x00\xff")
        # Materializing a record is not a local edit: nothing echoes back.
        assert memory_records.publish() == 0
    with a.active():
        _write(skill / "SKILL.md", SKILL + "Step two.\n")
        (skill / "references" / "old.md").unlink()
        assert memory_records.publish() == 1
    converge([a, b])
    with b.active():
        memory_records.apply_to_files(_ids(b))
        assert (copy / "SKILL.md").read_text().endswith("Step two.\n")
        assert not (copy / "references" / "old.md").exists()
        assert (copy / "references" / "layout.md").exists()
    with a.active():
        for path in sorted(skill.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        skill.rmdir()
        assert memory_records.publish() == 1
    converge([a, b])
    with b.active():
        memory_records.apply_to_files(_ids(b))
        assert not (copy / "SKILL.md").exists() and not (copy / "references").exists()
        assert (copy / "assets" / "local.bin").read_bytes() == b"\x00\xff"
        row = b.sql("SELECT deleted FROM network_memory_records WHERE id='prose-skill:three-mockups'")[0]
        assert row["deleted"]


def test_peer_record_not_yet_on_disk_is_neither_deleted_nor_reverted(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
        _write(_root(a) / "audit" / "SKILL.md", "v1")
        memory_records.publish()
    join(a, b)
    with b.active():
        # The row is here but the folder is not: absence is not a deletion.
        assert memory_records.publish() == 0
        memory_records.apply_to_files(_ids(b))
    with a.active():
        _write(_root(a) / "audit" / "SKILL.md", "v2")
        memory_records.publish()
    converge([a, b])
    with b.active():
        # v2 reached the database but not the disk; still v1 locally.
        assert (_root(b) / "audit" / "SKILL.md").read_text() == "v1"
        assert memory_records.publish() == 0
        row = b.sql("SELECT content FROM network_memory_records WHERE id='prose-skill:audit'")[0]
        assert json.loads(row["content"])["files"]["SKILL.md"] == "v2"
        memory_records.apply_to_files(_ids(b))
        assert (_root(b) / "audit" / "SKILL.md").read_text() == "v2"


def test_unpublished_local_edit_is_not_overwritten_and_wins_next(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
        _write(_root(a) / "audit" / "SKILL.md", "v1")
        memory_records.publish()
    join(a, b)
    with b.active():
        memory_records.apply_to_files(_ids(b))
        _write(_root(b) / "audit" / "SKILL.md", "b-edit")
    with a.active():
        _write(_root(a) / "audit" / "SKILL.md", "a-edit")
        memory_records.publish()
    converge([a, b])
    with b.active():
        memory_records.apply_to_files(_ids(b))
        assert (_root(b) / "audit" / "SKILL.md").read_text() == "b-edit"
        assert memory_records.publish() == 1
    converge([a, b])
    with a.active():
        memory_records.apply_to_files(_ids(a))
        assert (_root(a) / "audit" / "SKILL.md").read_text() == "b-edit"


def test_names_limits_and_held_folders(workers):
    a = workers("a")
    with a.active():
        engine.create_network("Home", device_name="A")
        root = _root(a)
        _write(root / ".hidden" / "SKILL.md", "no")
        _write(root / "bad name" / "SKILL.md", "no")
        _write(root / "no-skill-md" / "README.md", "no")
        _write(root / "ok" / "SKILL.md", "yes")
        _write(root / "stray.md", "not a folder")
        memory_records.publish()
        assert _ids(a) == ["prose-skill:ok"]
        # Past a limit the folder is held: not republished and not deleted.
        _write(root / "ok" / "SKILL.md", "y" * (prose_skills.MAX_FILE_BYTES + 1))
        assert memory_records.publish() == 0
        for i in range(prose_skills.MAX_FILES + 1):
            _write(root / "many" / ("SKILL.md" if i == 0 else f"f{i}.md"), "z")
        assert memory_records.publish() == 0
        assert not a.sql("SELECT deleted FROM network_memory_records WHERE id='prose-skill:ok'")[0]["deleted"]
        assert "prose-skill:many" not in _ids(a)
        # A missing root is not a mass deletion.
        for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        root.rmdir()
        assert memory_records.publish() == 0


def test_held_folder_warns_once_per_reason(workers, caplog, monkeypatch):
    monkeypatch.setattr(prose_skills, "_held_logged", {})
    a = workers("a")
    with a.active():
        engine.create_network("Home", device_name="A")
        root = _root(a)
        for i in range(prose_skills.MAX_FILES + 1):
            _write(root / "many" / ("SKILL.md" if i == 0 else f"f{i}.md"), "z")

        def held_warnings():
            return [r for r in caplog.records if r.levelname == "WARNING"
                    and r.getMessage().startswith("Prose skill many not synced")]

        caplog.set_level("INFO", logger=prose_skills.log.name)
        memory_records.publish()
        memory_records.publish()
        assert len(held_warnings()) == 1
        assert "too many files or bytes" in held_warnings()[0].getMessage()

        # A different reason is news and logs again, once.
        _write(root / "many" / "SKILL.md", "y" * (prose_skills.MAX_FILE_BYTES + 1))
        memory_records.publish()
        memory_records.publish()
        assert len(held_warnings()) == 2
        assert "SKILL.md is over the size limit" in held_warnings()[1].getMessage()

        # Back within the limits it syncs and forgets; holding it again warns again.
        for i in range(1, prose_skills.MAX_FILES + 1):
            (root / "many" / f"f{i}.md").unlink()
        _write(root / "many" / "SKILL.md", "fits")
        memory_records.publish()
        assert "prose-skill:many" in _ids(a)
        assert "many" not in prose_skills._held_logged
        assert any(r.getMessage() == "Prose skill many now synced" for r in caplog.records)
        _write(root / "many" / "SKILL.md", "y" * (prose_skills.MAX_FILE_BYTES + 1))
        memory_records.publish()
        assert len(held_warnings()) == 3


def test_hostile_records_are_rejected():
    assert prose_skills.decode(prose_skills.encode({"SKILL.md": "ok", "references/a.md": "a"})) is not None
    for files in ({"../escape.md": "x", "SKILL.md": "x"}, {"SKILL.md": "x", "a/../../b": "x"},
                  {"SKILL.md": "x", ".git/config": "x"}, {"SKILL.md": "x", "a/b/c/d.md": "too deep"},
                  {"README.md": "no skill file"},
                  {"SKILL.md": "x" * (prose_skills.MAX_FILE_BYTES + 1)},
                  {f"f{i}.md" if i else "SKILL.md": "x" for i in range(prose_skills.MAX_FILES + 1)}):
        assert prose_skills.decode(prose_skills.encode(files)) is None
    assert prose_skills.decode("not json") is None
    assert prose_skills.decode(json.dumps({"schema": 99, "files": {"SKILL.md": "x"}})) is None


def test_invalid_record_name_is_ignored(workers):
    a = workers("a")
    with a.active():
        engine.create_network("Home", device_name="A")
        content = prose_skills.encode({"SKILL.md": "x"})
        for rid in ("prose-skill:../evil", "prose-skill:.hidden"):
            a.sql("INSERT INTO network_memory_records (id, kind, scope, content, content_hash, deleted) "
                  "VALUES (?, 'prose_skill', 'global', ?, 'h', 0)", (rid, content))
        assert memory_records.apply_to_files(["prose-skill:../evil", "prose-skill:.hidden"]) == 0
        assert not (a.data_dir / "fluid" / "evil").exists()
        assert not _root(a).exists()
