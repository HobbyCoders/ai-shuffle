"""Real disconnected repositories: the frozen public-root record anchors the container sequence."""
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts.ci import public_cutover as c
from scripts.release import verify_image
from docker.update_contract import image_record

ROOT = Path(__file__).resolve().parents[2]


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def initialize(root):
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Cutover fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "core.hooksPath", str(root / "no-hooks"))


def commit(root, name, text):
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    git(root, "add", ".")
    git(root, "commit", "-qm", "Fixture")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def transition(tmp_path):
    old, new = tmp_path / "old", tmp_path / "public"
    initialize(old)
    published = commit(old, "desktop/src/main/managed-entry.ts", "export const x = 1;")
    envelope = b"signed frozen envelope (signature verification tested separately)"
    record = {"format":1,"old_repository":c.OLD_REPO,"new_repository":c.NEW_REPO,
              "old_repository_id":42,"export_pr":9,"maximum_shipped_sequence":1000,"maximum_desktop_sequence":10000,
              "published_revision":published,"envelope_sha256":hashlib.sha256(envelope).hexdigest()}
    (old / "release").mkdir()
    (old / c.OFFSET).write_text("1000\n")
    export = commit(old, c.MAPPING, json.dumps(record))
    initialize(new)
    shutil.copytree(old, new, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
    initial = commit(new, "desktop/src/main/managed-entry.ts", "export const x = 1;")
    return SimpleNamespace(old=old,new=new,published=published,envelope=envelope,record=record,
                           export=export,initial=initial)


def test_public_root_record_continues_the_archived_sequence(transition):
    t = transition
    assert c.mapping(t.new) == (t.initial, t.record)
    assert c.container_sequence(t.new) == 1001
    assert git(t.new,"rev-list","--count","HEAD") == "1"
    assert subprocess.run(["git","cat-file","-e",t.export],cwd=t.new,capture_output=True).returncode != 0
    next_revision=commit(t.new,"docs-note","reviewed follow-up")
    assert c.container_sequence(t.new,next_revision) == 1002
    with pytest.raises(ValueError,match="never publish"):
        c.container_sequence(t.old,t.export)


@pytest.mark.parametrize("offset",["-1","1.0","9007199254740992","0","1001"])
def test_changed_or_invalid_offset_refused(transition,offset):
    t=transition
    commit(t.new,c.OFFSET,offset)
    with pytest.raises(ValueError):c.container_sequence(t.new)


def test_reverted_epoch_files_are_not_a_second_reset(transition):
    t=transition
    commit(t.new,c.OFFSET,"1001")
    commit(t.new,c.OFFSET,"1000\n")
    with pytest.raises(ValueError,match="immutable"):c.container_sequence(t.new)


def test_uncommitted_offset_never_changes_sequence(transition,monkeypatch):
    t=transition
    (t.new/c.OFFSET).write_text("999999")
    monkeypatch.setenv("CONTAINER_SEQUENCE_OFFSET","999999")
    assert c.container_sequence(t.new)==1001


def test_zero_offset_single_commit_and_shallow_are_refused(tmp_path):
    root=tmp_path/"single"; initialize(root)
    commit(root,c.OFFSET,"0")
    with pytest.raises(ValueError):c.container_sequence(root)
    commit(root,"another","x")
    assert c.container_sequence(root)==2
    shallow=tmp_path/"shallow"
    subprocess.run(["git","clone","--depth=1",root.as_uri(),str(shallow)],check=True,capture_output=True)
    with pytest.raises(ValueError,match="Complete history"):c.container_sequence(shallow)


def test_legacy_unrelated_roots_do_not_break_zero_offset_but_public_imports_fail(transition,tmp_path):
    vendor=tmp_path/"vendor";initialize(vendor);commit(vendor,"vendor-file","vendor")
    legacy=tmp_path/"legacy";initialize(legacy);commit(legacy,c.OFFSET,"0")
    git(legacy,"fetch",str(vendor),"HEAD")
    git(legacy,"merge","--allow-unrelated-histories","--no-edit","FETCH_HEAD")
    assert c.container_sequence(legacy)==2
    assert c.mapping(legacy)[1] is None
    # Importing ANY unrelated root into the new mapped epoch remains forbidden.
    t=transition
    git(t.new,"fetch",str(vendor),"HEAD")
    git(t.new,"merge","--allow-unrelated-histories","--no-edit","FETCH_HEAD")
    with pytest.raises(ValueError,match="private ancestors are forbidden"):
        c.container_sequence(t.new)


def archive(tmp_path, name="archive", touch_epoch_files=True):
    """A scrubbed archive with two roots; one archive commit touches the epoch files."""
    root = tmp_path / name; initialize(root)
    first = commit(root, "app.py", f"{name} 1")
    branch = git(root, "symbolic-ref", "--short", "HEAD")
    git(root, "checkout", "-q", "--orphan", "vendored")
    git(root, "rm", "-rqf", ".")
    second = commit(root, "vendor/lib.py", f"{name} vendored")
    git(root, "checkout", "-q", branch)
    git(root, "merge", "-q", "--allow-unrelated-histories", "--no-edit", "vendored")
    if touch_epoch_files:
        commit(root, c.OFFSET, "777\n")
        commit(root, c.MAPPING, "{}")
    commit(root, "app.py", f"{name} 2")
    return SimpleNamespace(root=root, head=git(root, "rev-parse", "HEAD"), roots=sorted([first, second]))


def allow(root, *imports):
    record = {"format": 1, "imports": [{"head": a.head, "roots": a.roots} for a in imports]}
    return commit(root, c.IMPORTS, json.dumps(record, indent=2))


def import_history(root, source):
    """The import PR: an ours-merge that keeps the public tree and adds the archive as second parent."""
    git(root, "fetch", "-q", str(source), "HEAD")
    git(root, "merge", "-q", "-s", "ours", "--allow-unrelated-histories", "--no-edit", "FETCH_HEAD")
    return git(root, "rev-parse", "HEAD")


def test_single_root_with_an_allowlist_still_works(transition, tmp_path):
    t = transition
    allowed = allow(t.new, archive(tmp_path))
    assert c.mapping(t.new) == (t.initial, t.record)
    assert c.container_sequence(t.new, allowed) == 1002


def test_pinned_import_adds_one_to_the_sequence_and_ignores_archive_epoch_files(transition, tmp_path):
    t, a = transition, archive(tmp_path)
    before = allow(t.new, a)
    merged = import_history(t.new, a.root)
    assert git(t.new, "rev-parse", f"{merged}^{{tree}}") == git(t.new, "rev-parse", f"{before}^{{tree}}")
    assert len(git(t.new, "rev-list", "--max-parents=0", merged).splitlines()) == 3
    # The archive's own OFFSET/MAPPING commits are off the first-parent chain.
    assert git(t.new, "log", "--full-history", "--format=%H", f"{t.initial}..{merged}", "--", c.OFFSET, c.MAPPING)
    assert c.mapping(t.new, merged) == (t.initial, t.record)
    assert c.container_sequence(t.new, merged) == c.container_sequence(t.new, before) + 1 == 1003
    assert c.container_sequence(t.new, commit(t.new, "docs-note", "after import")) == 1004


def test_unlisted_extra_root_refuses(transition, tmp_path):
    t = transition
    allow(t.new, archive(tmp_path))
    import_history(t.new, archive(tmp_path, "other").root)
    with pytest.raises(ValueError, match="private ancestors are forbidden"):
        c.container_sequence(t.new)


def test_unpinned_history_sharing_an_allowlisted_root_refuses(transition, tmp_path):
    # Raw private history can share a root commit with the scrubbed import.
    t, a = transition, archive(tmp_path)
    allow(t.new, a)
    raw = tmp_path / "raw"
    subprocess.run(["git", "clone", "-q", str(a.root), str(raw)], check=True)
    git(raw, "config", "user.name", "Cutover fixture"); git(raw, "config", "user.email", "fixture@example.invalid")
    commit(raw, "secret.txt", "unscrubbed")
    import_history(t.new, raw)
    with pytest.raises(ValueError, match="outside the public root"):
        c.container_sequence(t.new)


def test_extra_root_carrying_the_mapping_refuses(transition, tmp_path):
    t = transition
    rogue = tmp_path / "rogue"; initialize(rogue)
    commit(rogue, c.MAPPING, json.dumps(t.record))
    rogue_archive = SimpleNamespace(head=git(rogue, "rev-parse", "HEAD"), roots=[git(rogue, "rev-parse", "HEAD")])
    allow(t.new, rogue_archive)
    import_history(t.new, rogue)
    with pytest.raises(ValueError, match="Only the public root may carry the cutover mapping"):
        c.container_sequence(t.new)


def test_offset_change_on_the_first_parent_chain_still_refuses_after_an_import(transition, tmp_path):
    t, a = transition, archive(tmp_path)
    allow(t.new, a)
    import_history(t.new, a.root)
    commit(t.new, c.OFFSET, "1001")
    commit(t.new, c.OFFSET, "1000\n")
    with pytest.raises(ValueError, match="immutable"):
        c.container_sequence(t.new)


def test_merge_that_changes_the_offset_refuses(transition):
    t = transition
    main = git(t.new, "symbolic-ref", "--short", "HEAD")
    git(t.new, "checkout", "-q", "-b", "side")
    commit(t.new, c.OFFSET, "1001")
    git(t.new, "checkout", "-q", main)
    git(t.new, "merge", "-q", "--no-ff", "--no-edit", "side")
    commit(t.new, c.OFFSET, "1000\n")  # restoring the content cannot hide either change
    with pytest.raises(ValueError, match="immutable"):
        c.container_sequence(t.new)


def merge_side_change(root, path, text):
    """Change ``path`` on a side branch and merge it with --no-ff, adding no first-parent commit of its own."""
    main = git(root, "symbolic-ref", "--short", "HEAD")
    git(root, "checkout", "-q", "-b", "side")
    commit(root, path, text)
    git(root, "checkout", "-q", main)
    git(root, "merge", "-q", "--no-ff", "--no-edit", "side")
    return git(root, "rev-parse", "HEAD")


def test_merge_that_changes_the_mapping_refuses(transition):
    # Only the merge carries the change to the first-parent chain; nothing
    # else reads the tip's mapping (the record is read from the public root).
    t = transition
    merge_side_change(t.new, c.MAPPING, json.dumps(t.record | {"export_pr": 10}))
    with pytest.raises(ValueError, match="immutable for this history epoch"):
        c.container_sequence(t.new)


def test_merge_that_changes_the_import_record_refuses(transition, tmp_path):
    t, a = transition, archive(tmp_path)
    allow(t.new, a)
    later = archive(tmp_path, "later")
    record = {"format": 1, "imports": [{"head": x.head, "roots": x.roots} for x in (a, later)]}
    merge_side_change(t.new, c.IMPORTS, json.dumps(record, indent=2))
    with pytest.raises(ValueError, match="immutable once committed"):
        c.container_sequence(t.new)


def test_import_record_is_immutable_and_validated(transition, tmp_path):
    t, a = transition, archive(tmp_path)
    allow(t.new, a)
    import_history(t.new, a.root)
    allow(t.new, a, archive(tmp_path, "later"))
    with pytest.raises(ValueError, match="immutable once committed"):
        c.container_sequence(t.new)
    fresh = tmp_path / "fresh"
    subprocess.run(["git", "clone", "-q", str(t.new), str(fresh)], check=True)
    git(fresh, "reset", "-q", "--hard", t.initial)
    git(fresh, "config", "user.name", "Cutover fixture"); git(fresh, "config", "user.email", "fixture@example.invalid")
    commit(fresh, c.IMPORTS, json.dumps({"format": 1, "imports": [{"head": "HEAD", "roots": []}]}))
    with pytest.raises(ValueError, match="Invalid imported history entry"):
        c.container_sequence(fresh)


def test_pinned_roots_must_match_the_pinned_head(transition, tmp_path):
    t, a = transition, archive(tmp_path)
    allow(t.new, SimpleNamespace(head=a.head, roots=a.roots + ["0" * 40]))
    import_history(t.new, a.root)
    with pytest.raises(ValueError, match="do not match the pinned head"):
        c.container_sequence(t.new)


def test_committed_import_record_pins_the_archive():
    record = json.loads((ROOT / c.IMPORTS).read_text())
    assert record == {"format": 1, "imports": [{
        "head": "05e394e92a150d06309da338c7d967682ae473fc",
        "roots": ["2a647413f01c215768fd84f586b7ddb630adfb65", "ca6bdc4938288f795ca5a0b9e4f6538195242248",
                  "e162747bcbd5bcb5a048d66de7516cf189243dbd"]}]}


def test_reused_image_requires_exact_revision_tree_sequence_and_contract(transition):
    t=transition
    record=image_record(t.initial,1001)|{"source_tree":git(t.new,"rev-parse","HEAD^{tree}")}
    verify_image.verify_record(t.new,record)
    for field in ("revision","sequence","source_tree","runtime","provides"):
        bad=dict(record);bad[field]="wrong"
        with pytest.raises(ValueError):verify_image.verify_record(t.new,bad)
    with pytest.raises(ValueError):verify_image.verify_record(t.new,image_record(t.initial,1001))


def test_workflow_canary_reuse_and_final_promotion_order():
    workflow=yaml.load((ROOT/".github/workflows/release.yml").read_text(),Loader=yaml.BaseLoader)
    assert "image_digest" in workflow["on"]["workflow_dispatch"]["inputs"]
    jobs=workflow["jobs"]
    assert jobs["docker"]["with"]["promote_latest"]=="false"
    assert {"buyer-install","source-stable","docker"} <= set(jobs["promote-image"]["needs"])
    assert "--digest" in jobs["promote-image"]["steps"][-1]["run"]
    docker=yaml.load((ROOT/".github/workflows/docker-publish.yml").read_text(),Loader=yaml.BaseLoader)
    steps=docker["jobs"]["build"]["steps"]
    build=next(s for s in steps if s.get("name")=="Build candidate image")
    assert build["if"]=="inputs.image_digest == ''"
    tags=next(s for s in steps if s.get("id")=="meta")["with"]["tags"]
    for alias in ("value=latest","value=nightly","pattern={{major}}","type=ref"):
        assert alias not in tags
    assert "value=${{ inputs.candidate_tag }}" in tags
