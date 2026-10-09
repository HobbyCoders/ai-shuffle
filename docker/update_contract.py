"""What a Docker image can run: the image/source contract (#1821).

An image bakes what it PROVIDES into /app/docker/image.json when it is built
(Dockerfile). publish-source.mjs signs what a source revision REQUIRES into its
update manifest (``container.requires`` and ``container.runtime``). The updater
in the image (docker/application_updates.py) takes an update only when the image
provides everything the source requires; otherwise it shows "Needs a newer
Docker image" and the running version keeps working.

Until #1821 the contract was a hash of eight whole files, so any edit to them,
even a comment, made every older image refuse every later update. Images built
before the change still compare ``container.runtime`` strings and nothing else.
LEGACY_RUNTIME is that hash as it stood when the scheme changed, frozen as the
name of base 1: while a release requires nothing beyond base 1 it publishes
this string, and those images keep taking it.

When to change this file:

- The source starts to depend on something only a newer image has (a newer
  Python, Node or PostgreSQL, a new system package, a supervisor change the app
  relies on on images that run their own supervisor): add a capability to
  CAPABILITIES and PROVIDES, add it to REQUIRES, and say so in the PR. Every
  older image then shows "Needs a newer Docker image" for that release. To warn
  users one or more releases ahead, put it in RECOMMENDS first.
- You changed what the image bakes in (IMAGE_INPUTS: the Dockerfile, the root
  entrypoint, the CLI installer it runs) without the source depending on it:
  run ``python docker/update_contract.py --pin`` and commit. Older images keep
  taking updates; they get your change with their next image.

Usage::

    python docker/update_contract.py                 # the runtime string to publish
    python docker/update_contract.py --manifest      # container fields to sign, as JSON
    python docker/update_contract.py ROOT REVISION SEQUENCE   # image.json (image build)
    python docker/update_contract.py --pin           # record IMAGE_INPUTS after a change
    python docker/update_contract.py --check         # fail when IMAGE_INPUTS is stale
"""
import hashlib
import json
from pathlib import Path
import re
import sys

# The runtime string of base 1: sha256 over Dockerfile, entrypoint.sh and six
# docker/ files at ce7c7fde7, the hash today's installed images carry. Never
# change it; images in the field compare against it byte for byte.
LEGACY_RUNTIME = "dd0de098e53ad234da2d74984419505c0eee7be6a2effde68e06cdc199f95d0c"
LEGACY_PROVIDES = ("base-1",)

# Every capability ever named, with what it promises. Never reuse a name.
CAPABILITIES = {
    "base-1": "Python 3.12, Node 22, PostgreSQL 16 and Chromium; /app, /data and the appuser "
              "account; the supervisor's control directory (status, request and state files) "
              "and its internal desktop endpoints",
    "source-supervisor": "the image starts the supervisor, the updater and the Chromium launch "
                         "scripts of the active verified update instead of its own copies",
}
# What an image built from this source provides.
PROVIDES = ("base-1", "source-supervisor")
# What this source needs from the image it runs on.
REQUIRES = ("base-1",)
# What a coming release will require. Images without it say so in advance.
RECOMMENDS = ()

# Baked into the image and never replaced by an update: the image's build
# recipe, the root entrypoint and the installer it runs before the supervisor.
# The trust key is checked on its own (scripts/ci/update_reach.py).
IMAGE_INPUTS = ("Dockerfile", "entrypoint.sh", "docker/install-claude-cli.sh")
# sha256 of IMAGE_INPUTS with comment lines removed. Update with --pin.
IMAGE_INPUTS_PIN = "0167fcfbb5cfde85b4990a8e3994afb1b31518e9149b0644254b7f879ec9c158"

NAME = re.compile(r"[a-z0-9][a-z0-9.-]{0,39}")


