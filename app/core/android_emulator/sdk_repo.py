"""Google's Android SDK repository manifests, read without sdkmanager.

The worker has no Java, so sdkmanager and avdmanager are not available. The
same packages are described by Google's repository XML: ``repository2-3.xml``
lists the emulator and platform-tools, and ``sys-img/<tag>/sys-img2-3.xml``
lists the system images of one tag. Each package names its license and, per
host, an archive with its size and SHA-1. Archive URLs are relative to the
manifest that lists them.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

REPOSITORY_BASE = "https://dl.google.com/android/repository/"
REPOSITORY_MANIFEST = REPOSITORY_BASE + "repository2-3.xml"
#: Manifests are a few hundred KB; anything far larger is not one.
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
#: The only license AI Shuffle asks the owner to accept.
SDK_LICENSE_ID = "android-sdk-license"

_SYSTEM_IMAGE = re.compile(r"^system-images;android-(\d+);([a-z0-9_]+);(x86_64|x86|arm64-v8a)$")


class ManifestError(ValueError):
    """A manifest could not be read, or lacks the package asked for."""


@dataclass(frozen=True)
class Archive:
    url: str
    size: int
    sha1: str
    host_os: Optional[str] = None
    host_arch: Optional[str] = None


@dataclass(frozen=True)
class Package:
    path: str
    revision: Tuple[int, ...]
    channel: str
    license_id: Optional[str]
    archives: Tuple[Archive, ...]
    obsolete: bool = False
    #: The full license texts the manifest carries, by id.
    licenses: Dict[str, str] = field(default_factory=dict, compare=False, repr=False)

    @property
    def revision_text(self) -> str:
        return ".".join(str(part) for part in self.revision)

    def archive_for(self, host_os: Optional[str], host_arch: Optional[str]) -> Optional[Archive]:
        """This host's archive: an exact match, or one that names no host."""
        for archive in self.archives:
            if archive.host_os not in (None, host_os):
                continue
            if archive.host_arch not in (None, host_arch):
                continue
            return archive
        return None

    @property
    def license_text(self) -> str:
        return self.licenses.get(self.license_id or "", "")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(node: ET.Element, name: str) -> Optional[ET.Element]:
    for child in node:
        if _local(child.tag) == name:
            return child
    return None


def _text(node: Optional[ET.Element], name: str) -> Optional[str]:
    if node is None:
        return None
    child = _child(node, name)
    return child.text.strip() if child is not None and child.text else None


def _revision(node: Optional[ET.Element]) -> Tuple[int, ...]:
    if node is None:
        return (0,)
    parts = []
    for name in ("major", "minor", "micro", "preview"):
        value = _text(node, name)
        if value is None:
            if name == "preview":
                break
            value = "0"
        try:
            parts.append(int(value))
        except ValueError:
            raise ManifestError(f"bad revision number {value!r}") from None
    return tuple(parts)


def parse_manifest(data: bytes, base_url: str) -> List[Package]:
    """Every package in one repository manifest. Archive URLs come back absolute."""
    if len(data) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest is too large")
    if b"<!DOCTYPE" in data[:4096] or b"<!ENTITY" in data:
        # Repository manifests never declare a DTD; refusing one rules out
        # entity expansion in the stdlib parser.
        raise ManifestError("manifest declares a DTD")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ManifestError(f"manifest is not valid XML: {exc}") from None
    channels: Dict[str, str] = {}
    licenses: Dict[str, str] = {}
    for child in root:
        name = _local(child.tag)
        if name == "channel" and child.get("id"):
            channels[child.get("id")] = (child.text or "").strip()
        elif name == "license" and child.get("id"):
            licenses[child.get("id")] = child.text or ""
    packages: List[Package] = []
    for node in root:
        if _local(node.tag) != "remotePackage":
            continue
        path = node.get("path") or ""
        channel_ref = _child(node, "channelRef")
        license_ref = _child(node, "uses-license")
        archives = []
        archives_node = _child(node, "archives")
        for archive in list(archives_node) if archives_node is not None else []:
            complete = _child(archive, "complete")
            if complete is None:
                continue
            checksum = _child(complete, "checksum")
            url = _text(complete, "url")
            size = _text(complete, "size")
            if checksum is None or not url or not size:
                continue
            kind = (checksum.get("type") or "sha1").lower()
            digest = (checksum.text or "").strip().lower()
            if kind != "sha1" or not re.fullmatch(r"[0-9a-f]{40}", digest):
                continue
            if "/" in url or "\\" in url or url.startswith("."):
                # Archives sit next to their manifest; never follow a path elsewhere.
                continue
            archives.append(Archive(
                url=base_url + url, size=int(size), sha1=digest,
                host_os=_text(archive, "host-os"), host_arch=_text(archive, "host-arch"),
            ))
        packages.append(Package(
            path=path,
            revision=_revision(_child(node, "revision")),
            channel=channels.get(channel_ref.get("ref") if channel_ref is not None else "", "stable"),
            license_id=license_ref.get("ref") if license_ref is not None else None,
            archives=tuple(archives),
            obsolete=(node.get("obsolete") or "").lower() == "true",
            licenses=licenses,
        ))
    return packages


def resolve(packages: List[Package], path: str, host_os: Optional[str],
            host_arch: Optional[str]) -> Tuple[Package, Archive]:
    """The newest stable, current package at ``path`` with an archive for this host."""
    best: Optional[Tuple[Package, Archive]] = None
    for package in packages:
        if package.path != path or package.obsolete or package.channel != "stable":
            continue
        archive = package.archive_for(host_os, host_arch)
        if archive is None:
            continue
        if best is None or package.revision > best[0].revision:
            best = (package, archive)
    if best is None:
        raise ManifestError(f"no stable {path} package for {host_os or 'any'}/{host_arch or 'any'}")
    return best


def system_image_manifest(path: str) -> str:
    """The sys-img manifest URL that lists ``path`` (``system-images;android-35;google_apis;x86_64``)."""
    match = _SYSTEM_IMAGE.match(path or "")
    if not match:
        raise ManifestError(f"not a system image package path: {path!r}")
    return f"{REPOSITORY_BASE}sys-img/{match.group(2)}/sys-img2-3.xml"


def system_image_parts(path: str) -> Tuple[int, str, str]:
    """``(api level, tag, abi)`` of a system image package path."""
    match = _SYSTEM_IMAGE.match(path or "")
    if not match:
        raise ManifestError(f"not a system image package path: {path!r}")
    return int(match.group(1)), match.group(2), match.group(3)
