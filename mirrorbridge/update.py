"""Updates from MirrorBridge's GitHub Releases: find a newer version, download its installer, check
it and run it.

Each release (built by .github/workflows/release.yml from a tag) carries
MirrorBridge-Setup-<version>.exe and SHA256SUMS. An update is taken only when all of this holds:

- the release is published (not a draft), its tag is v<X.Y.Z> and that version is newer than ours;
- it is a full release, or pre-releases are allowed (Settings.update_prereleases);
- its installer is named for that same version and both files come from this repository's
  release downloads (https://github.com/draczer01/mirrorbridge/releases/download/<tag>/...);
- the downloaded installer has the size GitHub lists, starts like a Windows program, and its
  SHA-256 is the one SHA256SUMS gives for it.

Anything else (no network, GitHub's rate limit, a broken download) leaves MirrorBridge as it is and
is tried again later. A download that breaks off is resumed where it stopped, and an installer
already downloaded and checked is not downloaded again.

The installer (packaging/installer.iss) is started with /UPDATE: it waits for this copy of
MirrorBridge to quit, replaces the exe, and with /RELAUNCH starts the new one. Installing is
updating when MirrorBridge is installed already: the installer finds it by its AppId and writes
over it in its folder, never a second copy, and refuses an older version. A copy run from
elsewhere (the exe in Downloads) starts the installed one when that is as new or newer
(hand_over_to), and otherwise updates into it: the installer moves Start with
Windows over to it, so the old copy does not come back at the next logon.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import APP_NAME, __version__

_LOGGER = logging.getLogger(__name__)

REPO = "draczer01/mirrorbridge"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases?per_page=30"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
DOWNLOADS = f"https://github.com/{REPO}/releases/download/"
SUMS = "SHA256SUMS"
INSTALLER = re.compile(r"MirrorBridge-Setup-(\d+\.\d+\.\d+)\.exe")
TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
# The app checks at most this often by itself; "Check for updates now" any time.
CHECK_EVERY = 24 * 3600
# The installer's own mutex name (installer.iss waits while a MirrorBridge holds it).
APP_MUTEX = "MirrorBridgeAppMutex"
# The uninstall entry Inno Setup writes for the AppId in installer.iss (test_installer.ps1 checks it).
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{8F1C2D7A-4B3E-4E59-9C1A-6D2B5E7F9A10}_is1"

MAX_LIST_BYTES = 4_000_000  # the release list (JSON)
MAX_SUMS_BYTES = 64_000
MAX_INSTALLER_BYTES = 400_000_000
TIMEOUT = 20  # seconds without a byte before a request is given up
ATTEMPTS = 3  # tries of each request, for errors that may pass (network, GitHub's 5xx)
CHUNK = 256 * 1024


class UpdateError(Exception):
    """Why there is no update now. `retry` is False when trying again soon would not help."""

    def __init__(self, message: str, retry: bool = True) -> None:
        super().__init__(message)
        self.retry = retry


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    prerelease: bool
    page: str  # the release on GitHub, for the notes
    installer_name: str
    installer_url: str
    installer_size: int
    sums_url: str


def parse_version(text: str) -> Optional[tuple[int, int, int]]:
    """(X, Y, Z) of "X.Y.Z" or "vX.Y.Z"; None for anything else (a pre-release suffix too)."""
    match = TAG.fullmatch(text if text.startswith("v") else f"v{text}")
    return tuple(int(n) for n in match.groups()) if match else None  # type: ignore[return-value]


def _asset(assets: list, name: str, tag: str) -> Optional[dict]:
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("name") != name:
            continue
        url = asset.get("browser_download_url")
        if isinstance(url, str) and url == f"{DOWNLOADS}{tag}/{name}":
            return asset
    return None


def newest(releases: object, current: str = __version__, prereleases: bool = True) -> Optional[Release]:
    """The newest release in GitHub's release list that is newer than `current` and can be
    installed (see the module's docstring); None when there is none."""
    mine = parse_version(current) or (0, 0, 0)
    best: Optional[tuple[tuple[int, int, int], Release]] = None
    for item in releases if isinstance(releases, list) else []:
        if not isinstance(item, dict) or item.get("draft"):
            continue
        tag = item.get("tag_name")
        version = parse_version(tag) if isinstance(tag, str) and tag.startswith("v") else None
        if version is None or version <= mine:
            continue
        prerelease = bool(item.get("prerelease"))
        if prerelease and not prereleases:
            continue
        text = ".".join(map(str, version))
        assets = item.get("assets") if isinstance(item.get("assets"), list) else []
        installer = _asset(assets, f"MirrorBridge-Setup-{text}.exe", tag)
        sums = _asset(assets, SUMS, tag)
        if installer is None or sums is None:
            _LOGGER.info("Release %s has no installer or %s; skipped", tag, SUMS)
            continue
        size = installer.get("size")
        release = Release(
            version=text, tag=tag, prerelease=prerelease,
            page=item.get("html_url") if isinstance(item.get("html_url"), str) else f"{RELEASES_PAGE}/tag/{tag}",
            installer_name=installer["name"], installer_url=installer["browser_download_url"],
            installer_size=size if isinstance(size, int) and size > 0 else 0,
            sums_url=sums["browser_download_url"],
        )
        if best is None or version > best[0]:
            best = (version, release)
    return best[1] if best else None


# -- network -----------------------------------------------------------------


def _request(url: str, headers: Optional[dict] = None) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{__version__}", **(headers or {})})


def _retrying(what: str, call: Callable[[], bytes], attempts: int = ATTEMPTS, wait: float = 2.0) -> bytes:
    """call(), tried again (after 2 s, then 4 s) when the error may pass."""
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except urllib.error.HTTPError as ex:
            if ex.code in (403, 429):  # GitHub's rate limit for unsigned requests: wait for the next check
                raise UpdateError(f"GitHub refused {what} for now ({ex.code}); trying again later", retry=False) from ex
            if ex.code < 500 or attempt == attempts:
                raise UpdateError(f"{what}: HTTP {ex.code}") from ex
        except (urllib.error.URLError, OSError, TimeoutError) as ex:
            if attempt == attempts:
                raise UpdateError(f"{what}: {getattr(ex, 'reason', ex)}") from ex
        _LOGGER.info("%s failed (try %d of %d); trying again", what, attempt, attempts)
        time.sleep(wait * 2 ** (attempt - 1))
    raise AssertionError("unreachable")


def _read(url: str, limit: int, what: str, headers: Optional[dict] = None) -> bytes:
    def call() -> bytes:
        with urllib.request.urlopen(_request(url, headers), timeout=TIMEOUT) as response:
            data = response.read(limit + 1)
        if len(data) > limit:
            raise UpdateError(f"{what} is larger than {limit:,} bytes", retry=False)
        return data

    return _retrying(what, call)


def fetch_releases(url: str = "") -> list:
    """GitHub's list of MirrorBridge releases, newest first. MIRRORBRIDGE_UPDATE_URL replaces
    the address (for tests, or a mirror of the releases)."""
    url = url or os.environ.get("MIRRORBRIDGE_UPDATE_URL") or RELEASES_API
    data = _read(url, MAX_LIST_BYTES, "the release list",
                 {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        releases = json.loads(data)
    except ValueError as ex:
        raise UpdateError(f"The release list is not JSON: {ex}") from ex
    if not isinstance(releases, list):
        raise UpdateError("The release list is not a list")
    return releases


def for_install(releases: object, current: str = __version__, prereleases: bool = True) -> Optional[Release]:
    """The release to install this copy from: the newest newer one, else this version's own (its
    installer puts this same version in place); None when neither is published."""
    newer = newest(releases, current, prereleases)
    if newer:
        return newer
    own = [item for item in releases if isinstance(item, dict) and item.get("tag_name") == f"v{current}"] \
        if isinstance(releases, list) else []
    return newest(own, "0.0.0", prereleases=True)  # this version, even as a pre-release


def check(current: str = __version__, prereleases: bool = True, url: str = "") -> Optional[Release]:
    """The release to update to, or None when this is the newest; UpdateError when it is not known."""
    release = newest(fetch_releases(url), current, prereleases)
    if release:
        _LOGGER.info("Update: %s %s is out (%s)", APP_NAME, release.version,
                     "pre-release" if release.prerelease else "release")
    else:
        _LOGGER.info("Update: %s %s is the newest", APP_NAME, current)
    return release


# -- download ----------------------------------------------------------------


def parse_sums(text: str) -> dict[str, str]:
    """name -> sha256 from a SHA256SUMS file ("<hex>  <name>" or "<hex> *<name>" lines)."""
    sums = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *](.+)", line.strip())
        if match:
            sums[match.group(2).strip()] = match.group(1).lower()
    return sums


def updates_dir() -> Path:
    from .config import app_data_dir

    path = app_data_dir() / "updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sha256(path: Path) -> str:
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def _good(path: Path, release: Release, expected: str) -> bool:
    if not path.is_file():
        return False
    if release.installer_size and path.stat().st_size != release.installer_size:
        return False
    with open(path, "rb") as f:
        if f.read(2) != b"MZ":
            return False
    return _sha256(path) == expected


def _fetch_to(url: str, part: Path, size: int, progress: Optional[Callable[[int, int], None]],
              cancelled: Callable[[], bool]) -> None:
    """Download url into part, resuming from what part already holds when the server can."""
    have = part.stat().st_size if part.exists() else 0
    if size and have >= size:
        part.unlink()
        have = 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    with urllib.request.urlopen(_request(url, headers), timeout=TIMEOUT) as response:
        if have and response.status != 206:  # no resume: start again
            have = 0
        with open(part, "ab" if have else "wb") as f:
            while True:
                if cancelled():
                    raise UpdateError("The download was stopped", retry=False)
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                have += len(chunk)
                if have > (size or MAX_INSTALLER_BYTES):
                    raise UpdateError("The installer is larger than GitHub said", retry=False)
                f.write(chunk)
                if progress:
                    progress(have, size)
    if size and have < size:  # the connection ended early without an error: resume on the next try
        raise ConnectionError(f"the download stopped at {have:,} of {size:,} bytes")


def download(release: Release, folder: Optional[Path] = None,
             progress: Optional[Callable[[int, int], None]] = None,
             cancelled: Callable[[], bool] = lambda: False) -> Path:
    """The release's installer in folder (the updates folder), checked against its SHA256SUMS."""
    folder = folder or updates_dir()
    folder.mkdir(parents=True, exist_ok=True)
    sums = parse_sums(_read(release.sums_url, MAX_SUMS_BYTES, SUMS).decode("utf-8", "replace"))
    expected = sums.get(release.installer_name)
    if not expected:
        raise UpdateError(f"{SUMS} of {release.tag} has no line for {release.installer_name}", retry=False)
    target = folder / release.installer_name
    if _good(target, release, expected):
        _LOGGER.info("Update: %s was already downloaded and checked", target.name)
        return target
    target.unlink(missing_ok=True)
    part = folder / (release.installer_name + ".part")

    def fetch() -> bytes:
        _fetch_to(release.installer_url, part, release.installer_size, progress, cancelled)
        return b""

    _retrying(f"Downloading {release.installer_name}", fetch)
    if not _good(part, release, expected):
        got = _sha256(part) if part.exists() else "nothing"
        part.unlink(missing_ok=True)  # never resume from bytes that do not check out
        raise UpdateError(f"{release.installer_name} did not match {SUMS} (SHA-256 {got}, expected {expected}): "
                          "it was deleted; trying again later")
    os.replace(part, target)
    _LOGGER.info("Update: downloaded %s, SHA-256 %s as %s says", target.name, expected, SUMS)
    return target


def clean(folder: Optional[Path] = None, keep: str = "") -> None:
    """Delete older installers and broken downloads from the updates folder, but `keep`."""
    folder = folder or updates_dir()
    for path in folder.glob("MirrorBridge-Setup-*"):
        if path.name in (keep, keep + ".part"):
            continue
        try:
            path.unlink()
        except OSError:
            pass  # in use: next time


# -- install -----------------------------------------------------------------


def _uninstall_value(name: str) -> Optional[str]:
    """A value of the uninstall entry the installer wrote (installer.iss's AppId); None without one."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return value if isinstance(value, str) and value else None


def installed_exe() -> Optional[Path]:
    """MirrorBridge.exe where the installer put it, when it is installed and the file is there."""
    folder = _uninstall_value("InstallLocation")
    exe = Path(folder) / "MirrorBridge.exe" if folder else None
    return exe if exe and exe.is_file() else None


def installed_version() -> Optional[str]:
    return _uninstall_value("DisplayVersion")


def is_installed_copy(exe: Optional[Path] = None) -> bool:
    """This process is the installed MirrorBridge.exe (not a copy run from Downloads or elsewhere)."""
    exe = exe or installed_exe()
    if exe is None or not getattr(sys, "frozen", False):
        return False
    try:
        return Path(sys.executable).resolve() == exe.resolve()
    except OSError:
        return False


def hand_over_to(installed: Optional[Path], version: Optional[str], current: str = __version__) -> bool:
    """Whether a copy run from elsewhere should start the installed one instead: there is one, and it
    is this version or newer (an older one is updated over by this copy's updates instead)."""
    if installed is None or version is None:
        return False
    theirs, mine = parse_version(version), parse_version(current)
    return theirs is not None and mine is not None and theirs >= mine


def can_install() -> bool:
    """An installer can update this copy: the Windows exe (from source, the release page opens)."""
    return sys.platform == "win32" and getattr(sys, "frozen", False)


def install_command(installer: Path, relaunch: bool = True) -> list[str]:
    command = [str(installer), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/UPDATE",
               f"/LOG={installer.with_suffix('.log')}"]
    return command + (["/RELAUNCH"] if relaunch else [])


def start_installer(installer: Path, relaunch: bool = True) -> None:
    """Start the installer on its own, so it goes on after MirrorBridge quits (which it waits for)."""
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    _LOGGER.info("Update: starting %s", " ".join(install_command(installer, relaunch)))
    subprocess.Popen(install_command(installer, relaunch), close_fds=True, creationflags=flags,  # pylint: disable=consider-using-with
                     cwd=str(installer.parent))


_mutex = None


def hold_app_mutex() -> None:
    """Say that MirrorBridge is running, to the installer (installer.iss waits for it to quit, and
    asks the user to quit it when installing by hand). Held until the process ends."""
    global _mutex  # pylint: disable=global-statement
    if sys.platform != "win32" or _mutex is not None:
        return
    import ctypes

    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, APP_MUTEX) or None
