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
        assert route.startswith(f"commits/{REVISION}/statuses?")
        if scenario == "missing":
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


def test_accepted_nightly_requires_no_final_tag_and_only_moves_nightly(lane):
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
