"""Signed whole-release evidence and fail-closed, read-back-driven activation.

This module has no network or credential access. The adapter supplies CURRENT
state on every attempt; a saved progress flag is never evidence of activation.
The caller must serialize writers (release.yml and desktop-source-publish).
"""
from __future__ import annotations

import base64
import hashlib
import json
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

KIND = "ai-shuffle/whole-release-stage/v1"
REQUIRED = {"docker", "buyer", "source", "linux", "apk", "play"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def sign_record(record, private_pem):
    key = serialization.load_pem_private_key(private_pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Ed25519 staging key required")
    payload = canonical(record)
    return canonical({"payload": base64.b64encode(payload).decode(),
                      "signature": base64.b64encode(key.sign(payload)).decode()})


def verify_signed(envelope, public_pem):
    signed = json.loads(envelope)
    payload = base64.b64decode(signed["payload"], validate=True)
    signature = base64.b64decode(signed["signature"], validate=True)
    for block in re.findall(rb"-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----", public_pem, re.S):
        key = serialization.load_pem_public_key(block)
        if not isinstance(key, Ed25519PublicKey):
            continue
        try:
            key.verify(signature, payload)
            return json.loads(payload)
        except InvalidSignature:
            pass
    raise ValueError("Invalid signature or untrusted staging/source key")


def version(tag):
    if not isinstance(tag, str) or not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag):
        raise ValueError("A final vX.Y.Z tag is required")
    return tuple(map(int, tag[1:].split(".")))


def positive(value):
    return type(value) is int and 0 < value < 2**53


def asset_identity(row):
    return {key: row[key] for key in ("id", "name", "size", "digest")}


def verify_asset(expected, actual, data):
    if (not positive(expected["id"]) or not positive(expected["size"])
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", expected["name"])
            or actual.get("state") != "uploaded"
            or asset_identity(actual) != expected
            or len(data) != expected["size"] or "sha256:" + sha256(data) != expected["digest"]):
        raise ValueError("Artifact identity or bytes substituted")


def validate(record, current, public_pem):
    """Validate authenticated intent against fresh gates, tags and sequence floors."""
    if record.get("kind") != KIND:
        raise ValueError("Not a whole-release staging record")
    for field in ("tag", "revision", "tree", "repository", "release_id"):
        if record.get(field) != current.get(field):
            raise ValueError(f"Stale or substituted {field}")
    if not positive(record["repository"]["id"]) or not positive(record["release_id"]):
        raise ValueError("Numeric repository/release provenance required")
    if not re.fullmatch(r"[a-f0-9]{40}", record["revision"]) or not re.fullmatch(r"[a-f0-9]{40}", record["tree"]):
        raise ValueError("Exact revision/tree required")
    if version(record["tag"]) < version(current["newest_tag"]):
        raise ValueError("Obsolete version cannot be promoted")
    if not all(current.get(gate) is True for gate in ("owner", "merged_pr", "exact_tree", "update_reach")):
        raise ValueError("Current owner/merged-PR/exact-tree/update-reach evidence missing")
    if type(record.get("windows")) is not bool:
        raise ValueError("Windows intent missing")
    required = REQUIRED | ({"windows"} if record["windows"] else set())
    if set(record.get("evidence", {})) != required or any(record["evidence"][key] != "success" for key in required):
        raise ValueError("Missing successful build/verification evidence")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", record.get("image_digest", "")):
        raise ValueError("Missing immutable image digest")
    source = verify_signed(record["source"]["manifest"], public_pem)
    if (source.get("format") != 2 or source.get("revision") != record["revision"]
            or source.get("version") != record["tag"][1:] or source.get("channel") != "stable"
            or not positive(source.get("sequence")) or not positive(source.get("container", {}).get("sequence"))):
        raise ValueError("Source signed bytes do not describe this stable release")
    asset = record["source"]["asset"]
    if asset["digest"] != "sha256:" + source["sha256"] or asset["size"] != source["size"]:
        raise ValueError("Source archive does not match its signed bytes")
    if source["sequence"] <= max(current["installer_floor"], current["historical_source_floor"]):
        raise ValueError("Source is below the installer/historical sequence floor")
    if source["container"]["sequence"] <= current["historical_image_floor"]:
        raise ValueError("Image is below the historical sequence floor")
    for row in current["sources"]:
        same = row["revision"] == record["revision"] and row["channel"] == "stable"
        if same:
            if (row.get("withdrawn_at") is not None or row["manifest"] != record["source"]["manifest"]
                    or row["asset_id"] != asset["id"] or row["release_repository_id"] != record["repository"]["id"]
                    or row["release_repository"] != record["repository"]["name"]):
                raise ValueError("Existing source was withdrawn or substituted")
        elif row["sequence"] >= source["sequence"]:
            # Includes withdrawn rows. Removing a bad release never lowers floors.
            raise ValueError("Source sequence is obsolete (including withdrawn deliveries)")
        if row.get("withdrawn_at") is not None:
            prior = json.loads(base64.b64decode(json.loads(row["manifest"])["payload"], validate=True))
            if prior["container"]["sequence"] >= source["container"]["sequence"]:
                raise ValueError("Image is below a withdrawn sequence floor")
    for installer in current["installers"]:
        if version("v" + installer["version"]) > version(record["tag"]):
            raise ValueError("A newer installer is already active")
    assets = record.get("assets", [])
    names = [a["name"] for a in assets]
    if len(names) != len(set(names)) or len({a["id"] for a in assets}) != len(assets):
        raise ValueError("Duplicate artifact identity")
    required_names = {"AI-Shuffle-Android.apk", "AI-Shuffle-Android.apk.sha256", "android-build.json",
                      "app-release.aab", "signing-certificate.pem", "linux-build-provenance.json", "SHA256SUMS-linux"}
    if not required_names <= set(names) or sum(n.endswith(".AppImage") for n in names) != 1 or sum(n.endswith(".deb") for n in names) != 1:
        raise ValueError("Missing durable platform artifacts")
    if record["windows"] and (sum(n.endswith(".exe") for n in names) != 1 or not {"desktop-build.json", "SHA256SUMS"} <= set(names)):
        raise ValueError("Missing Windows evidence")
    if not record["windows"] and any(n.endswith(".exe") for n in names):
        raise ValueError("Unrequested installer")
    if not positive(record["play"]["version_code"]) or record["play"]["asset_id"] != next(a["id"] for a in assets if a["name"] == "app-release.aab"):
        raise ValueError("Play bundle identity missing")
    return source


def verify_all(envelope, backend):
    record = verify_signed(envelope, backend.public_key)
    validate(record, backend.current(record), backend.public_key)
    for asset in [*record["assets"], record["source"]["asset"]]:
        actual, data = backend.asset(asset["id"])
        verify_asset(asset, actual, data)
    backend.verify_image(record["image_digest"])
    backend.verify_provenance(record)
    return record


def activate(envelope, backend, *, play_only=False):
    """Resume from the destinations themselves, even after write/ack loss.

    No rebuild, signing, sequence allocation or mutable progress journal here.
    A sink write MUST be conditional/idempotent; every write is read back.
    """
    record = verify_all(envelope, backend)
    sinks = ["play"] if play_only else ["source", "installer", "docker", "release"]
    for sink in sinks:
        if sink == "installer" and not record["windows"]:
            continue
        validate(record, backend.current(record), backend.public_key)
        if not backend.matches(sink, record):
            backend.write(sink, record)
        if not backend.matches(sink, record):
            raise ValueError(f"{sink} activation read-back failed; retry the same promotion")
    return record
