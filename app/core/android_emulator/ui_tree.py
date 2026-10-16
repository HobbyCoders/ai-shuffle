"""A uiautomator dump, compacted into a short tree an agent can tap from.

The raw dump is tens of KB of nested layout nodes. Only nodes a person would
see or touch are kept (text, a description, an id on something clickable,
editable or scrollable), indented under their nearest kept ancestor, each
with its tap point and bounds in the caller's coordinate space.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import List, Optional

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
MAX_LINES = 250
MAX_TEXT = 80


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"


def compact(xml_text: str, scale: float = 1.0, max_lines: int = MAX_LINES) -> str:
    """The readable tree; bounds and tap points are multiplied by ``scale``."""
    start = xml_text.find("<?xml")
    if start < 0:
        start = xml_text.find("<hierarchy")
    end = xml_text.rfind(">")
    if start < 0 or end < 0:
        raise ValueError("no UI hierarchy in the dump")
    if "<!DOCTYPE" in xml_text or "<!ENTITY" in xml_text:
        raise ValueError("the UI dump declares a DTD")  # uiautomator never does
    root = ET.fromstring(xml_text[start:end + 1])
    lines: List[str] = []
    package: Optional[str] = None
    truncated = 0

    def walk(node: ET.Element, depth: int) -> None:
        nonlocal package, truncated
        attrs = node.attrib
        kept = False
        if node.tag == "node":
            package = package or attrs.get("package")
            match = _BOUNDS.match(attrs.get("bounds", ""))
            text = attrs.get("text", "")
            desc = attrs.get("content-desc", "")
            rid = attrs.get("resource-id", "")
            cls = attrs.get("class", "").rsplit(".", 1)[-1]
            flags = [name for name in ("clickable", "long-clickable", "scrollable", "checkable")
                     if attrs.get(name) == "true"]
            if attrs.get("checked") == "true":
                flags.append("checked")
            if attrs.get("focused") == "true":
                flags.append("focused")
            if attrs.get("enabled") == "false":
                flags.append("disabled")
            if attrs.get("password") == "true":
                flags.append("password")
            editable = "EditText" in cls
            if editable:
                flags.append("editable")
            interesting = bool(text or desc or editable or {"clickable", "scrollable", "checkable",
                                                             "long-clickable"} & set(flags))
            if match and interesting:
                x1, y1, x2, y2 = (int(round(int(v) * scale)) for v in match.groups())
                if x2 > x1 and y2 > y1:
                    kept = True
                    if len(lines) >= max_lines:
                        truncated += 1
                    else:
                        parts = [cls or "View"]
                        if text:
                            parts.append(f'"{_clip(text)}"')
                        if desc and desc != text:
                            parts.append(f'desc="{_clip(desc)}"')
                        if rid:
                            parts.append("#" + rid.split(":id/", 1)[-1])
                        parts.append(f"@({(x1 + x2) // 2},{(y1 + y2) // 2})")
                        parts.append(f"[{x1},{y1},{x2},{y2}]")
                        parts.extend(flags)
                        lines.append("  " * depth + " ".join(parts))
        for child in node:
            walk(child, depth + 1 if kept else depth)

    walk(root, 0)
    head = f"package={package}" if package else "package=?"
    tail = [f"… {truncated} more elements not shown"] if truncated else []
    if not lines:
        return head + "\n(no visible text or touchable elements)"
    return "\n".join([head, *lines, *tail])
