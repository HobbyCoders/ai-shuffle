"""Find or install the native tunnel client without changing the system PATH."""
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from fluid.tools.child_env import scrub_child_env

# Keep the native client aligned with Dockerfile. Digests are from Cloudflare's
# official GitHub release assets, pinned so setup never executes an unchecked download.
VERSION = '2026.8.3'
_DIGESTS = {
    'cloudflared-windows-amd64.exe': '83e726ed18ea78c5ad5213c4c3a3a27051393950d2bc8ed4de69bec12d14eaae',
    'cloudflared-windows-386.exe': 'bdfab00122a3c2a0772d3f176445f6baf0271fed71656d0902cbc23a0eea7048',
    'cloudflared-linux-amd64': 'f29324fe934d1e100617484c78deef803c4dc2cd351d645bbde42e96b4fccc5e',
    'cloudflared-linux-arm64': '4bcfd35521a7cbc545ebfd5d57334a71ee180e2a64874981f374c81472118391',
    'cloudflared-linux-386': '691e3a2b8926f90ec4fcec4f4bc8e38b1de14f6232d93a24f6cb7b2c23ab5e92',
    # Darwin release-body checksums differ from the downloadable archives. Use
    # GitHub's asset digests, independently checked against the downloaded bytes.
    'cloudflared-darwin-amd64.tgz': '61e1316266a00fd70ce40da011d612badc805367fb65293dd1925f938f704c99',
    'cloudflared-darwin-arm64.tgz': '40c9144d86df8937c5b43293a1f7d2d2107029aa74725023dd46b1b27154352f',
}


def _asset():
    system = {'win32': 'windows', 'darwin': 'darwin', 'linux': 'linux'}.get(sys.platform)
    machine = platform.machine().lower()
    arch = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64',
            'arm64': 'arm64', 'i386': '386', 'i686': '386', 'x86': '386'}.get(machine)
    suffix = '.exe' if system == 'windows' else '.tgz' if system == 'darwin' else ''
    name = f'cloudflared-{system}-{arch}{suffix}'
    if name not in _DIGESTS:
        raise RuntimeError('Install cloudflared on PATH for this operating system and architecture')
    return name


def _local_path():
    executable = 'cloudflared.exe' if sys.platform == 'win32' else 'cloudflared'
    return Path(__file__).resolve().parents[2] / 'downloads' / 'cloudflared' / VERSION / executable


def find_executable():
    installed = shutil.which('cloudflared')
    if installed:
        return installed
    local = _local_path()
    return str(local) if local.is_file() and os.access(local, os.X_OK) else None


def install(*, force_local=False):
    """Install a verified client; packaging always refreshes the local copy.

    Normal setup may reuse PATH or an offline installation. A desktop build
    must carry its own pinned binary, even if the builder already has one.
    """
    existing = None if force_local else find_executable()
    if existing:
        return existing
    asset = _asset()
    target = _local_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.install-', dir=target.parent) as temporary:
        archive = Path(temporary) / asset
        request = urllib.request.Request(
            f'https://github.com/cloudflare/cloudflared/releases/download/{VERSION}/{asset}',
            headers={'User-Agent': 'Shuffle-local-setup'},
        )
        digest = hashlib.sha256()
        size = 0
        with urllib.request.urlopen(request, timeout=30) as response, archive.open('wb') as output:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 128 * 1024 * 1024:
                    raise RuntimeError('cloudflared download exceeds the expected size')
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != _DIGESTS[asset]:
            raise RuntimeError('cloudflared download checksum did not match the pinned release')
        binary = archive
        if asset.endswith('.tgz'):
            binary = Path(temporary) / 'cloudflared'
            with tarfile.open(archive, 'r:gz') as bundle:
                member = bundle.getmember('cloudflared')
                if not member.isfile() or member.size > 128 * 1024 * 1024:
                    raise RuntimeError('Invalid cloudflared archive')
                with bundle.extractfile(member) as source, binary.open('wb') as output:
                    shutil.copyfileobj(source, output)
        binary.chmod(0o755)
        os.replace(binary, target)
    return str(target)


def bundle(*, reuse_verified=False):
    """Verify a pinned binary, downloading unless an allowed cache matches exactly."""
    asset = _asset()
    local = _local_path()
    # Windows/Linux raw binaries can be checked against the pinned archive hash.
    # macOS archives are compressed, so keep their existing download path.
    cached = (reuse_verified and not asset.endswith('.tgz') and local.is_file()
              and hashlib.sha256(local.read_bytes()).hexdigest() == _DIGESTS[asset])
    executable = str(local) if cached else install(force_local=True)
    result = subprocess.run(
        [executable, '--version'], check=True, capture_output=True, text=True,
        timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        env=scrub_child_env(),
    )
    if not result.stdout.strip().startswith(f'cloudflared version {VERSION} '):
        raise RuntimeError('Bundled cloudflared did not report the pinned version')
    asset = _asset()
    return {
        'version': VERSION,
        'asset': asset,
        'archiveSha256': _DIGESTS[asset],
        'sha256': hashlib.sha256(Path(executable).read_bytes()).hexdigest(),
        'executable': executable,
    }


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', action='store_true', help='Install and verify a package-local binary, returning its manifest entry')
    parser.add_argument('--reuse-verified', action='store_true', help='Reuse a local binary only when its hash matches the pinned release')
    args = parser.parse_args()
    print(json.dumps(bundle(reuse_verified=args.reuse_verified)) if args.bundle else install())
