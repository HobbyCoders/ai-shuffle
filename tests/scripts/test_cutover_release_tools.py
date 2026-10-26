"""Adversarial external-boundary tests for release tools."""
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from scripts.release import attach_linux, promote_image, push_candidate, verify_image, release_wiring

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


def test_release_wiring_reports_missing_android_caller(tmp_path):
    source=yaml.load((ROOT/".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
    del source["jobs"]["android-apk"]
    path=tmp_path/".github/workflows/release.yml";path.parent.mkdir(parents=True);path.write_text(yaml.safe_dump(source))
    with pytest.raises(ValueError,match="android-apk"):release_wiring.verify(tmp_path)


def test_release_wiring_accepts_the_real_workflows():
    assert "digest reuse" in release_wiring.verify(ROOT)


@pytest.mark.parametrize("desktop", ["success", "skipped", "failure", "cancelled"])
@pytest.mark.parametrize("needed", [True, False])
@pytest.mark.parametrize("failed_job", [None, "docker", "buyer-install", "source-stable", "android-apk", "linux-release", "windows-policy"])
def test_promotion_requires_all_artifacts_or_verified_windows_skip(desktop,needed,failed_job):
    workflow=yaml.load((ROOT/".github/workflows/release.yml").read_text(),Loader=yaml.BaseLoader)
    expression=workflow["jobs"]["promote-image"]["if"]
    expression=expression.replace("needs.create-release.outputs.promote_latest", repr("true"))
    expression=expression.replace("needs.windows-policy.outputs.build", repr(str(needed).lower()))
    for job in ("create-release", "docker", "buyer-install", "source-stable", "android-apk", "linux-release", "desktop", "windows-policy"):
        result=desktop if job=="desktop" else ("failure" if job==failed_job else "success")
        expression=expression.replace(f"needs.{job}.result",repr(result))
    expression=expression.replace("always()","True").replace("failure()","False").replace("cancelled()","False")
    expression=expression.replace("&&","and").replace("||","or").replace("!","not ")
    # Only quoted fixture values and boolean operators remain; no names/calls.
    result=eval(expression,{"__builtins__":{}},{})
    assert result == (failed_job is None and (desktop=="success" or (desktop=="skipped" and not needed)))


def test_linux_release_is_exact_tag_and_durable():
    workflow=yaml.load((ROOT/".github/workflows/release.yml").read_text(),Loader=yaml.BaseLoader)
    jobs=workflow["jobs"]
    assert jobs["linux-build"]["uses"]=="./.github/workflows/desktop-linux-artifact.yml"
    assert jobs["linux-build"]["with"]["version"]=="${{ needs.preflight.outputs.release_version }}"
    publish=jobs["linux-release"]
    assert {"preflight","create-release","linux-build"} <= set(publish["needs"])
    assert "attach_linux.py --tag" in publish["steps"][-1]["run"]
    assert publish["steps"][0]["with"]["ref"]=="refs/tags/${{ needs.preflight.outputs.release_tag }}"
