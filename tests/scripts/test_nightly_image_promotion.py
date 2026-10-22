"""Nightly promotion keeps stable aliases isolated and reuses the exact-main gate."""
from pathlib import Path

import pytest

from scripts.ci import edge_publication_gate as gate
from scripts.release import promote_image, verify_image

DIGEST = "sha256:" + "a" * 64
REVISION = "b" * 40


@pytest.fixture
def lane(monkeypatch):
    state = {"scenario": "success", "writes": [], "verified": [], "reads": [], "branches": 0}

    def git(root, *args):
        if args == ("status", "--porcelain"):
            return "dirty" if state["scenario"] == "dirty" else ""
        if args == ("rev-parse", "HEAD"):
            return REVISION
        if args == ("rev-parse", "origin/main"):
            return "c" * 40 if state["scenario"] == "other_checkout" else REVISION
        pytest.fail(f"Nightly must not need a final release tag: {args}")

    def api(route):
        scenario = state["scenario"]
        if route == "branches/main":
            state["branches"] += 1
            moved = scenario == "superseded" or (scenario == "moved_during" and state["branches"] > 1)
            return {"protected": scenario != "unprotected", "commit": {"sha": "c" * 40 if moved else REVISION}}
        if route.startswith("actions/workflows/main-suite.yml/runs?"):
            return {"workflow_runs": [{"id": 41, "head_branch": "main", "event": "push"}]
                    if scenario == "hosted" else []}
        if route == "":
            return {"id": 42, "full_name": gate.REPOSITORY}
        if route == "actions/workflows/main-suite.yml":
            return {"id": 7, "path": gate.MAIN_SUITE_PATH}
        if route == "actions/runs/41":
            return {"id": 41, "workflow_id": 7, "path": gate.MAIN_SUITE_PATH,
                    "head_sha": REVISION, "head_branch": "main", "event": "push", "run_attempt": 1,
                    "status": "completed", "conclusion": "success",
                    "repository": {"id": 42, "full_name": gate.REPOSITORY},
                    "head_repository": {"id": 42, "full_name": gate.REPOSITORY}}
        if route.startswith("actions/runs/41/attempts/1/jobs?"):
            return {"jobs": [{"name": gate.MAIN_SUITE_AGGREGATE, "status": "completed", "conclusion": "success"}]}
        assert route.startswith(f"commits/{REVISION}/statuses?")
        if scenario in ("missing", "hosted"):
            return []
        return [{"context": gate.CONTEXT, "state": "failure" if scenario == "failed" else "success",
                 "creator": {"id": "wrong-owner" if scenario == "wrong_owner" else gate.OWNER_ID}}]

    def image(root, digest):
        state["verified"].append(digest)
        if state["scenario"] == "wrong_image":
            raise ValueError("Image provenance does not match exact revision/tree/sequence")

    def inspect(ref):
        state["reads"].append(ref)
        assert ref == promote_image.IMAGE + ":nightly"
        return "sha256:" + "c" * 64 if state["scenario"] == "bad_readback" else DIGEST

    monkeypatch.setattr(promote_image, "git", git)
    monkeypatch.setattr(gate, "GitHub", lambda: api)
    monkeypatch.setattr(verify_image, "verify_image", image)
    monkeypatch.setattr(promote_image, "inspect_digest", inspect)
    monkeypatch.setattr(promote_image.subprocess, "run", lambda args, **kw: state["writes"].append(args))
    return state


@pytest.mark.parametrize("evidence", ["success", "hosted"])
def test_accepted_nightly_requires_no_final_tag_and_only_moves_nightly(lane, evidence):
    lane["scenario"] = evidence
    promote_image.promote(Path.cwd(), DIGEST, ["nightly"])
    assert lane["verified"] == [DIGEST]
    assert lane["branches"] == 2  # Existing gate rechecks current main after status lookup.
    assert lane["writes"] == [["docker", "buildx", "imagetools", "create", "--prefer-index=false",
                               "--tag", promote_image.IMAGE + ":nightly", promote_image.IMAGE + "@" + DIGEST]]
    assert lane["reads"] == [promote_image.IMAGE + ":nightly"]


@pytest.mark.parametrize("scenario", ["dirty", "other_checkout", "missing", "failed", "wrong_owner",
                                      "unprotected", "superseded", "moved_during", "wrong_image"])
def test_nightly_refuses_missing_or_mismatched_acceptance_evidence(lane, scenario):
    lane["scenario"] = scenario
    with pytest.raises(ValueError):
        promote_image.promote(Path.cwd(), DIGEST, ["nightly"])
    assert not lane["writes"]


@pytest.mark.parametrize("tags", [["nightly", "latest"], ["1", "nightly"], ["nightly", "1.2"], ["nightly", "nightly"]])
def test_nightly_cannot_be_mixed_with_stable_aliases(lane, tags):
    with pytest.raises(ValueError, match="cannot move stable"):
        promote_image.promote(Path.cwd(), DIGEST, tags)
    assert not lane["writes"] and not lane["verified"]


def test_nightly_readback_failure_is_not_success(lane):
    lane["scenario"] = "bad_readback"
    with pytest.raises(ValueError, match="read-back"):
        promote_image.promote(Path.cwd(), DIGEST, ["nightly"])
    assert len(lane["writes"]) == 1


def test_nightly_refuses_malformed_digest_before_any_lookup(lane):
    with pytest.raises(ValueError, match="complete sha256"):
        promote_image.promote(Path.cwd(), "sha256:short", ["nightly"])
    assert not lane["writes"] and not lane["verified"] and not lane["branches"]
