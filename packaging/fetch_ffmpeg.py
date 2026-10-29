"""Fetch the FFmpeg that goes inside MirrorBridge.exe and check that it is the pinned one.

    python -I packaging/fetch_ffmpeg.py                  # downloads it from GitHub the first time (109 MB)
    python -I packaging/fetch_ffmpeg.py --zip C:\\path\\ffmpeg-8.1.1-essentials_build.zip
    python -I packaging/fetch_ffmpeg.py --bundled        # only say which ffmpeg.exe MirrorBridge.spec takes
    python -I packaging/fetch_ffmpeg.py --source build/ffmpeg-source   # FFmpeg's source, for a release

packaging/ffmpeg_pin.json names the build: gyan.dev's "essentials" zip of FFmpeg 8.1.1, with its
size and SHA-256 and those of the ffmpeg.exe inside it. The zip, downloaded or given with --zip
(for a PC whose security software blocks the download), must match both. Only ffmpeg.exe comes
out of it, to build/ffmpeg_811/ffmpeg.exe, and it is checked again before it takes that place.
The path of that file is the one line on standard output; everything else goes to standard error.
Nothing here runs the binary: build_exe.ps1 does that, to check what goes inside the exe.

MirrorBridge.spec bundles that file and nothing else, unless MIRRORBRIDGE_OTHER_FFMPEG=1 is set
(build_exe.ps1 -AllowOtherFFmpeg). Then it takes MIRRORBRIDGE_FFMPEG (or AIRCAST_FFMPEG, its name
before the rename) or else the FFmpeg of the imageio-ffmpeg package, as builds did before.

--source downloads FFmpeg's source tarball for that version and its signature, which a release
publishes next to the exe because FFmpeg's GPL asks for it. The signature must be good and made
by FFmpeg's release signing key: packaging/ffmpeg-devel.asc, whose fingerprint the pin holds as
https://ffmpeg.org/download.html#release-verification lists it. gpgv or gpg checks it (on Windows,
the copies that come with Git for Windows do). Once the pin has the tarball's SHA-256, the tarball
must match that too; until then the SHA-256 is printed, to be put in the pin.

Only the standard library is used, so it runs before anything is installed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PIN = HERE / "ffmpeg_pin.json"
KEY = HERE / "ffmpeg-devel.asc"  # FFmpeg's release signing key
OPT_IN = "MIRRORBRIDGE_OTHER_FFMPEG"
OVERRIDES = ("MIRRORBRIDGE_FFMPEG", "AIRCAST_FFMPEG")  # honoured only with OPT_IN=1
USER_AGENT = "MirrorBridge-build (packaging/fetch_ffmpeg.py)"
TRIES = 3
CHUNK = 1 << 20
SOURCE_LIMIT = 200_000_000  # FFmpeg's source tarballs are about 11 MB
SIGNATURE_LIMIT = 64 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}")
FIELDS = {"version": str, "provider": str, "build": str, "archive_url": str, "archive_size": int,
          "archive_sha256": str, "exe_member": str, "exe_size": int, "exe_sha256": str, "source_commit": str,
          "source_url": str, "source_key_fingerprint": str, "source_sha256": str}


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def load_pin(path: Path = PIN) -> dict:
    """The pinned build (ffmpeg_pin.json), once it is clear that it holds what this script needs."""
    pin = json.loads(path.read_text(encoding="utf-8"))
    wrong = [name for name, kind in FIELDS.items()
             if not isinstance(pin.get(name), kind) or isinstance(pin.get(name), bool)]
    if wrong:
        raise SystemExit(f"{path} is missing these, or they have the wrong type: {', '.join(wrong)}")
    version, member = pin["version"], pin["exe_member"]
    checks = {
        "version": re.fullmatch(r"\d+\.\d+(\.\d+)?", version),
        "archive_url": pin["archive_url"].startswith("https://"),
        "archive_size": pin["archive_size"] > 0,
        "archive_sha256": SHA256.fullmatch(pin["archive_sha256"]),
        "exe_member": member.endswith("/ffmpeg.exe") and not member.startswith("/") and ".." not in member.split("/"),
        "exe_size": pin["exe_size"] > 0,
        "exe_sha256": SHA256.fullmatch(pin["exe_sha256"]),
        "source_commit": re.fullmatch(r"[0-9a-f]{40}", pin["source_commit"]),
        # package_release.py takes only a tarball named like FFmpeg's own: ffmpeg-<version>.tar.xz
        "source_url": pin["source_url"].startswith("https://")
        and _file_name(pin["source_url"]) == f"ffmpeg-{version}.tar.xz",
        "source_key_fingerprint": re.fullmatch(r"[0-9A-F]{40}", pin["source_key_fingerprint"]),
        "source_sha256": pin["source_sha256"] == "" or SHA256.fullmatch(pin["source_sha256"]),
    }
    if bad := [name for name, ok in checks.items() if not ok]:
        raise SystemExit(f"{path}: these do not look right: {', '.join(bad)}")
    return pin


def _file_name(url: str) -> str:
    return urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])


def exe_path(pin: dict, root: Path = ROOT) -> Path:
    """Where the checked ffmpeg.exe goes: build/ffmpeg_811/ffmpeg.exe for 8.1.1."""
    return root / "build" / f"ffmpeg_{pin['version'].replace('.', '')}" / "ffmpeg.exe"


def sha256_of(path: Path) -> str:
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def matches(path: Path, size: int, sha256: str) -> bool:
    return path.is_file() and path.stat().st_size == size and sha256_of(path) == sha256


def check_file(path: Path, size: int, sha256: str, what: str) -> None:
    """Stops unless path has that size and SHA-256 (the size first: it is quicker and says more)."""
    if not path.is_file():
        raise SystemExit(f"{path} does not exist")
    actual = path.stat().st_size
    if actual != size:
        raise SystemExit(f"{path} is {actual:,} bytes, not {size:,}: it is not {what}")
    digest = sha256_of(path)
    if digest != sha256:
        raise SystemExit(f"{path} has SHA-256 {digest}, not {sha256}: it is not {what}")


def download(url: str, dest: Path, limit: int) -> None:
    """url saved as dest, through dest.part, tried up to TRIES times; stops past limit bytes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        for attempt in range(1, TRIES + 1):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=60) as response, open(part, "wb") as out:
                    got = 0
                    while chunk := response.read(CHUNK):
                        got += len(chunk)
                        if got > limit:
                            raise SystemExit(f"{url} is bigger than {limit:,} bytes: it is not the pinned file")
                        out.write(chunk)
                os.replace(part, dest)
                return
            except OSError as ex:  # urllib's errors are OSErrors too
                if attempt == TRIES:
                    raise SystemExit(f"Could not download {url}: {ex}") from ex
                log(f"Could not download {url} ({ex}); trying again in {5 * attempt} s")
                time.sleep(5 * attempt)
    finally:
        part.unlink(missing_ok=True)


