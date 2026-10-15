"""Real disconnected repositories; no private objects enter the public checkout."""
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts.ci import public_cutover as c
from scripts.ci import edge_publication_gate as gate
from scripts.release import repin_catalog, promote_image, verify_image, cutover_check
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
    def api(repo, route):
        assert repo == c.OLD_REPO
        if not route:
            return {"id":42,"full_name":c.OLD_REPO,"private":True}
        if route == "branches/main":
            return {"protected":True,"commit":{"sha":export}}
        if route == "pulls/9":
            return {"merged_at":"now","merge_commit_sha":export,"base":{"ref":"main","repo":{"id":42}}}
        raise AssertionError(route)
    return SimpleNamespace(old=old,new=new,published=published,envelope=envelope,record=record,
                           export=export,initial=initial,api=api)


def bridge(t, **kwargs):
    return c.verify_bridge(t.new,"HEAD",t.published,t.envelope,private_root=t.old,
                           github=kwargs.pop("github",t.api),walk=kwargs.pop("walk",gate.update_reach_readiness),**kwargs)


def test_real_disconnected_exact_tree_bridge(transition):
    t = transition
    assert bridge(t) == (t.initial,t.export)
    assert c.container_sequence(t.new) == 1001
    assert git(t.new,"rev-list","--count","HEAD") == "1"
    assert subprocess.run(["git","cat-file","-e",t.export],cwd=t.new,capture_output=True).returncode != 0
    next_revision=commit(t.new,"docs-note","reviewed follow-up")
    assert c.container_sequence(t.new,next_revision) == 1002
    with pytest.raises(ValueError,match="never publish"):
        c.container_sequence(t.old,t.export)


@pytest.mark.parametrize("change",["tree","envelope","revision","id","name","visibility","unmerged","unprotected","base"])
def test_bridge_adversarial_evidence(transition,change):
    t=transition
    if change=="tree":
        # A non-runtime file is still part of the full-tree proof.
        t.initial=commit(t.new,"private-note","must not silently differ")
        # Verify root mismatch by modifying the old export instead.
        t.export=commit(t.old,"private-note","different export tree")
    if change=="envelope": t.envelope+=b"changed"
    if change=="revision": t.published="f"*40
    def api(repo,route):
        value=t.api(repo,route)
        if not route:
            if change=="id":value["id"]=7
            if change=="name":value["full_name"]="other/repo"
            if change=="visibility":value["private"]=False
        if change=="tree":
            if route=="branches/main":value["commit"]["sha"]=t.export
            if route=="pulls/9":value["merge_commit_sha"]=t.export
        if route=="branches/main" and change=="unprotected":value["protected"]=False
        if route=="pulls/9":
            if change=="unmerged":value["merged_at"]=None
            if change=="base":value["base"]["repo"]["id"]=7
        return value
    with pytest.raises(ValueError):bridge(t,github=api)


@pytest.mark.parametrize("tampered", [False, True])
def test_publication_baseline_only_accepts_the_frozen_signed_cutover(transition, monkeypatch, tmp_path, tampered):
    t = transition
    candidate = commit(t.new, "later", "new public change")
    monkeypatch.chdir(t.new)
    envelope = tmp_path / "manifest.json"
    envelope.write_bytes(t.envelope + (b"tampered" if tampered else b""))
    monkeypatch.setattr(gate, "edge_cache_path", lambda: envelope)
    # Signature validation itself is covered by the existing manifest tests;
    # this test drives the producer's real mapping and exact-envelope hash.
    monkeypatch.setattr(gate, "verify_edge_manifest", lambda raw: t.published)
    if tampered:
        with pytest.raises(ValueError, match="frozen cutover"):
            gate.unpublished_history([candidate, t.initial], t.published)
    else:
        assert gate.unpublished_history([candidate, t.initial], t.published) == [candidate, t.initial]


def test_private_walk_cannot_be_replaced_by_tree_equality(transition):
    t=transition
    calls=[]
    def refused(export,**kwargs):
        calls.append((export,kwargs));return False,"owner must accept windows-launcher"
    with pytest.raises(ValueError,match="windows-launcher"):bridge(t,walk=refused)
    assert calls[0][1]["root"]==t.old
    assert calls[0][1]["repository"]==c.OLD_REPO
    assert calls[0][1]["base"]==t.published
    assert calls[0][1]["bridge"] is False


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


def test_catalog_producer_repins_public_bytes_without_old_objects(tmp_path):
    root=tmp_path/"catalog";initialize(root)
    first=commit(root,"app/core/connectors/one.py","hello")
    path=root/"app/core/connectors/catalog.json"
    path.write_text(json.dumps({"revision":"f"*40,"release":"old","packages":{"one":{"files":{"one.py":hashlib.sha256(b"hello").hexdigest()}}}}))
    repin_catalog.repin(root,first)
    repin_catalog.repin(root,first,check=True)
    assert json.loads(path.read_text())["revision"]==first
    with pytest.raises(ValueError):repin_catalog.repin(root,"e"*40)


def test_reused_image_requires_exact_revision_tree_sequence_and_contract(transition):
    t=transition
    record=image_record(t.initial,1001)|{"source_tree":git(t.new,"rev-parse","HEAD^{tree}")}
    verify_image.verify_record(t.new,record)
    for field in ("revision","sequence","source_tree","runtime","provides"):
        bad=dict(record);bad[field]="wrong"
        with pytest.raises(ValueError):verify_image.verify_record(t.new,bad)
    with pytest.raises(ValueError):verify_image.verify_record(t.new,image_record(t.initial,1001))


def test_real_private_launcher_delta_needs_its_own_old_pr_label(transition, tmp_path):
    t=transition
    changed=commit(t.old,"desktop/src/main/managed-entry.ts","export const x = 2;")
    fresh=tmp_path/"new-public";initialize(fresh)
    shutil.copytree(t.old,fresh,dirs_exist_ok=True,ignore=shutil.ignore_patterns(".git"))
    commit(fresh,"desktop/src/main/managed-entry.ts","export const x = 2;")
    t.new=fresh
    def api(labels):
        def call(repo,route):
            if route=="branches/main":return {"protected":True,"commit":{"sha":changed}}
            if route=="pulls/9":return {"merged_at":"now","merge_commit_sha":changed,"base":{"ref":"main","repo":{"id":42}}}
            if route.startswith(f"commits/{changed}/pulls"):
                return [{"number":9,"merged_at":"now","merge_commit_sha":changed,"base":{"ref":"main","repo":{"full_name":c.OLD_REPO}},"labels":[{"name":label} for label in labels]}]
            return t.api(repo,route)
        return call
    with pytest.raises(ValueError,match="windows-launcher"):
        bridge(t,github=api([]))
    with pytest.raises(ValueError,match="windows-launcher"):
        bridge(t,github=api(["update-reach:docker-image"]))
    assert bridge(t,github=api(["update-reach:windows-launcher"]))[1]==changed


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
