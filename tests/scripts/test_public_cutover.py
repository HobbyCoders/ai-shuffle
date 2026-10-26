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