def extract(archive: Path, pin: dict, exe: Path) -> None:
    """Only the pinned ffmpeg.exe out of the zip, checked before it takes its place."""
    member, size, sha256 = pin["exe_member"], pin["exe_size"], pin["exe_sha256"]
    exe.parent.mkdir(parents=True, exist_ok=True)
    part = exe.with_name(exe.name + ".part")
    try:
        with zipfile.ZipFile(archive) as z:
            try:
                info = z.getinfo(member)
            except KeyError:
                raise SystemExit(f"{archive} has no {member}") from None
            if info.file_size != size:
                raise SystemExit(f"{member} in {archive} is {info.file_size:,} bytes, not {size:,}")
            digest, got = hashlib.sha256(), 0
            with z.open(info) as src, open(part, "wb") as out:
                while chunk := src.read(CHUNK):
                    got += len(chunk)
                    if got > size:
                        raise SystemExit(f"{member} in {archive} unpacks to more than {size:,} bytes")
                    digest.update(chunk)
                    out.write(chunk)
        if got != size or digest.hexdigest() != sha256:
            raise SystemExit(f"{member} in {archive} has SHA-256 {digest.hexdigest()}, not {sha256}")
        os.replace(part, exe)
    except (zipfile.BadZipFile, EOFError) as ex:
        raise SystemExit(f"{archive} is not a good zip file: {ex}") from ex
    finally:
        part.unlink(missing_ok=True)