def runtime(requires=REQUIRES):
    """The runtime string a release requiring these capabilities publishes.

    Base-1 releases publish LEGACY_RUNTIME so that images built before
    capabilities accept them; anything more gets a string those images never
    match, so they show that a new image is needed.
    """
    requires = sorted(set(requires))
    if set(requires) <= set(LEGACY_PROVIDES):
        return LEGACY_RUNTIME
    return hashlib.sha256(("ai-shuffle-image-contract\0" + "\n".join(requires)).encode()).hexdigest()


def manifest_fields():
    """The ``container`` fields publish-source.mjs signs (it adds ``sequence``)."""
    return {"runtime": runtime(REQUIRES), "requires": sorted(REQUIRES), "recommends": sorted(RECOMMENDS)}


def image_record(revision, sequence):
    """/app/docker/image.json. ``runtime`` keeps updates published before
    capabilities (which carry no ``requires``) working on new images."""
    return {"format": 2, "revision": revision, "sequence": int(sequence),
            "runtime": runtime(LEGACY_PROVIDES) if set(LEGACY_PROVIDES) <= set(PROVIDES) else runtime(PROVIDES),
            "provides": sorted(PROVIDES)}


def normalized(name, data):
    """The bytes that matter: a Dockerfile or shell script without comment
    lines or blank lines, so a comment never asks for a new pin."""
    lines = data.decode("utf8", "replace").splitlines()
    kept = [line.rstrip() for index, line in enumerate(lines)
            if line.strip() and not (line.lstrip().startswith("#") and not (index == 0 and line.startswith("#!")))]
    return ("\n".join(kept) + "\n").encode()


def image_inputs_digest(read):
    """sha256 over the normalized IMAGE_INPUTS; ``read(path)`` returns bytes or None."""
    digest = hashlib.sha256()
    for name in IMAGE_INPUTS:
        data = read(name)
        digest.update(name.encode() + b"\0" + (b"" if data is None else normalized(name, data)) + b"\0")
    return digest.hexdigest()


def validate():
    """Errors in this file's own declarations (empty when consistent)."""
    errors = []
    for group, names in (("PROVIDES", PROVIDES), ("REQUIRES", REQUIRES), ("RECOMMENDS", RECOMMENDS),
                         ("LEGACY_PROVIDES", LEGACY_PROVIDES)):
        for name in names:
            if not NAME.fullmatch(name) or name not in CAPABILITIES:
                errors.append(f"{group} names {name!r}, which CAPABILITIES does not describe")
    for name in REQUIRES:
        if name not in PROVIDES:
            errors.append(f"REQUIRES names {name!r}, but images built from this source would not provide it")
    return errors


def _reader(root):
    def read(name):
        path = Path(root) / name
        return path.read_bytes() if path.is_file() else None
    return read


def main(argv):
    root = Path(__file__).resolve().parents[1]
    if not argv:
        print(runtime(REQUIRES))
    elif argv == ["--manifest"]:
        print(json.dumps(manifest_fields(), sort_keys=True))
    elif argv in (["--pin"], ["--check"]):
        errors = validate()
        actual = image_inputs_digest(_reader(root))
        if argv == ["--pin"] and not errors:
            path = Path(__file__)
            text = path.read_text()
            path.write_text(re.sub(r'(?m)^IMAGE_INPUTS_PIN = "[a-f0-9]{64}"$', f'IMAGE_INPUTS_PIN = "{actual}"', text, count=1))
            print(f"IMAGE_INPUTS_PIN = {actual}")
        elif actual != IMAGE_INPUTS_PIN:
            errors.append("the image inputs changed (" + ", ".join(IMAGE_INPUTS) + ") and IMAGE_INPUTS_PIN is stale")
        for error in errors:
            print(f"docker/update_contract.py: {error}", file=sys.stderr)
        return 1 if errors else 0
    else:
        image_root, revision, sequence = argv
        del image_root  # the record is declared, not hashed from the image's files
        print(json.dumps(image_record(revision, sequence)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
