"""Opt-in whole-release stage/seal/promote adapter; never used by the live default.

All payloads are public release evidence. Credentials are inherited only, never
arguments or output. Promotion consumes immutable asset IDs and original signed
source bytes, not build outputs from this run. Run from the exact release tag.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.ci import release_staging as protocol
from scripts.ci import release_preflight as preflight
from scripts.ci.public_cutover import mapping, container_sequence
from scripts.release import promote_image

REPO = preflight.REPOSITORY
RECORD = "whole-release-stage.json"


def command(*args, data=None, env=None):
    result = subprocess.run(list(args), input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if result.returncode:
        # No raw stderr: publication tools may echo environment/configuration.
        raise ValueError(f"Release command failed: {args[0]} {args[1]} (exit {result.returncode})")
    return result.stdout


def api(route="", *, body=None, method=None):
    args = ["gh", "api", f"repos/{REPO}" + ("/" + route if route else "")]
    if method:
        args += ["--method", method]
    if body is not None:
        args += ["--input", "-"]
    return json.loads(command(*args, data=protocol.canonical(body) if body is not None else None))


def git(*args):
    return command("git", *args).decode().strip()


def inventory(release_id):
    result = []
    for page in range(1, 101):
        rows = api(f"releases/{release_id}/assets?per_page=100&page={page}")
        if not isinstance(rows, list):
            raise ValueError("Malformed asset inventory")
        result.extend(rows)
        if len(rows) < 100:
            if len({a["name"] for a in result}) != len(result):
                raise ValueError("Duplicate release asset names")
            return result
    raise ValueError("Incomplete asset inventory")


def upload(tag, release_id, file):
    """No --clobber, ever. A retry may only discover identical immutable bytes."""
    expected = file.read_bytes()
    rows = inventory(release_id)
    row = next((a for a in rows if a["name"] == file.name), None)
    if row is None:
        command("gh", "release", "upload", tag, str(file), "--repo", REPO)
        row = next((a for a in inventory(release_id) if a["name"] == file.name), None)
    if row is None:
        raise ValueError("Uploaded asset is missing")
    wanted = {"id": row["id"], "name": file.name, "size": len(expected), "digest": "sha256:" + protocol.sha256(expected)}
    actual = command("gh", "api", f"repos/{REPO}/releases/assets/{row['id']}", "-H", "Accept: application/octet-stream")
    protocol.verify_asset(wanted, row, actual)
    return wanted


def owner_gate(tag):
    protocol.version(tag)
    if (os.environ.get("GITHUB_REPOSITORY") != REPO or os.environ.get("GITHUB_ACTOR_ID") != "76577322"
            or not os.environ.get("GITHUB_ACTOR") or os.environ.get("GITHUB_TRIGGERING_ACTOR") != os.environ["GITHUB_ACTOR"]
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("GITHUB_REF") != "refs/tags/" + tag):
        raise ValueError("Whole-release operations require the owner dispatch from the exact tag")
    metadata = api()
    if (metadata.get("full_name") != REPO or not protocol.positive(metadata.get("id"))
            or str(metadata["id"]) != os.environ.get("GITHUB_REPOSITORY_ID")
            or str(metadata["id"]) != os.environ.get("RELEASE_GITHUB_REPO_ID")):
        raise ValueError("Pinned numeric repository identity mismatch")
    return {"name": REPO, "id": metadata["id"]}


def gates(tag):
    repository = owner_gate(tag)
    command("git", "fetch", "origin", "main", "--tags")
    # A remotely moved tag must never be hidden by an old local tag object.
    remote = git("ls-remote", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
    refs = dict(line.split()[::-1] for line in remote.splitlines())
    revision = preflight.verify_tag(ROOT, tag)
    if refs.get(f"refs/tags/{tag}^{{}}", refs.get(f"refs/tags/{tag}")) != revision:
        raise ValueError("Stale tag: remote revision differs")
    command("git", "diff", "--exit-code", "HEAD", "--")
    preflight.verify_versions(ROOT, tag)
    preflight.verify_catalog(ROOT, remote=True)
    preflight.verify_merged_pr(revision)
    if not preflight.newest_final(ROOT, tag):
        raise ValueError("Obsolete version cannot be staged or promoted")
    from scripts.ci.edge_publication_gate import update_reach_readiness
    ready, _ = update_reach_readiness(revision, root=ROOT)
    if not ready:
        raise ValueError("Current update-reach evidence missing or refused")
    return {"tag": tag, "revision": revision, "tree": git("rev-parse", "HEAD^{tree}"),
            "repository": repository, "newest_tag": tag, "owner": True, "merged_pr": True,
            "exact_tree": True, "update_reach": True}


class Backend:
    def __init__(self, directory):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.public_key = (ROOT / "desktop/build/application-update-key.pem").read_bytes()
        self.cache = {}
        self.play = None

    def sql(self, sql):
        database = os.environ.get("ACCOUNT_HUB_DATABASE_ID", "")
        if not re.fullmatch(r"[a-f0-9-]{36}", database):
            raise ValueError("D1 database configuration required")
        config = self.directory / "staging-d1.json"
        config.write_text(json.dumps({"d1_databases": [{"binding": "DB", "database_name": "release-metadata", "database_id": database}]}))
        env = {k: v for k, v in os.environ.items() if k not in {"DESKTOP_APPLICATION_SIGNING_KEY", "GH_TOKEN", "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON"}}
        try:
            response = json.loads(command("node", "scripts/source-publisher/node_modules/wrangler/bin/wrangler.js",
                "d1", "execute", "DB", "--remote", "--config", str(config), "--command", sql, "--json", env=env))
            if not isinstance(response, list) or any(r.get("success") is False for r in response):
                raise ValueError("D1 operation did not succeed")
            return [row for result in response for row in result.get("results", [])]
        finally:
            config.unlink(missing_ok=True)

    def current(self, record):
        state = gates(record["tag"])
        release = api("releases/tags/" + record["tag"])
        state["release_id"] = release["id"]
        present = {a["id"] for a in inventory(release["id"])}
        if any(a["id"] not in present for a in record.get("assets", [])):
            raise ValueError("Staged asset missing from its release")
        state["sources"] = self.sql("SELECT revision,channel,version,manifest,sequence,asset_id,release_repository,release_repository_id,withdrawn_at FROM source_releases;")
        state["installers"] = self.sql("SELECT version,revision,release_repository_id FROM desktop_installers UNION SELECT version,revision,release_repository_id FROM desktop_platform_installers;")
        _, baseline = mapping(ROOT, state["revision"])
        state["historical_source_floor"] = baseline["maximum_desktop_sequence"] if baseline else 0
        state["historical_image_floor"] = baseline["maximum_shipped_sequence"] if baseline else 0
        state["installer_floor"] = int(git("show", "-s", "--format=%ct", state["revision"])) * 1000
        for row in state["installers"]:
            if baseline and row["release_repository_id"] == baseline["old_repository_id"]:
                continue  # frozen historical inventory is already included above
            if row["release_repository_id"] != state["repository"]["id"] or not re.fullmatch(r"[a-f0-9]{40}", row["revision"]):
                raise ValueError("Unknown installer baseline provenance")
            state["installer_floor"] = max(state["installer_floor"], int(git("show", "-s", "--format=%ct", row["revision"])) * 1000)
        return state

    def asset(self, asset_id):
        row = api(f"releases/assets/{asset_id}")
        data = command("gh", "api", f"repos/{REPO}/releases/assets/{asset_id}", "-H", "Accept: application/octet-stream")
        self.cache[asset_id] = data
        return row, data

    def file(self, record, name):
        asset = next(a for a in record["assets"] if a["name"] == name)
        path = self.directory / name
        path.write_bytes(self.cache[asset["id"]])
        return path

    def verify_image(self, digest):
        from scripts.release.verify_image import verify_image
        verify_image(ROOT, digest)

    def verify_provenance(self, record):
        def metadata(name):
            return json.loads(self.file(record, name).read_bytes())
        android = metadata("android-build.json")
        linux = metadata("linux-build-provenance.json")
        if (android.get("commit") != record["revision"] or android.get("versionName") != record["tag"][1:]
                or linux.get("sourceCommit") != record["revision"] or linux.get("version") != record["tag"][1:]):
            raise ValueError("Platform build provenance does not match the tag")
        by_name = {a["name"]: a for a in record["assets"]}
        for artifact in linux["artifacts"]:
            asset = by_name[artifact["name"]]
            if asset["digest"] != "sha256:" + artifact["sha256"] or asset["size"] != artifact["bytes"]:
                raise ValueError("Linux provenance does not match artifact bytes")
        if record["windows"] and metadata("desktop-build.json").get("revision") != record["revision"]:
            raise ValueError("Windows provenance does not match the tag")
        source = protocol.verify_signed(record["source"]["manifest"], self.public_key)
        if source["container"]["sequence"] != container_sequence(ROOT, record["revision"]):
            raise ValueError("Signed image sequence is not the committed sequence")

    def matches(self, sink, record):
        if sink == "source":
            rows = self.sql("SELECT manifest,asset_id,release_repository_id,withdrawn_at FROM source_releases WHERE channel='stable' AND revision=" + quote(record["revision"]) + ";")
            return bool(rows and rows[0]["manifest"] == record["source"]["manifest"] and rows[0]["asset_id"] == record["source"]["asset"]["id"] and rows[0]["release_repository_id"] == record["repository"]["id"] and rows[0]["withdrawn_at"] is None)
        if sink == "installer":
            asset = next(a for a in record["assets"] if a["name"].endswith(".exe"))
            rows = self.sql("SELECT version,revision,asset_id,sha256,release_repository_id FROM desktop_installers WHERE id=1;")
            return bool(rows and rows[0] == {"version": record["tag"][1:], "revision": record["revision"], "asset_id": asset["id"], "sha256": asset["digest"][7:], "release_repository_id": record["repository"]["id"]})
        if sink == "docker":
            return all(alias_digest(alias) == record["image_digest"] for alias in aliases(record))
        if sink == "release":
            release = api("releases/tags/" + record["tag"])
            return not release["draft"] and api("releases/latest")["id"] == record["release_id"]
        if sink == "play":
            return self.play.matches(record)
        raise ValueError("Unknown activation sink")

    def write(self, sink, record):
        if sink == "source":
            source = protocol.verify_signed(record["source"]["manifest"], self.public_key)
            asset = record["source"]["asset"]
            # Conditional insert includes ALL historical/withdrawn rows. Conflict
            # never updates an existing signed sequence or clears a withdrawal.
            values = [quote(record["revision"]), "'stable'", quote(record["tag"][1:]), str(asset["id"]), str(asset["size"]),
                      quote(record["source"]["manifest"]), str(source["sequence"]), quote(REPO), str(record["repository"]["id"])]
            self.sql("INSERT INTO source_releases(revision,channel,version,asset_id,size,manifest,sequence,release_repository,release_repository_id) SELECT "
                     + ",".join(values) + f" WHERE {source['sequence']} > COALESCE((SELECT MAX(sequence) FROM source_releases),0) ON CONFLICT(revision,channel) DO NOTHING;")
        elif sink == "installer":
            asset = next(a for a in record["assets"] if a["name"].endswith(".exe"))
            old = self.sql("SELECT version,revision,asset_id,sha256,release_repository_id FROM desktop_installers WHERE id=1;")
            if old and protocol.version("v" + old[0]["version"]) >= protocol.version(record["tag"]):
                raise ValueError("Equal/newer installer identity changed; refusing overwrite")
            values = ["1", quote(record["tag"][1:]), quote(record["revision"]), str(asset["id"]), str(asset["size"]),
                      quote(asset["digest"][7:]), quote(REPO), str(record["repository"]["id"])]
            if old:
                expected = old[0]
                where = " AND ".join(key + "=" + (str(value) if type(value) is int else quote(value)) for key, value in expected.items())
                assignments = ",".join(key + "=" + value for key, value in zip(
                    ("id", "version", "revision", "asset_id", "size", "sha256", "release_repository", "release_repository_id"), values))
                self.sql("UPDATE desktop_installers SET " + assignments + " WHERE id=1 AND " + where + ";")
            else:
                self.sql("INSERT INTO desktop_installers(id,version,revision,asset_id,size,sha256,release_repository,release_repository_id) VALUES("
                         + ",".join(values) + ") ON CONFLICT(id) DO NOTHING;")
            # R2 is an optional versioned cache; the hub validates its hash and
            # falls back to these exact GitHub bytes. No cache copy is necessary.
        elif sink == "docker":
            promote_image.promote(ROOT, record["image_digest"], aliases(record))
        elif sink == "release":
            api(f"releases/{record['release_id']}", method="PATCH", body={"draft": False, "make_latest": "true"})
        elif sink == "play":
            self.play.write(record, self.file(record, "app-release.aab"))
        else:
            raise ValueError("Unknown activation sink")


def quote(value):
    return "'" + value.replace("'", "''") + "'"


def alias_digest(alias):
    result = subprocess.run(["docker", "buildx", "imagetools", "inspect", f"{promote_image.IMAGE}:{alias}",
                             "--format", "{{.Manifest.Digest}}"], capture_output=True, text=True)
    if result.returncode:
        if "manifest unknown" in result.stderr.lower() or "not found" in result.stderr.lower():
            return None
        raise ValueError("Docker alias read failed; no activation attempted")
    digest = result.stdout.strip()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError("Invalid Docker alias digest")
    return digest


def aliases(record):
    parts = record["tag"][1:].split(".")
    return ["latest", parts[0], ".".join(parts[:2])]


def attestation(path, tag, revision):
    command("gh", "attestation", "verify", str(path), "--repo", REPO,
            "--signer-workflow", REPO + "/.github/workflows/release.yml",
            "--source-ref", "refs/tags/" + tag, "--source-digest", revision,
            "--signer-digest", revision, "--deny-self-hosted-runners")


def begin(tag):
    state = gates(tag)
    # Search without converting API failures into 'not found'.
    found = None
    for page in range(1, 101):
        rows = api(f"releases?per_page=100&page={page}")
        found = next((r for r in rows if r["tag_name"] == tag), None)
        if found or len(rows) < 100:
            break
    else:
        raise ValueError("Incomplete release inventory")
    if found:
        if not found["draft"] or any(a["name"] == RECORD for a in inventory(found["id"])):
            raise ValueError("Release already sealed/live; use promote, never rebuild or restage")
    else:
        found = api("releases", method="POST", body={"tag_name": tag, "target_commitish": state["revision"],
                    "name": "AI Shuffle " + tag + " (staged)", "draft": True, "make_latest": "false"})
    saved = api(f"releases/{found['id']}")
    if not saved["draft"] or saved["tag_name"] != tag:
        raise ValueError("Draft release read-back failed")


def stage(args, backend):
    state = gates(args.tag)
    release = api("releases/tags/" + args.tag)
    if not release["draft"] or any(a["name"] == RECORD for a in inventory(release["id"])):
        raise ValueError("Cannot replace a sealed or live release")
    from scripts.release.attach_linux import verified_files
    files = verified_files(args.artifacts / "linux", args.tag, state["revision"])
    files += [args.artifacts / "apk" / name for name in ("AI-Shuffle-Android.apk", "AI-Shuffle-Android.apk.sha256", "android-build.json")]
    files += [args.artifacts / "play" / name for name in ("app-release.aab", "signing-certificate.pem")]
    assets = [upload(args.tag, release["id"], path) for path in files]
    if args.windows:
        assets += [protocol.asset_identity(a) for a in inventory(release["id"]) if a["name"].endswith(".exe") or a["name"] in {"SHA256SUMS", "desktop-build.json"}]
    evidence = json.loads(os.environ["STAGING_JOB_RESULTS"])
    if not args.windows and evidence.pop("windows", None) != "skipped":
        raise ValueError("Unexpected Windows staging job result")
    record = {key: state[key] for key in ("tag", "revision", "tree", "repository")}
    record.update(kind=protocol.KIND, release_id=release["id"], windows=args.windows, assets=assets,
                  source=json.loads((args.artifacts / "source" / "source-evidence.json").read_bytes()),
                  image_digest=args.image_digest, evidence=evidence,
                  play={"version_code": args.play_code, "asset_id": next(a["id"] for a in assets if a["name"] == "app-release.aab")})
    envelope = protocol.sign_record(record, os.environ["DESKTOP_APPLICATION_SIGNING_KEY"].encode())
    protocol.verify_all(envelope, backend)
    args.record.write_bytes(envelope)


def load(args, backend):
    release = api("releases/tags/" + args.tag)
    row = next((a for a in inventory(release["id"]) if a["name"] == RECORD), None)
    if row is None:
        raise ValueError("Missing signed staging record; live release is not whole-release staging")
    actual, envelope = backend.asset(row["id"])
    protocol.verify_asset(protocol.asset_identity(row), actual, envelope)
    args.record.write_bytes(envelope)
    attestation(args.record, args.tag, preflight.verify_tag(ROOT, args.tag))
    return envelope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("begin", "stage", "seal", "promote", "play"))
    parser.add_argument("--tag", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--record", type=Path)
    parser.add_argument("--image-digest")
    parser.add_argument("--play-code", type=int)
    parser.add_argument("--windows", action="store_true")
    args = parser.parse_args()
    args.directory = args.directory.resolve()
    args.record = args.record or args.directory / RECORD
    backend = Backend(args.directory)
    if args.action == "begin":
        begin(args.tag)
    elif args.action == "stage":
        stage(args, backend)
    elif args.action == "seal":
        envelope = args.record.read_bytes()
        record = protocol.verify_all(envelope, backend)
        attestation(args.record, args.tag, record["revision"])
        upload(args.tag, record["release_id"], args.record)
    else:
        # Neither promotion path reads the private signing key.
        envelope = load(args, backend)
        if args.action == "play":
            from scripts.ci.release_staging_play import Play
            backend.play = Play()
        protocol.activate(envelope, backend, play_only=args.action == "play")
    print("PASS whole-release " + args.action + ": " + args.tag)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never emit HTTP bodies, credentials or configuration in a failure log.
        print("::error::" + (str(error) if isinstance(error, ValueError) else type(error).__name__), file=sys.stderr)
        sys.exit(1)
