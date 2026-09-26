"""Install a pinned official static binary, verifying its release SHA-256."""
import hashlib
import io
import os
import platform
import tarfile
import urllib.request
from pathlib import Path

VERSION = '16.4'
ASSETS = {
    'amd64': ('x86-64', 'b47ecc82fce1dcebbbc4183d839e52f07f7630c9d7ad0f54db753d1939299354'),
    'arm64': ('aarch64', 'bf544f94f305a1ff37d82f5ef1298d584f1f98b51dfd8182d1437a02073d8731'),
}


def main():
    arch = os.getenv('TARGETARCH') or {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
    if arch not in ASSETS:
        raise RuntimeError('PostgREST image supports amd64 and arm64')
    name, expected = ASSETS[arch]
    url = f'https://github.com/PostgREST/postgrest/releases/download/v{VERSION}/postgrest-v{VERSION}-linux-static-{name}.tar.xz'
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read(64 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError('PostgREST release checksum mismatch')
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        binary = archive.extractfile('postgrest')
        if binary is None:
            raise RuntimeError('PostgREST release has no binary')
        path = Path('/usr/local/bin/postgrest')
        path.write_bytes(binary.read())
        path.chmod(0o755)


if __name__ == '__main__':
    main()
