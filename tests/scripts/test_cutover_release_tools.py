"""Adversarial external-boundary tests for cutover release tools."""
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from scripts.release import attach_linux, promote_image, push_candidate, verify_image, cutover_check, prepare_cutover
from scripts.ci import public_cutover as c

ROOT=Path(__file__).resolve().parents[2]
DIGEST="sha256:"+"a"*64


@pytest.fixture
def linux(tmp_path):
    artifacts=[]
    for name in ("shuffle.AppImage","shuffle.deb"):
        data=name.encode();(tmp_path/name).write_bytes(data)
        artifacts.append({"name":name,"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()})
    proof={"sourceCommit":"a"*40,"version":"1.0.0","artifacts":artifacts}
    (tmp_path/"linux-build-provenance.json").write_text(json.dumps(proof))
    (tmp_path/"SHA256SUMS").write_text("".join(f"{a['sha256']}  {a['name']}\n" for a in artifacts))
    return tmp_path,proof


def test_linux_producer_bytes_and_complete_inventory(linux):
    root,proof=linux
    files=attach_linux.verified_files(root,"v1.0.0","a"*40)
    assert {p.name for p in files}=={"shuffle.AppImage","shuffle.deb","SHA256SUMS-linux","linux-build-provenance.json"}
    (root/"shuffle.AppImage").write_bytes(b"tampered")
    with pytest.raises(ValueError,match="bytes differ"):attach_linux.verified_files(root,"v1.0.0","a"*40)


@pytest.mark.parametrize("field,value",[("sourceCommit","b"*40),("version","1.0.1")])
def test_linux_wrong_release_refused(linux,field,value):
    root,proof=linux;proof[field]=value
    (root/"linux-build-provenance.json").write_text(json.dumps(proof))
    with pytest.raises(ValueError):attach_linux.verified_files(root,"v1.0.0","a"*40)


def test_linux_upload_readback_and_retry(linux,monkeypatch):
    root,_=linux; assets=[];uploads=[]
    monkeypatch.setattr(attach_linux,"verify_tag",lambda *_:"a"*40)
    monkeypatch.setattr(attach_linux,"verify_merged_pr",lambda *_:None)
    monkeypatch.setattr(attach_linux,"github",lambda route: assets if "/assets?" in route else {"id":1})
    def run(args,**kwargs):
        assert args[:3]==["gh","release","upload"] and "--clobber" not in args
        path=Path(args[4]);data=path.read_bytes();uploads.append(path.name)
        assets.append({"name":path.name,"size":len(data),"digest":"sha256:"+hashlib.sha256(data).hexdigest(),"state":"uploaded"})
    monkeypatch.setattr(attach_linux.subprocess,"run",run)
    attach_linux.attach(root,root,"v1.0.0")
    assert len(uploads)==4
    attach_linux.attach(root,root,"v1.0.0")
    assert len(uploads)==4
    assets[0]["digest"]="sha256:"+"f"*64
    with pytest.raises(ValueError,match="read-back"):attach_linux.attach(root,root,"v1.0.0")


def test_promotion_rechecks_version_digest_and_aliases(monkeypatch):
    calls=[];reads=[]
    monkeypatch.setattr(promote_image,"git",lambda *_:"v1.2.3")
    monkeypatch.setattr(promote_image.checks,"verify_tag",lambda *_:"a"*40)
    monkeypatch.setattr(promote_image.checks,"verify_merged_pr",lambda *_:None)
    monkeypatch.setattr(promote_image.checks,"verify_catalog",lambda *_,**kw:None)
    monkeypatch.setattr(promote_image.checks,"newest_final",lambda *_:True)
    monkeypatch.setattr(promote_image,"inspect_digest",lambda ref:reads.append(ref) or DIGEST)
    monkeypatch.setattr(promote_image.subprocess,"run",lambda args,**kw:calls.append(args))
    promote_image.promote(Path.cwd(),DIGEST,["latest","1","1.2"])
    assert len(reads)==4 and reads[0].endswith(":1.2.3")
    assert calls[0][-1].endswith("@"+DIGEST)
    assert not any("build"==arg or "push"==arg for arg in calls[0])
    calls.clear()
    monkeypatch.setattr(promote_image,"inspect_digest",lambda _:"sha256:"+"b"*64)
    with pytest.raises(ValueError,match="candidate digest"):promote_image.promote(Path.cwd(),DIGEST,["latest"])
    assert not calls
    with pytest.raises(ValueError,match="cannot move stable"):
        promote_image.promote(Path.cwd(),DIGEST,["nightly", "latest"])


@pytest.mark.parametrize("status,stderr",[(0,""),(1,"unauthorized"),(1,"network unreachable")])
def test_candidate_refuses_existing_tags_and_unknown_registry_errors(monkeypatch,status,stderr):
    calls=[]
    def run(args,**kw):
        calls.append(args);return SimpleNamespace(returncode=status,stderr=stderr)
    monkeypatch.setattr(push_candidate.subprocess,"run",run)
    with pytest.raises(ValueError):push_candidate.push([push_candidate.IMAGE+":1.2.3"])
    assert len(calls)==1 and "push" not in calls[0]


def test_candidate_checks_all_tags_before_first_write(monkeypatch):
    calls=[]
    def run(args,**kw):
        calls.append(args);return SimpleNamespace(returncode=1,stderr="manifest unknown")
    monkeypatch.setattr(push_candidate.subprocess,"run",run)
    with pytest.raises(ValueError):push_candidate.push([push_candidate.IMAGE+":1.2.3",push_candidate.IMAGE+":latest"])
    assert len(calls)==1
    calls.clear()
    push_candidate.push([push_candidate.IMAGE+":candidate-public-1"])
    assert [call[1] for call in calls]==["manifest","tag","push","manifest"]


def test_reuse_copies_provenance_without_running_candidate(tmp_path,monkeypatch):
    calls=[]
    monkeypatch.setattr(verify_image,"inspect_digest",lambda _:DIGEST)
    monkeypatch.setattr(verify_image,"verify_record",lambda root,record:None)
    monkeypatch.setattr(verify_image.subprocess,"check_output",lambda args,**kw:calls.append(args) or "container-id")
    def run(args,**kwargs):
        calls.append(args)
        if args[1]=="cp":Path(args[-1]).write_text('{}')
    monkeypatch.setattr(verify_image.subprocess,"run",run)
    verify_image.verify_image(tmp_path,DIGEST)
    assert not any("run"==arg or "start"==arg for call in calls for arg in call)
    assert calls[-1]==["docker","rm","container-id"]


@pytest.mark.parametrize("identity,protected,head,expected", [(7,True,"a"*40,True),(None,True,"a"*40,False),(42,True,"a"*40,False),(7,False,"a"*40,False),(7,True,"b"*40,False)])
def test_cutover_requires_distinct_identity_and_exact_protected_main(tmp_path,monkeypatch,identity,protected,head,expected):
    monkeypatch.setattr(cutover_check.cutover,"git",lambda root,*args:"" if args[0]=="status" else "a"*40)
    monkeypatch.setattr(cutover_check.cutover,"mapping",lambda *_:("root",{"old_repository_id":42}))
    def api(repo,route=""):
        if not route:return {"full_name":c.NEW_REPO,"private":False,"id":identity}
        if route=="branches/main":return {"protected":protected,"commit":{"sha":head}}
        if route=="actions/permissions":return {"enabled":False}
        return {"total_count":0}
    monkeypatch.setattr(cutover_check.cutover,"api",api)
    monkeypatch.setattr(cutover_check,"attest",lambda *_:{"ok":True})
    monkeypatch.setattr(cutover_check.cutover,"container_sequence",lambda *_:1001)
    monkeypatch.setattr(cutover_check,"verify_catalog",lambda *_,**kw:None)
    monkeypatch.setattr(cutover_check,"verify_merged_pr",lambda *_:None)
    args=SimpleNamespace(old_repo=c.OLD_REPO,new_repo=c.NEW_REPO,private_export="a"*40,public_root="b"*40,private_checkout=tmp_path)
    result=cutover_check.checks(ROOT,args)
    assert next(row for row in result if row["check"]=="repository identities")["passed"] is expected
    if expected:assert all(row["passed"] for row in result)


@pytest.mark.parametrize("blocked", [None, "old", "cloud", "public", "public-running", "running", "missing"])
def test_cutover_checks_global_freeze_on_all_repositories(tmp_path,monkeypatch,blocked):
    root=ROOT
    def api(repo,route=""):
        if route=="actions/permissions":
            if blocked=="missing":return {}
            enabled=(blocked=="old" and repo==c.OLD_REPO) or (blocked=="cloud" and repo.endswith("hobbycoders-cloud")) or (blocked=="public" and repo==c.NEW_REPO)
            return {"enabled":enabled}
        if route.startswith("actions/runs?"):
            outstanding = (blocked=="running" and "in_progress" in route) or (blocked=="public-running" and repo==c.NEW_REPO and "status=pending" in route)
            return {"total_count":1 if outstanding else 0}
        return {}
    monkeypatch.setattr(cutover_check.cutover,"api",api)
    monkeypatch.setattr(cutover_check,"attest",lambda *_:{})
    monkeypatch.setattr(cutover_check,"verify_catalog",lambda *_,**kw:None)
    monkeypatch.setattr(cutover_check,"verify_merged_pr",lambda *_:None)
    args=SimpleNamespace(old_repo=c.OLD_REPO,new_repo=c.NEW_REPO,private_export="a"*40,public_root="b"*40,private_checkout=tmp_path)
    checks=cutover_check.checks(root,args)
    result=next(row for row in checks if row["check"]=="all publishers frozen/drained")
    assert result["passed"] is (blocked is None)
    workflow=next(row for row in checks if row["check"]=="guarded image reuse and promotion")
    assert workflow["passed"],workflow


def test_cutover_check_reports_missing_android_pr_without_authorizing(tmp_path,monkeypatch):
    source=yaml.load((ROOT/".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
    del source["jobs"]["android-apk"]
    path=tmp_path/".github/workflows/release.yml";path.parent.mkdir(parents=True);path.write_text(yaml.safe_dump(source))
    monkeypatch.setattr(cutover_check.cutover,"git",lambda *_:"")
    monkeypatch.setattr(cutover_check.cutover,"api",lambda *_: {})
    monkeypatch.setattr(cutover_check,"attest",lambda *_: {})
    monkeypatch.setattr(cutover_check.cutover,"container_sequence",lambda *_: 1)
    monkeypatch.setattr(cutover_check,"verify_catalog",lambda *_,**kw:None)
    monkeypatch.setattr(cutover_check,"verify_merged_pr",lambda *_:None)
    args=SimpleNamespace(old_repo=c.OLD_REPO,new_repo=c.NEW_REPO,private_export="a"*40,public_root="b"*40,private_checkout=tmp_path)
    results=cutover_check.checks(tmp_path,args)
    stage=next(r for r in results if r["check"]=="guarded image reuse and promotion")
    assert not stage["passed"] and "android-apk" in stage["reason"]
    assert any(not row["passed"] for row in results)


def test_prepare_produces_frozen_mapping_and_refuses_low_inventory(tmp_path,monkeypatch):
    import base64
    mapping_path=tmp_path/c.MAPPING
    envelope=json.dumps({"payload":base64.b64encode(json.dumps({"sequence":5000,"container":{"sequence":50}}).encode()).decode(),"signature":"fixture"}).encode()
    cache=tmp_path/"envelope";cache.write_bytes(envelope)
    monkeypatch.setattr(c,"git",lambda *_:"")
    monkeypatch.setattr(c,"mapping",lambda *_:("root",None))
    monkeypatch.setattr(c,"api",lambda repo,route="": {"private":True,"full_name":c.NEW_REPO,"id":42} if not route else {"merged_at":None,"base":{"ref":"main"}})
    monkeypatch.setattr(prepare_cutover.gate,"published_edge_revision",lambda :"a"*40)
    monkeypatch.setattr(prepare_cutover.gate,"edge_cache_path",lambda :cache)
    monkeypatch.setattr(prepare_cutover.gate,"verify_edge_manifest",lambda _:"a"*40)
    with pytest.raises(ValueError,match="Desktop inventory"):prepare_cutover.prepare(tmp_path,9,60,4999)
    with pytest.raises(ValueError,match="Inventory maximum"):prepare_cutover.prepare(tmp_path,9,49,6000)
    assert not mapping_path.exists()
    record=prepare_cutover.prepare(tmp_path,9,60,6000)
    assert json.loads(mapping_path.read_text())==record
    assert record["envelope_sha256"]==hashlib.sha256(envelope).hexdigest()
    assert record["maximum_shipped_sequence"]==60
    assert record["maximum_desktop_sequence"]==6000
    assert (tmp_path/c.OFFSET).read_text()=="60\n"
    assert "private_export" not in record and "public_root" not in record
    with pytest.raises(FileExistsError):prepare_cutover.prepare(tmp_path,9,60,6000)


@pytest.mark.parametrize("requested", [False, True])
@pytest.mark.parametrize("desktop", ["success", "skipped", "failure", "cancelled"])
@pytest.mark.parametrize("failed_job", [None, "docker", "buyer-install", "source-stable", "android-apk", "linux-release"])
def test_promotion_requires_all_artifacts_and_optional_windows(requested,desktop,failed_job):
    workflow=yaml.load((ROOT/".github/workflows/release.yml").read_text(),Loader=yaml.BaseLoader)
    expression=workflow["jobs"]["promote-image"]["if"]
    expression=expression.replace("needs.create-release.outputs.promote_latest", repr("true"))
    for job in ("create-release", "docker", "buyer-install", "source-stable", "android-apk", "linux-release", "desktop"):
        result=desktop if job=="desktop" else ("failure" if job==failed_job else "success")
        expression=expression.replace(f"needs.{job}.result",repr(result))
    expression=expression.replace("inputs.build_desktop",repr(requested))
    expression=expression.replace("always()","True").replace("failure()","False").replace("cancelled()","False")
    expression=expression.replace("&&","and").replace("||","or").replace("!","not ")
    # Only quoted fixture values and boolean operators remain; no names/calls.
    result=eval(expression,{"__builtins__":{}},{})
    assert result == (failed_job is None and (desktop=="success" or (not requested and desktop=="skipped")))


def test_linux_release_is_exact_tag_and_durable():
    workflow=yaml.load((ROOT/".github/workflows/release.yml").read_text(),Loader=yaml.BaseLoader)
    jobs=workflow["jobs"]
    assert jobs["linux-build"]["uses"]=="./.github/workflows/desktop-linux-artifact.yml"
    assert jobs["linux-build"]["with"]["version"]=="${{ needs.preflight.outputs.release_version }}"
    publish=jobs["linux-release"]
    assert {"preflight","create-release","linux-build"} <= set(publish["needs"])
    assert "attach_linux.py --tag" in publish["steps"][-1]["run"]
    assert publish["steps"][0]["with"]["ref"]=="refs/tags/${{ needs.preflight.outputs.release_tag }}"