def fetch(pin: dict, archive: Path | None = None, root: Path = ROOT) -> Path:
    """The pinned ffmpeg.exe, checked: the one already in place, or else taken out of archive (the
    pinned zip, downloaded into build/ffmpeg-download when None)."""
    exe = exe_path(pin, root)
    what = f"the pinned FFmpeg {pin['version']} zip ({pin['provider']} {pin['build']})"
    if matches(exe, pin["exe_size"], pin["exe_sha256"]):
        log(f"{exe} is already the pinned FFmpeg {pin['version']}")
        return exe
    if archive is None:
        archive = root / "build" / "ffmpeg-download" / _file_name(pin["archive_url"])
        if not matches(archive, pin["archive_size"], pin["archive_sha256"]):
            log(f"Downloading FFmpeg {pin['version']} ({pin['archive_size'] / 1e6:.0f} MB) from {pin['archive_url']}")
            download(pin["archive_url"], archive, pin["archive_size"])
            check_file(archive, pin["archive_size"], pin["archive_sha256"], what)
    else:
        check_file(archive, pin["archive_size"], pin["archive_sha256"], what)
    extract(archive, pin, exe)
    log(f"FFmpeg {pin['version']} checked and put in {exe}")
    return exe


def bundled(environ: dict | None = None, pin: dict | None = None, root: Path = ROOT) -> tuple[Path, str]:
    """The ffmpeg.exe that MirrorBridge.spec puts inside the exe, and why that one."""
    environ = os.environ if environ is None else environ
    pin = pin or load_pin()
    if environ.get(OPT_IN) == "1":
        name = next((n for n in OVERRIDES if environ.get(n)), None)
        if name:
            path, why = Path(environ[name]), f"{name}, as {OPT_IN}=1 allows"
        else:
            try:
                import imageio_ffmpeg  # pylint: disable=import-outside-toplevel
            except ImportError:
                raise SystemExit(f"{OPT_IN}=1, but neither {' nor '.join(OVERRIDES)} is set and the "
                                 "imageio-ffmpeg package is not installed") from None
            path, why = Path(imageio_ffmpeg.get_ffmpeg_exe()), f"the imageio-ffmpeg package's, as {OPT_IN}=1 allows"
        if not path.is_file():
            raise SystemExit(f"{path} ({why}) does not exist")
        return path, why
    exe = exe_path(pin, root)
    if not matches(exe, pin["exe_size"], pin["exe_sha256"]):
        raise SystemExit(f"{exe} is missing or is not the pinned FFmpeg {pin['version']}: run "
                         f"python -I packaging/fetch_ffmpeg.py first (packaging\\build_exe.ps1 does), or set "
                         f"{OPT_IN}=1 to bundle another FFmpeg")
    why = f"the pinned FFmpeg {pin['version']} ({pin['provider']} {pin['build']}), checked by its SHA-256"
    ignored = [n for n in OVERRIDES if environ.get(n)]
    if ignored:
        why += f"; {' and '.join(ignored)} left alone without {OPT_IN}=1"
    return exe, why


# --- FFmpeg's source, for a release --------------------------------------------------------------

def dearmor(text: str) -> bytes:
    """The binary OpenPGP data of an ASCII-armored block (gpgv reads only binary keyrings)."""
    lines = [line.strip() for line in text.splitlines()]
    try:
        begin = next(i for i, line in enumerate(lines) if line.startswith("-----BEGIN PGP "))
        end = next(i for i, line in enumerate(lines) if i > begin and line.startswith("-----END PGP "))
    except StopIteration:
        raise SystemExit("Not an ASCII-armored OpenPGP block") from None
    body = lines[begin + 1:end]
    if "" in body:  # armor headers (Comment: ...) end at the first empty line
        body = body[body.index("") + 1:]
    data = "".join(line for line in body if line and not line.startswith("="))  # =XXXX is the checksum
    return base64.b64decode(data, validate=True)


def find_gpg() -> str | None:
    """gpgv, else gpg: on PATH, or (on Windows) the copies that come with Git for Windows."""
    folders: list[str | None] = [None]  # None: PATH
    if os.name == "nt":
        roots = []
        if git := shutil.which("git"):
            roots.append(Path(git).resolve().parents[1])  # ...\Git\cmd\git.exe -> ...\Git
        roots += [Path(os.environ[v]) / "Git" for v in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")
                  if os.environ.get(v)]
        folders += [str(r / "usr" / "bin") for r in roots]
    for name in ("gpgv", "gpg"):
        for folder in folders:
            if found := shutil.which(name, path=folder):
                return found
    return None


