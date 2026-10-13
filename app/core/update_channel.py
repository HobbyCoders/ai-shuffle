"""Local channel selection shared by the supervisor and owner API."""
import json
import logging
import os
from pathlib import Path

CHANNELS = ("stable", "nightly")
_alias_logged = False


def normalize(value):
    global _alias_logged
    if value == "edge":
        if not _alias_logged:
            logging.getLogger(__name__).warning("Update channel edge is deprecated; using nightly")
            _alias_logged = True
        return "nightly"
    if value not in CHANNELS:
        raise ValueError("Update channel must be stable or nightly")
    return value


def selection(root, environ=None):
    env = os.environ if environ is None else environ
    if "UPDATE_CHANNEL" in env:
        return normalize(env["UPDATE_CHANNEL"].strip().lower()), "env"
    try:
        value = json.loads((Path(root) / "update-channel.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "stable", "default"
    if not isinstance(value, dict) or "channel" not in value:
        raise ValueError("Invalid update-channel.json; expected a channel object")
    return normalize(value["channel"]), "file"
