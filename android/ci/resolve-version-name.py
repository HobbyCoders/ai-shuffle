#!/usr/bin/env python3
"""Resolve the Play display version from the checked-out release history."""
import argparse
import re
import subprocess


RELEASE = r"v[0-9]+\.[0-9]+\.[0-9]+(?:[.-][A-Za-z0-9.-]+)?"


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def version_name(release_tag=""):
    if release_tag:
        if not re.fullmatch(RELEASE, release_tag):
            raise ValueError("Invalid release tag")
        if git("rev-parse", "HEAD") != git("rev-parse", f"refs/tags/{release_tag}^{{commit}}"):
            raise ValueError("Release tag does not match the checked-out commit")
        return release_tag[1:]
    description = git("describe", "--tags", "--match", "v[0-9]*", "--long", "HEAD")
    match = re.fullmatch(f"({RELEASE})-([0-9]+)-g([0-9a-f]+)", description)
    if not match:
        raise ValueError("No valid release version in checkout history; fetch release tags")
    tag, distance, revision = match.groups()
    return tag[1:] if distance == "0" else f"{tag[1:]}-dev.{distance}+g{revision}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-tag", default="")
    print(version_name(parser.parse_args().release_tag))