def verify_signature(data: Path, signature: Path, fingerprint: str, key: Path = KEY, gpg: str | None = None) -> None:
    """Stops unless signature is a good signature of data by the key with that fingerprint."""
    gpg = gpg or find_gpg()
    if not gpg:
        raise SystemExit("Neither gpgv nor gpg is installed (Git for Windows comes with them), so FFmpeg's "
                         "signature cannot be checked")
    with tempfile.TemporaryDirectory() as home:
        keyring = Path(home) / "ffmpeg-release-key.gpg"
        keyring.write_bytes(dearmor(key.read_text(encoding="ascii")))
        # Forward slashes: Git for Windows' gpg takes C:/... as well as the shell's own paths.
        args = [gpg, "--homedir", Path(home).as_posix(), "--status-fd", "1", "--keyring", keyring.as_posix()]
        if not Path(gpg).name.lower().startswith("gpgv"):  # gpg: only that keyring, which is trusted as pinned
            args += ["--batch", "--no-default-keyring", "--trust-model", "always", "--verify"]
        result = subprocess.run(args + [signature.as_posix(), data.as_posix()], capture_output=True,
                                encoding="utf-8", errors="replace", timeout=600, check=False)
    status = [line.split()[1:] for line in result.stdout.splitlines() if line.startswith("[GNUPG:] ")]
    signers = {fields[-1].upper() for fields in status if fields and fields[0] == "VALIDSIG"}  # primary key
    refused = [fields[0] for fields in status if fields and fields[0] in
               ("BADSIG", "ERRSIG", "EXPSIG", "EXPKEYSIG", "REVKEYSIG", "NO_PUBKEY")]
    if result.returncode != 0 or refused or signers != {fingerprint.upper()}:
        said = (result.stdout + result.stderr).strip()
        raise SystemExit(f"{signature.name} is not a good signature of {data.name} by FFmpeg's release key "
                         f"{fingerprint} ({gpg} exited with {result.returncode}):\n{said}")


def fetch_source(pin: dict, folder: Path, key: Path = KEY, gpg: str | None = None) -> Path:
    """FFmpeg's source tarball for the pinned version in folder (downloaded unless it is there),
    its signature checked, and its SHA-256 too once the pin has one."""
    tarball = folder / _file_name(pin["source_url"])
    signature = tarball.with_name(tarball.name + ".asc")
    if not tarball.is_file():
        log(f"Downloading {pin['source_url']}")
        download(pin["source_url"], tarball, SOURCE_LIMIT)
    if not signature.is_file():
        download(pin["source_url"] + ".asc", signature, SIGNATURE_LIMIT)
    verify_signature(tarball, signature, pin["source_key_fingerprint"], key, gpg)
    digest = sha256_of(tarball)
    log(f"{tarball.name}: good signature by FFmpeg's release key {pin['source_key_fingerprint']}, SHA-256 {digest}")
    if pin["source_sha256"] and digest != pin["source_sha256"]:
        raise SystemExit(f"{tarball} has SHA-256 {digest}, not the pinned {pin['source_sha256']}")
    if not pin["source_sha256"]:
        log(f"The pin has no source_sha256 yet: put {digest} in packaging/ffmpeg_pin.json")
    return tarball


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    what = parser.add_mutually_exclusive_group()
    what.add_argument("--zip", type=Path, help="the pinned zip, already on this PC, instead of downloading it")
    what.add_argument("--bundled", action="store_true", help="only print the ffmpeg.exe MirrorBridge.spec will bundle")
    what.add_argument("--source", type=Path, metavar="DIR",
                      help="fetch FFmpeg's source tarball and its signature into DIR and check them")
    parser.add_argument("--gpg", help="gpgv or gpg for --source (default: found on PATH or with Git for Windows)")
    parser.add_argument("--pin", type=Path, default=PIN, help=argparse.SUPPRESS)
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    pin, root = load_pin(args.pin), args.root.resolve()
    if args.bundled:
        path, why = bundled(pin=pin, root=root)
        log(f"MirrorBridge.spec will bundle {path}: {why}")
    elif args.source:
        path = fetch_source(pin, args.source.resolve(), gpg=args.gpg)
    else:
        path = fetch(pin, args.zip.resolve() if args.zip else None, root)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
