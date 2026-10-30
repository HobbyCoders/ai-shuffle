#!/usr/bin/env python3
"""Fail if a purported minified build has no mapping or obfuscated app classes."""
import re
import sys
from pathlib import Path


def check_mapping(path):
    text = Path(path).read_text(encoding="utf-8")
    entries = re.findall(r"^(com\.hobbycoders\.aishuffle\.[^ ]+) -> ([^:]+):$", text, re.MULTILINE)
    renamed = [(before, after) for before, after in entries if before != after and ".ci." not in before]
    if not renamed:
        raise ValueError(f"{path}: no obfuscated production app classes; is R8 enabled?")
    print(f"{path}: {len(renamed)} production app class mappings are obfuscated")
    return renamed


if __name__ == "__main__":
    for argument in sys.argv[1:]:
        check_mapping(argument)
    if len(sys.argv) < 2:
        raise SystemExit("usage: check-minified-build.py <mapping.txt> [...]")
