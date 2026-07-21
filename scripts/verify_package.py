from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_NOTICE = (
    ROOT / "packages" / "anywidget-mcp" / "THIRD_PARTY_NOTICES"
).read_bytes()


def _read_wheel_notice(path: Path) -> bytes:
    with zipfile.ZipFile(path) as archive:
        matches = [
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/licenses/THIRD_PARTY_NOTICES")
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"{path} contains {len(matches)} third-party notice files"
            )
        return archive.read(matches[0])


def _read_sdist_notice(path: Path) -> bytes:
    with tarfile.open(path, "r:gz") as archive:
        matches = [
            member
            for member in archive.getmembers()
            if member.isfile() and member.name.endswith("/THIRD_PARTY_NOTICES")
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"{path} contains {len(matches)} third-party notice files"
            )
        stream = archive.extractfile(matches[0])
        if stream is None:
            raise RuntimeError(f"Could not read third-party notices from {path}")
        return stream.read()


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: verify_package.py WHEEL SDIST")

    wheel, sdist = map(Path, sys.argv[1:])
    for archive, notice in (
        (wheel, _read_wheel_notice(wheel)),
        (sdist, _read_sdist_notice(sdist)),
    ):
        if notice != EXPECTED_NOTICE:
            raise RuntimeError(f"{archive} contains stale third-party notices")


if __name__ == "__main__":
    main()
