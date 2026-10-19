"""Whether a worker on nightly can go back to stable without running older code.

Going back is safe when the code this worker runs is not newer than the
current stable release: the same revision, or an earlier point of main. Then
every later stable release is newer code again and migrations only ever run
forwards. Going back from code that is ahead of stable is refused: the stable
feed would offer older code over a database that newer code already migrated.

Signed manifests order main by ``container.sequence``, the main-history
ordinal Docker workers already compare (docker/application_updates.py). The
desktop's own ``sequence`` is a signing time and says nothing about which code
is older. A worker whose ordinal is unknown (a desktop still running the build
its installer bundled) falls back to its version: a final X.Y.Z is a tagged
stable release, and stable releases only move forward along main.

This verdict decides whether the API accepts the switch. What installs code
enforces it again: the desktop shell re-verifies the signed stable manifest it
is handed (desktop/src/main/channel-return.ts) and the container supervisor
never installs a lower container sequence. So the manifest is parsed here but
its signature is left to them.
"""
import base64
import json
import os
from pathlib import Path
import re

# docker/application_updates.py ORIGIN and desktop/src/main/source-auth.ts SOURCE_ORIGIN.
STABLE_FEED = "https://hobbycoders.com/v1/desktop/manifest"
LEASE_HEADER = "X-Shuffle-Lease"
MAX_MANIFEST = 32768
REVISION = re.compile(r"[a-f0-9]{40}")
FINAL = re.compile(r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})")

AHEAD = ("This worker runs code that is newer than the current stable release{release}, so it cannot "
         "switch back to stable yet: nightly may already have changed how its data is stored. It can "
         "switch once a stable release catches up with the version it runs.")
UNKNOWN = ("Cannot tell whether this worker's version is older than the current stable release{release}, "
           "so it stays on nightly. Install the next update, then try again.")


class Refused(ValueError):
    """The switch back to stable is not safe or cannot be checked; the message says why."""


def sequence_of(value):
    return value if type(value) is int and 0 < value < 2**53 else None


def release_fields(text):
    """revision, channel, version and container sequence of a signed manifest envelope."""
    if len(text) > MAX_MANIFEST:
        raise ValueError("Source manifest exceeds limits")
    envelope = json.loads(text)
    payload = json.loads(base64.b64decode(envelope["payload"], validate=True))
    if not isinstance(payload, dict) or not isinstance(payload.get("revision"), str) or not REVISION.fullmatch(payload["revision"]):
        raise ValueError("Invalid source manifest")
    container = payload.get("container")
    version = payload.get("version")
    return {"revision": payload["revision"],
            "channel": "nightly" if payload.get("channel") == "edge" else payload.get("channel"),
            "version": version if isinstance(version, str) else None,
            "sequence": sequence_of(container.get("sequence")) if isinstance(container, dict) else None}


def fetch_stable():
    """The stable feed's signed manifest text and its fields; raises Refused when unavailable."""
    import httpx
    from app.core.license import lease_header

    lease = lease_header()
    try:
        # The lease goes only to the hub's own update endpoint; redirects are refused.
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=15) as client:
            response = client.get(STABLE_FEED, headers={LEASE_HEADER: lease} if lease else {})
    except httpx.HTTPError as exc:
        raise Refused("Could not reach the update feed to compare this worker with the current stable "
                      "release. Try again later.") from exc
    if response.status_code != 200:
        raise Refused(f"The stable update feed did not answer ({response.status_code}), so this worker "
                      "cannot be compared with the current stable release. Try again later.")
    try:
        text = response.content.decode("utf-8")
        stable = release_fields(text)
    except (ValueError, KeyError, TypeError) as exc:
        raise Refused("The stable update feed returned an unreadable release. Try again later.") from exc
    if stable["channel"] != "stable":
        raise Refused("The update feed did not return a stable release. Try again later.")
    return text, stable


def _final(version):
    match = FINAL.fullmatch(version or "")
    return tuple(int(part) for part in match.groups()) if match else None


def refusal(installed, stable):
    """None when ``installed`` (revision, sequence, version) is not ahead of ``stable``; else why."""
    if installed.get("revision") == stable["revision"]:
        return None
    release = f" ({stable['version']})" if stable.get("version") else ""
    mine, theirs = sequence_of(installed.get("sequence")), stable.get("sequence")
    if mine is not None and theirs is not None:
        return None if mine < theirs else AHEAD.format(release=release)
    a, b = _final(installed.get("version")), _final(stable.get("version"))
    if a and b:
        return None if a <= b else AHEAD.format(release=release)
    return UNKNOWN.format(release=release)


def container_installed(root, app_version=None):
    """revision, sequence and version of the code this container runs.

    Raises Refused while an update is staged or starting: that build, not the
    running one, is what would run next."""
    try:
        state = json.loads((Path(root) / "state.json").read_text())
    except FileNotFoundError:
        state = {}
    except (OSError, ValueError) as exc:
        raise Refused("Cannot read this worker's update state, so it stays on nightly.") from exc
    if not isinstance(state, dict):
        raise Refused("Cannot read this worker's update state, so it stays on nightly.")
    if state.get("pending") or state.get("booting") or state.get("blocked"):
        raise Refused("Finish the current update before changing channel")
    revision = os.environ.get("GIT_SHA", "")
    if not REVISION.fullmatch(revision):
        return {"revision": None, "sequence": None, "version": app_version}
    try:
        fields = release_fields((Path(root) / "versions" / revision / "release.json").read_text())
        if fields["revision"] == revision:
            return {"revision": revision, "sequence": fields["sequence"], "version": fields["version"] or app_version}
    except (OSError, ValueError, KeyError, TypeError):
        pass  # The image's own code runs: its image.json says where it sits on main.
    image_dir = os.environ.get("SHUFFLE_IMAGE_DIR") or str(Path(__file__).resolve().parents[2] / "docker")
    try:
        image = json.loads((Path(image_dir) / "image.json").read_text())
        if isinstance(image, dict) and image.get("revision") == revision:
            return {"revision": revision, "sequence": sequence_of(image.get("sequence")), "version": app_version}
    except (OSError, ValueError):
        pass
    return {"revision": revision, "sequence": None, "version": app_version}
