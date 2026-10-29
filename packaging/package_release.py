"""Put the files of a MirrorBridge release together.

    python packaging/package_release.py --exe dist/MirrorBridge.exe --notices build/notices \\
        --out release [--ffmpeg-source ffmpeg-7.1.tar.xz] [--version 0.3.0]

writes, in --out (which must be empty or new):

    MirrorBridge.exe                     the exe, as built
    MirrorBridge-0.3.0-windows-x64.zip   MirrorBridge-0.3.0/ with the exe, LEEME.md, README.md, LICENSE,
                                          THIRD-PARTY-NOTICES.txt and licenses/ (third_party_notices.py)
    ffmpeg-7.1.tar.xz                    FFmpeg's official source, which its GPL asks to go with the binary
    SHA256SUMS                           "<sha256>  <name>" for each of those, for `sha256sum -c SHA256SUMS`

The zip has the same bytes every time it is made from the same files: its entries are sorted, and
they all carry one date (SOURCE_DATE_EPOCH when it is set, else 1980-01-01).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import tarfile
import time
import zipfile
from pathlib import Path

from check_version import code_version

ROOT = Path(__file__).resolve().parents[1]
DOCS = ("LEEME.md", "README.md", "LICENSE")
NOTICES = "THIRD-PARTY-NOTICES.txt"
EXE_NAME = "MirrorBridge.exe"
SUMS = "SHA256SUMS"
FFMPEG_SOURCE = re.compile(r"ffmpeg-(\d+\.\d+(?:\.\d+)?)\.tar\.xz")


def zip_name(version: str) -> str:
    return f"MirrorBridge-{version}-windows-x64.zip"


def check_ffmpeg_source(path: Path) -> str:
    """The FFmpeg release that an official source tarball (ffmpeg-<version>.tar.xz) holds; stops
    unless every file in it is under ffmpeg-<version>/ and its RELEASE file says that version."""
    name = FFMPEG_SOURCE.fullmatch(path.name)
    if not name:
        raise SystemExit(f"{path.name} is not named like FFmpeg's source tarballs (ffmpeg-<version>.tar.xz)")
    version, top = name.group(1), f"ffmpeg-{name.group(1)}"
    try:
        with tarfile.open(path, "r:xz") as tar:
            stray = [n for n in tar.getnames() if n != top and not n.startswith(f"{top}/")]
            release = tar.extractfile(f"{top}/RELEASE")
            said = release.read().decode("utf-8", "replace").strip() if release else ""
    except (tarfile.TarError, KeyError, OSError, EOFError) as ex:
        raise SystemExit(f"{path} is not FFmpeg's source tarball: {ex}") from ex
    if stray:
        raise SystemExit(f"{path} has files outside {top}/: {', '.join(stray[:5])}")
    if said != version:
        raise SystemExit(f"{path} is named {version}, but its RELEASE file says {said!r}")
    return version


def _date() -> tuple[int, int, int, int, int, int]:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if not epoch:
        return (1980, 1, 1, 0, 0, 0)
    return time.gmtime(max(int(epoch), 315532800))[:6]  # zip dates start in 1980


def write_zip(path: Path, files: dict[str, Path]) -> None:
    """A zip of files (name inside it -> file), the same bytes for the same files."""
    date = _date()
    folders = sorted({"/".join(name.split("/")[:i]) + "/" for name in files for i in range(1, name.count("/") + 1)})
    with zipfile.ZipFile(path, "w") as archive:
        for name in sorted([*folders, *files]):
            info = zipfile.ZipInfo(name, date)
            info.create_system = 3  # the same on every OS: Unix permissions below, MS-DOS flags for folders
            if name in files:
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, files[name].read_bytes(), compresslevel=9)
            else:
                info.external_attr = (0o40755 << 16) | 0x10
                archive.writestr(info, b"")


def sha256(path: Path) -> str:
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def package(exe: Path, notices: Path, out: Path, version: str, ffmpeg_source: Path | None = None,
            root: Path = ROOT) -> list[Path]:
    """Write the release's files into out; returns them, SHA256SUMS last."""
    if not exe.is_file():
        raise SystemExit(f"{exe} is missing: build it first")
    notices_text = notices / NOTICES
    if not notices_text.is_file() or not (notices / "licenses").is_dir():
        raise SystemExit(f"{notices} has no {NOTICES} and licenses/: run packaging/third_party_notices.py first")
    if ffmpeg_source is not None:
        check_ffmpeg_source(ffmpeg_source)
        if ffmpeg_source.name not in notices_text.read_text(encoding="utf-8"):
            raise SystemExit(f"{NOTICES} does not name {ffmpeg_source.name}: it was made for another FFmpeg")
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty; everything in it would be published")
    out.mkdir(parents=True, exist_ok=True)

    top = f"MirrorBridge-{version}"
    files = {f"{top}/{EXE_NAME}": exe, f"{top}/{NOTICES}": notices_text}
    files.update({f"{top}/{name}": root / name for name in DOCS})
    for text in sorted((notices / "licenses").rglob("*")):
        if text.is_file():
            files[f"{top}/{text.relative_to(notices).as_posix()}"] = text

    written = [out / EXE_NAME, out / zip_name(version)]
    shutil.copyfile(exe, written[0])
    write_zip(written[1], files)
    if ffmpeg_source is not None:
        written.append(out / ffmpeg_source.name)
        shutil.copyfile(ffmpeg_source, written[-1])
    sums = out / SUMS
    with open(sums, "w", encoding="utf-8", newline="\n") as f:
        for path in sorted(written, key=lambda p: p.name):
            f.write(f"{sha256(path)}  {path.name}\n")
    return [*written, sums]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--exe", type=Path, default=ROOT / "dist" / EXE_NAME)
    parser.add_argument("--notices", type=Path, default=ROOT / "build" / "notices",
                        help="the folder third_party_notices.py wrote")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--version", help="defaults to the code's version, which it must equal")
    parser.add_argument("--ffmpeg-source", type=Path, help="FFmpeg's ffmpeg-<version>.tar.xz, for a release")
    args = parser.parse_args(argv)
    version = code_version()
    if args.version and args.version != version:
        raise SystemExit(f"--version {args.version}, but the code is version {version}")
    for path in package(args.exe, args.notices, args.out, version, args.ffmpeg_source):
        print(f"{path}  ({path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
