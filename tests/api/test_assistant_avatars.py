"""Avatars: one SVG file each, listed in avatars.json, mirrored by the backend."""
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from app.db import assistant as store

AVATARS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "components" / "assistant" / "avatars"
SVG = "{http://www.w3.org/2000/svg}"
DRAWING_TAGS = {SVG + tag for tag in ("path", "circle", "ellipse", "rect")}


def manifest():
    return json.loads((AVATARS / "avatars.json").read_text(encoding="utf-8"))


def test_backend_allowlist_is_the_manifest():
    # The frontend reads avatars.json directly; the backend ships without the
    # frontend sources, so its copy is held to the manifest here.
    assert tuple(manifest()) == store.AVATAR_PRESETS
    assert len(set(manifest())) == len(manifest()) == 16
    assert all(re.fullmatch(r"[a-z]+", avatar) for avatar in manifest())


def test_every_listed_avatar_is_exactly_one_svg_file():
    assert {path.stem for path in AVATARS.glob("*.svg")} == set(manifest())
    assert "robot" in manifest()  # the component's fallback


def test_avatar_files_are_plain_static_drawings():
    """They are inlined with {@html}: shapes only, no scripts, links or styles."""
    for path in AVATARS.glob("*.svg"):
        root = ET.fromstring(path.read_text(encoding="utf-8"))
        assert root.tag == SVG + "svg" and root.get("viewBox") == "0 0 64 64", path.name
        children = list(root.iter())[1:]
        assert children and all(child.tag in DRAWING_TAGS for child in children), path.name
        for element in root.iter():
            for name, value in element.attrib.items():
                assert not name.startswith("on") and "href" not in name, (path.name, name)
                assert "url(" not in value and "javascript:" not in value, (path.name, name)
