"""Write THIRD-PARTY-NOTICES.txt and a licenses folder for everything inside MirrorBridge.exe.

Run it with the Python that built the exe, from the repo root, after PyInstaller:

    python packaging/third_party_notices.py --out build/notices
    python packaging/third_party_notices.py --ffmpeg-version    # e.g. 8.1.1: whose source to publish

What is inside the exe comes from PyInstaller's own lists of what it packed (PKG-00.toc and
PYZ-00.toc in build/pyinstaller/MirrorBridge), checked against the exe itself. Each file is put
down to whoever made it: a Python package (by the RECORD in its dist-info), CPython and the
libraries that come with it, Microsoft's C runtime, FFmpeg or PyInstaller. The licence texts come
from those packages' dist-info, the interpreter's own licence file, conda's package folders when
the Python is a conda one, and packaging/licenses for the GNU licences that Qt names but does not
ship. A file it cannot put down to anyone stops it, so no notice goes missing quietly.
"""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import textwrap
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from check_version import code_version
from fetch_ffmpeg import load_pin

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "pyinstaller" / "MirrorBridge"
EXE = ROOT / "dist" / "MirrorBridge.exe"
GNU_LICENSES = Path(__file__).resolve().parent / "licenses"  # GNU texts some packages name but lack
FAIRPLAY = ROOT / "mirrorbridge" / "fairplay"  # MirrorBridge's port of doubletake's FairPlay SAP: LGPL-3.0
FAIRPLAY_TEXTS = ("LICENSE", "COPYING.GPL")
FAIRPLAY_FOLDER = "doubletake-fairplay"
SOURCE_URL = "https://github.com/draczer01/mirrorbridge"
FFMPEG_IN_EXE = "ffmpeg/ffmpeg.exe"  # where MirrorBridge.spec puts it
# PyInstaller's launcher, loader and runtime hooks are inside every exe it makes; the rest of
# these packages only builds it. They get their own section instead of a place among the packages.
LAUNCHER = ("pyinstaller", "pyinstaller-hooks-contrib")
LAUNCHER_LICENCES = {
    "pyinstaller": "GPL-2.0-or-later WITH Bootloader-exception (its runtime hooks: Apache-2.0)",
    "pyinstaller-hooks-contrib": "Apache-2.0 (its runtime hooks, the only part inside the exe)",
}

# Microsoft's runtime DLLs, whoever ships a copy (the interpreter, PySide6, numpy...).
VC_RUNTIME = re.compile(r"(vcruntime140(_1|_threads)?|msvcp140(_1|_2|_atomic_wait|_codecvt_ids)?|concrt140"
                        r"|vcomp140|vccorlib140|vcamp140)(-[0-9a-f]+)?\.dll", re.IGNORECASE)
UCRT = re.compile(r"(ucrtbase|api-ms-win-[a-z0-9-]+)\.dll", re.IGNORECASE)
MICROSOFT = {  # is it the VC++ runtime (or else the Universal C Runtime)?
    True: ("Microsoft Visual C++ Redistributable", "https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist"),
    False: ("Microsoft Universal C Runtime", "https://learn.microsoft.com/cpp/windows/universal-crt-deployment"),
}
# Libraries that come with the interpreter on Windows (python.org puts them in DLLs, conda in
# Library\bin), by file name: name, licence, home page, and the word that names it in CPython's
# licence file, which on python.org builds carries their licence texts too.
NATIVE = (
    (re.compile(r"lib(crypto|ssl)-\d[\w-]*\.dll", re.I), "OpenSSL", "Apache-2.0", "https://www.openssl.org/", "openssl"),
    (re.compile(r"(lib)?ffi-\d+\.dll", re.I), "libffi", "MIT", "https://sourceware.org/libffi/", "libffi"),
    (re.compile(r"zlib1?\.dll", re.I), "zlib", "Zlib", "https://zlib.net/", "zlib"),
    (re.compile(r"libbz2\.dll", re.I), "bzip2", "bzip2-1.0.6", "https://sourceware.org/bzip2/", "bzip2"),
    (re.compile(r"liblzma\.dll", re.I), "XZ Utils", "0BSD", "https://tukaani.org/xz/", "xz"),
    (re.compile(r"libexpat\.dll", re.I), "Expat", "MIT", "https://libexpat.github.io/", "expat"),
    (re.compile(r"sqlite3\.dll", re.I), "SQLite", "blessing", "https://www.sqlite.org/", "sqlite"),
    (re.compile(r"t(cl|k)\d+t?\.dll", re.I), "Tcl/Tk", "TCL", "https://www.tcl-lang.org/", "tcl"),
)
GNU_TITLES = {
    "GPL-3.0": re.compile(r"GNU GENERAL PUBLIC LICENSE\s+Version 3"),
    "LGPL-3.0": re.compile(r"GNU LESSER GENERAL PUBLIC LICENSE\s+Version 3"),
    "GPL-2.0": re.compile(r"GNU GENERAL PUBLIC LICENSE\s+Version 2"),
    "LGPL-2.1": re.compile(r"GNU LESSER GENERAL PUBLIC LICENSE\s+Version 2\.1"),
}
# Who made an FFmpeg build, from its version line: name, and where the build and the libraries
# it links are described.
FFMPEG_PROVIDERS = {"gyan.dev": ("Gyan Doshi (www.gyan.dev)", "https://www.gyan.dev/ffmpeg/builds/")}


@dataclass(frozen=True)
class Entry:
    """One thing PyInstaller packed: its name inside the exe, the file it came from, its kind."""

    name: str
    source: str
    kind: str


@dataclass
class Component:
    """Something inside the exe that has a licence of its own."""

    name: str
    version: str = ""
    licence: str = ""
    url: str = ""
    texts: dict[str, Path] = field(default_factory=dict)  # file name under licenses/<folder>/ -> file
    texts_in: str = ""  # another component whose licence file also holds this one's
    contents: set[str] = field(default_factory=set)  # its names inside the exe
    notes: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return f"{self.name} {self.version}".strip()

    @property
    def folder(self) -> str:
        return re.sub(r"[^A-Za-z0-9._+-]+", "-", self.title.replace(" ", "-"))


@dataclass
class FFmpegBuild:
    """What `ffmpeg -version` says about a build."""

    banner: str  # its first line
    release: str | None  # 7.1, or None for a build from a git snapshot
    configuration: str
    built_with: str = ""

    @property
    def licence(self) -> str:
        options = set(self.configuration.split())
        if "--enable-nonfree" in options:
            raise SystemExit("This FFmpeg was configured with --enable-nonfree: it may not be redistributed")
        gpl, version3 = "--enable-gpl" in options, "--enable-version3" in options
        return {(True, True): "GPL-3.0-or-later", (True, False): "GPL-2.0-or-later",
                (False, True): "LGPL-3.0-or-later", (False, False): "LGPL-2.1-or-later"}[(gpl, version3)]

    @property
    def libraries(self) -> list[str]:
        """The external libraries its configuration line enables."""
        external = {"gnutls", "gmp", "iconv", "fontconfig", "bzlib", "lzma", "zlib", "sdl2", "avisynth", "cairo", "openal"}
        names = [option.removeprefix("--enable-") for option in self.configuration.split()]
        return [name for name in names if name.startswith("lib") or name in external]

    @property
    def provider(self) -> tuple[str, str] | None:
        return next((who for key, who in FFMPEG_PROVIDERS.items() if key in self.banner), None)


def parse_ffmpeg_version(text: str) -> FFmpegBuild:
    """Read `ffmpeg -version`. The release is the number an official source tarball has
    (ffmpeg-7.1.tar.xz): "7.1-essentials_build-www.gyan.dev" and "n7.1" are 7.1, while
    "n7.1-153-g1234abc" (153 commits later) and "N-118000-g..." are git snapshots, with none."""
    lines = text.strip().splitlines()
    banner = lines[0].strip() if lines else ""
    version = re.match(r"ffmpeg version (\S+)", banner)
    if not version:
        raise SystemExit(f"Not FFmpeg's version output: {banner!r}")
    release = re.fullmatch(r"n?(\d+\.\d+(?:\.\d+)?)(-.*)?", version.group(1))
    snapshot = release and re.match(r"-\d+-g[0-9a-f]{7,}", release.group(2) or "")
    configuration = next((line.split(":", 1)[1].strip() for line in lines if line.startswith("configuration:")), "")
    built_with = next((line.strip() for line in lines if line.startswith("built with")), "")
    return FFmpegBuild(banner, release.group(1) if release and not snapshot else None, configuration, built_with)


def run_ffmpeg_version(ffmpeg: str) -> str:
    return subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, timeout=120, check=True).stdout


def _toc(path: Path) -> list[tuple[str, str, str]]:
    """The (name, source, kind) list in a .toc file PyInstaller wrote (a Python literal)."""
    for item in ast.literal_eval(path.read_text(encoding="utf-8")):
        if isinstance(item, list) and item and all(isinstance(e, tuple) and len(e) == 3 for e in item):
            return item
    raise SystemExit(f"{path} holds no list of contents")


def read_build(build: Path) -> list[Entry]:
    """Everything PyInstaller packed into the exe, Python modules included."""
    for name in ("PKG-00.toc", "PYZ-00.toc"):
        if not (build / name).is_file():
            raise SystemExit(f"{build / name} is missing: build MirrorBridge.exe first")
    entries = [Entry(*e) for e in _toc(build / "PKG-00.toc") if e[2] not in ("OPTION", "PYZ")]
    return entries + [Entry(*e) for e in _toc(build / "PYZ-00.toc")]


def check_exe(build: Path, exe: Path) -> None:
    """Stop unless the exe holds exactly what the build folder lists, file for file and size for size,
    so the notices describe this exe and not an older build."""
    from PyInstaller.archive.readers import CArchiveReader  # pylint: disable=import-outside-toplevel

    archive = CArchiveReader(str(exe)).toc  # name -> (offset, length, unpacked length, compressed, type code)
    toc = [e for e in _toc(build / "PKG-00.toc") if e[2] != "OPTION"]
    # The Python modules' archive is listed as PYZ-00.pyz but packed under another name (PYZ.pyz).
    packed = {name: entry[2] for name, entry in archive.items() if entry[4] != "z"}
    listed = {name: source for name, source, kind in toc if kind != "PYZ"}
    if set(packed) != set(listed):
        differ = sorted(set(packed) ^ set(listed))
        raise SystemExit(f"{exe} and {build} list different files ({', '.join(differ[:5])}...): rebuild the exe")
    stale = [name for name, source in listed.items()
             if os.path.isfile(source) and not source.endswith((".py", ".pyc")) and os.path.getsize(source) != packed[name]]
    pyz = sorted(entry[2] for entry in archive.values() if entry[4] == "z")
    if pyz != sorted(os.path.getsize(source) for _, source, kind in toc if kind == "PYZ"):
        stale.append("the Python modules")
    if stale:
        raise SystemExit(f"{exe} was not built from the files in {build} ({', '.join(stale[:5])}): rebuild the exe")


def _key(path: str | os.PathLike) -> str:
    return os.path.normcase(os.path.abspath(path))


def _under(path: str, folder: str | os.PathLike) -> bool:
    folder = _key(folder)
    return _key(path).startswith(folder.rstrip("\\/") + os.sep)


def _record_hash(path: str) -> str:
    with open(path, "rb") as f:
        return base64.urlsafe_b64encode(hashlib.file_digest(f, "sha256").digest()).rstrip(b"=").decode()


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


class Environment:
    """The Python that built the exe: its packages, its own files and what they are."""

    def __init__(self, paths: list[str] | None = None, base_prefix: str | None = None,
                 app_dirs: list[Path] | None = None, build: Path = BUILD, versions: dict[str, str] | None = None):
        self.base_prefix = Path(base_prefix or sys.base_prefix)
        self.app_dirs = app_dirs if app_dirs is not None else [ROOT / "mirrorbridge", ROOT / "packaging"]
        self.build = build
        self.versions = versions if versions is not None else _runtime_versions()
        # Folders that hold installed packages: not the standard library, nor the interpreter's
        # own folder, which Windows puts on sys.path too.
        unique: dict[str, str] = {}
        for path in paths if paths is not None else sys.path:
            if path:
                unique.setdefault(_key(path), path)
        self.site_dirs = [p for p in unique.values()
                          if os.path.isdir(p) and any(n.endswith(".dist-info") for n in os.listdir(p))]
        self.dists = list(metadata.distributions(path=self.site_dirs))
        self._names = {id(d): d.metadata["Name"] for d in self.dists}
        self._owners: dict[str, list[metadata.Distribution]] = {}
        for dist in self.dists:
            for f in dist.files or ():
                self._owners.setdefault(_key(dist.locate_file(f)), []).append(dist)
        self._last: dict[tuple[int, ...], metadata.Distribution] = {}
        self.conda = _conda_files(self.base_prefix)

    def name_of(self, dist: metadata.Distribution) -> str:
        return self._names[id(dist)]

    def find(self, name: str) -> metadata.Distribution | None:
        found = [d for d in self.dists if normalise(self.name_of(d)) == normalise(name)]
        return self._installed(found) if len(found) > 1 else (found or [None])[0]

    def package_of(self, source: str) -> metadata.Distribution | None:
        """The installed package that a file belongs to, by the RECORD of its dist-info. When two
        dist-infos claim it, as when an upgrade left the old one behind, the one installed last."""
        owners = self._owners.get(_key(source), [])
        return self._installed(owners) if len(owners) > 1 else (owners or [None])[0]

    def _installed(self, candidates: list[metadata.Distribution]) -> metadata.Distribution:
        """Of several dist-infos for one package, the one that was installed last: of the files they
        record with different hashes, the copies on disk match its hashes."""
        if len({(normalise(self.name_of(d)), d.version) for d in candidates}) == 1:
            return candidates[0]
        ids = tuple(sorted(id(d) for d in candidates))
        if ids not in self._last:
            self._last[ids] = self._decide(candidates)
        return self._last[ids]

    def _decide(self, candidates: list[metadata.Distribution]) -> metadata.Distribution:
        candidates = list({(normalise(self.name_of(d)), d.version): d for d in candidates}.values())
        # Each one's recorded hash by file, keyed by _key; the file itself by the path as written, which
        # Wine's case-sensitive Z: drive needs.
        recorded = [{_key(d.locate_file(f)): (str(d.locate_file(f)), f.hash.value) for f in d.files or () if f.hash}
                    for d in candidates]
        differ = [recorded[0][key][0] for key in set.intersection(*(set(r) for r in recorded))
                  if len({r[key][1] for r in recorded}) > 1 and os.path.isfile(recorded[0][key][0])]
        wins = [0] * len(candidates)
        for path in sorted(differ, key=os.path.getsize)[:50]:
            digest = _record_hash(path)
            wins = [w + (r[_key(path)][1] == digest) for w, r in zip(wins, recorded)]
        if sorted(wins)[-1] in (0, sorted(wins)[-2]):
            raise SystemExit("Cannot tell which of these is installed: "
                             + ", ".join(f"{self.name_of(d)} {d.version}" for d in candidates))
        return candidates[wins.index(max(wins))]

    def in_site_dir(self, source: str) -> bool:
        return any(_under(source, d) for d in self.site_dirs)

    def cpython_licence(self) -> Path:
        candidates = [self.base_prefix / n for n in ("LICENSE.txt", "LICENSE_PYTHON.txt", "LICENSE")]
        candidates.append(Path(sysconfig.get_paths()["stdlib"]) / "LICENSE.txt")
        found = next((p for p in candidates if p.is_file()), None)
        if found is None:
            raise SystemExit(f"No licence file for CPython in {self.base_prefix}")
        return found


def _runtime_versions() -> dict[str, str]:
    """Versions of the libraries the running interpreter loaded, which are the ones the exe has."""
    found = {"CPython": platform.python_version()}
    for name, read in (("OpenSSL", lambda: __import__("ssl").OPENSSL_VERSION.split()[1]),
                       ("zlib", lambda: __import__("zlib").ZLIB_RUNTIME_VERSION),
                       ("SQLite", lambda: __import__("sqlite3").sqlite_version),
                       ("Expat", lambda: __import__("pyexpat").EXPAT_VERSION.removeprefix("expat_"))):
        try:
            found[name] = read()
        except (ImportError, AttributeError, IndexError):
            pass
    return found


def _conda_files(prefix: Path) -> dict[str, dict]:
    """For a conda Python, which conda package installed each file (conda-meta/*.json)."""
    files: dict[str, dict] = {}
    for record in sorted((prefix / "conda-meta").glob("*.json")):
        info = json.loads(record.read_text(encoding="utf-8"))
        for name in info.get("files", ()):
            files[_key(prefix / name)] = info
    return files


def _conda_texts(info: dict) -> dict[str, Path]:
    """The licence texts conda keeps for a package in its package cache (plain text only)."""
    if not info.get("extracted_package_dir"):
        return {}
    folder = Path(info["extracted_package_dir"]) / "info" / "licenses"
    return {p.relative_to(folder).as_posix(): p for p in sorted(folder.rglob("*"))
            if p.is_file() and p.suffix.lower() not in (".docx", ".rtf", ".pdf")}


def licence_of(dist: metadata.Distribution) -> str:
    """A package's licence: License-Expression, else a one-line License, else its classifiers."""
    md = dist.metadata
    if md.get("License-Expression"):
        return md["License-Expression"].strip()
    text = (md.get("License") or "").strip()
    if text and "\n" not in text and len(text) <= 120:
        return text
    classifiers = [c.split(" :: ")[-1] for c in md.get_all("Classifier") or () if c.startswith("License ::")]
    return " / ".join(classifiers) or "see its licence texts"


def url_of(dist: metadata.Distribution) -> str:
    md = dist.metadata
    urls = dict(u.split(", ", 1) for u in md.get_all("Project-URL") or () if ", " in u)
    for label in ("Homepage", "homepage", "Home", "Source", "Source Code", "Repository", "GitHub"):
        if urls.get(label):
            return urls[label].strip()
    return (md.get("Home-page") or "").strip() or f"https://pypi.org/project/{md['Name']}/"


def licence_files(dist: metadata.Distribution) -> dict[str, Path]:
    """The licence, notice and author files in a package's dist-info, by their path inside it."""
    found: dict[str, Path] = {}
    for f in dist.files or ():
        parts = f.parts
        if len(parts) < 2 or not parts[0].endswith(".dist-info"):
            continue
        if parts[1] == "licenses" and len(parts) > 2:
            found["/".join(parts[2:])] = Path(dist.locate_file(f))
        elif re.match(r"(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", parts[-1], re.IGNORECASE):
            found["/".join(parts[1:])] = Path(dist.locate_file(f))
    return dict(sorted(found.items()))


def add_gnu_texts(component: Component) -> None:
    """Make sure the full GNU licence texts its licence names are with it: the LGPL v3 is a set of
    extra permissions on top of the GPL v3, so it needs both. Qt's packages ship neither."""
    licence = component.licence
    needed = []
    if re.search(r"LGPL-?v?3", licence, re.IGNORECASE):
        needed += ["LGPL-3.0", "GPL-3.0"]
    elif re.search(r"(?<!L)GPL-?v?3", licence, re.IGNORECASE):
        needed.append("GPL-3.0")
    if component.name == "FFmpeg":  # its configuration decides, and nothing else ships a text for it
        needed += {"GPL-2.0-or-later": ["GPL-2.0"], "LGPL-2.1-or-later": ["LGPL-2.1"]}.get(licence, [])
    texts = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in component.texts.values())
    for spdx in needed:
        if GNU_TITLES[spdx].search(texts):
            continue
        text = GNU_LICENSES / f"{spdx}.txt"
        if not text.is_file():
            raise SystemExit(f"{component.title} needs the {spdx} text: add it as {text}")
        component.texts[text.name] = text
        texts += text.read_text(encoding="utf-8")


@dataclass
class Notices:
    """Everything inside the exe, grouped the way THIRD-PARTY-NOTICES.txt lists it."""

    ffmpeg: Component
    ffmpeg_build: FFmpegBuild
    cpython: Component
    natives: dict[str, Component]
    microsoft: dict[str, Component]
    packages: dict[str, Component]
    launcher: dict[str, Component]


def collect(entries: list[Entry], env: Environment, ffmpeg_build: FFmpegBuild, origin: str = "") -> Notices:
    """Put each entry down to the component it belongs to; stops on any it cannot place."""
    cpython = Component("CPython", env.versions.get("CPython", ""), "PSF-2.0", "https://www.python.org/")
    cpython.texts["LICENSE.txt"] = env.cpython_licence()
    cpython_text = cpython.texts["LICENSE.txt"].read_text(encoding="utf-8", errors="replace")
    docs = ".".join(cpython.version.split(".")[:2]) or "3"
    cpython.notes.append("Python's own modules also contain code from other projects, such as libmpdec in _decimal, "
                         "HACL* in hashlib and the Mersenne Twister in random. The Python documentation lists them "
                         f"with their licences: https://docs.python.org/{docs}/license.html")
    ffmpeg = Component("FFmpeg", ffmpeg_build.release or "", ffmpeg_build.licence, "https://ffmpeg.org/",
                       notes=[origin] if origin else [])
    notices = Notices(ffmpeg, ffmpeg_build, cpython, {}, {}, {}, {})
    found: dict[str, tuple[metadata.Distribution | None, set[str]]] = {}  # package -> its files inside the exe
    unknown = []

    def library(group: dict[str, Component], name: str, licence: str, url: str, mention: str, source: str):
        component = group.setdefault(name, Component(name, env.versions.get(name, ""), licence, url))
        conda = env.conda.get(_key(source))
        if conda and not component.texts:  # the copy conda installed says which version, and has the texts
            component.version = conda.get("version", "")
            component.licence = conda.get("license") or licence
            component.texts.update(_conda_texts(conda))
        if not component.texts and mention and re.search(rf"\b{mention}", cpython_text, re.IGNORECASE):
            component.texts_in = cpython.folder
        elif component.texts:
            component.texts_in = ""
        return component

    for entry in entries:
        name, source = entry.name.replace("\\", "/"), entry.source
        base = name.rsplit("/", 1)[-1]
        microsoft = VC_RUNTIME.fullmatch(base) or UCRT.fullmatch(base)
        if source in ("", "-"):  # a namespace package: its modules say where they come from
            continue
        if name == FFMPEG_IN_EXE:
            ffmpeg.contents.add(name)
        elif _under(source, env.build):  # made while building: the launcher's loader, the compiled stdlib
            if name.startswith("pyimod"):
                found.setdefault("pyinstaller", (None, set()))[1].add(name)
            elif name == "base_library.zip" or entry.kind == "PYMODULE":
                cpython.contents.add(name)
            else:
                unknown.append(entry)
        elif any(_under(source, d) for d in env.app_dirs):
            pass  # MirrorBridge itself
        elif microsoft:  # whoever ships a copy, it is Microsoft's
            label, url = MICROSOFT[bool(VC_RUNTIME.fullmatch(base))]
            library(notices.microsoft, label, "Microsoft Software License Terms", url, "Microsoft Distributable Code",
                    source).contents.add(name)
        elif (dist := env.package_of(source)) is not None:
            key = normalise(env.name_of(dist))
            if found.get(key, (None,))[0] is None:
                found[key] = (dist, found.get(key, (None, set()))[1])
            found[key][1].add(name)
        elif known := next((n for n in NATIVE if n[0].fullmatch(base)), None):
            library(notices.natives, *known[1:], source).contents.add(name)
        elif (conda := env.conda.get(_key(source))) and conda.get("name") not in ("python", "libpython"):
            library(notices.natives, conda["name"], conda.get("license", ""), conda.get("url", ""), "",
                    source).contents.add(name)
        elif _under(source, env.base_prefix) and not env.in_site_dir(source):
            cpython.contents.add(name)  # the interpreter, its standard library and extension modules
        else:
            unknown.append(entry)
    if unknown:
        raise SystemExit("Cannot tell whose these files inside the exe are:\n"
                         + "\n".join(f"  {e.name} ({e.kind}) from {e.source}" for e in unknown))

    found.setdefault("pyinstaller", (None, set()))  # its launcher starts every exe, whatever else is in it
    launcher = {normalise(n) for n in LAUNCHER}
    for key, (dist, files) in sorted(found.items()):
        dist = dist or env.find(key)
        if dist is None:
            raise SystemExit(f"The exe has files of {key}, but {key} is not installed in this Python")
        component = Component(env.name_of(dist), dist.version, LAUNCHER_LICENCES.get(key) or licence_of(dist),
                              url_of(dist), licence_files(dist), contents=files)
        (notices.launcher if key in launcher else notices.packages)[key] = component
    for component in [ffmpeg, cpython, *notices.natives.values(), *notices.packages.values(), *notices.launcher.values()]:
        add_gnu_texts(component)
    return notices


def _paragraph(text: str, indent: str = "", hang: str | None = None) -> list[str]:
    """text wrapped at 90 columns; later lines are indented by hang (default: as much as the first)."""
    return textwrap.wrap(text, width=90, initial_indent=indent, subsequent_indent=indent if hang is None else hang,
                         break_long_words=False, break_on_hyphens=False) or [""]


def _is_qt(component: Component) -> bool:
    return normalise(component.name).startswith(("pyside6", "shiboken6"))


def _texts_line(component: Component, folder: str = "licenses") -> str:
    if component.texts:
        return ", ".join(f"{folder}/{component.folder}/{name}" for name in component.texts)
    if component.texts_in:
        return f"in {folder}/{component.texts_in}/LICENSE.txt, with CPython's"
    return f"not shipped by the package; see {component.url}"


def _entry(component: Component, extra: list[str] = ()) -> list[str]:
    lines = [component.title]
    lines += [f"  Licence:  {component.licence}"] if component.licence else []
    lines += [f"  Website:  {component.url}"] if component.url else []
    lines += _paragraph(f"Licence texts: {_texts_line(component)}", "  ", "    ")
    for note in [*component.notes, *extra]:
        lines += _paragraph(note, "  ", "    ")
    return lines + [""]


def render(notices: Notices, app_version: str) -> str:
    """THIRD-PARTY-NOTICES.txt."""
    ffmpeg, build = notices.ffmpeg, notices.ffmpeg_build
    lgpl = [c for c in notices.packages.values() if re.search(r"LGPL", c.licence, re.IGNORECASE)]
    sections: list[tuple[str, list[str]]] = []

    lines = []
    lines += ["What ffmpeg.exe -version prints:", ""]
    lines += [f"  {line}" for line in (build.banner, build.built_with, f"configuration: {build.configuration}") if line]
    lines += [""]
    reasons = [o for o in ("--enable-gpl", "--enable-version3") if o in build.configuration.split()]
    because = f", because it was configured with {' and '.join(reasons)}" if reasons else ""
    lines += _paragraph(f"Licence: {ffmpeg.licence}{because}")
    lines += _paragraph(f"Licence texts: {_texts_line(ffmpeg)}") + [""]
    for note in ffmpeg.notes:
        lines += _paragraph(note) + [""]
    if build.release:
        tarball = f"ffmpeg-{build.release}.tar.xz"
        lines += _paragraph(f"Source code: FFmpeg's official source for this release, {tarball}, is attached to "
                            f"the same MirrorBridge release as this file ({SOURCE_URL}/releases/tag/v{app_version}). "
                            f"FFmpeg publishes the same file at https://ffmpeg.org/releases/{tarball}.") + [""]
    provider = build.provider
    if build.libraries:
        lines += _paragraph("This FFmpeg is linked with these libraries, each under its own licence: "
                            + ", ".join(build.libraries) + ".")
        lines += _paragraph(f"The build provider, {provider[0]}, lists their versions and where each one's source "
                            f"code is at {provider[1]}." if provider else
                            "Their versions are those the build provider used (see the version line).") + [""]
    sections.append((f"FFmpeg {build.release or ''}".strip() + " (ffmpeg\\ffmpeg.exe inside MirrorBridge.exe)", lines))

    if lgpl:
        lines = []
        if any(_is_qt(c) for c in lgpl):
            lines += _paragraph("Qt and Qt for Python (PySide6, shiboken6) are offered under the LGPL-3.0, the GPL-2.0 "
                                "or the GPL-3.0, or under a commercial licence from The Qt Company; MirrorBridge uses "
                                "them under the LGPL-3.0. Qt itself contains third-party code under other licences, "
                                "listed at https://doc.qt.io/qt-6/licenses-used-in-qt.html.") + [""]
        lines += _paragraph("Under the LGPL you may use MirrorBridge with your own or a changed version of these "
                            "libraries:") + [""]
        for component in lgpl:
            source = ("https://download.qt.io/official_releases/QtForPython/ (Qt itself: "
                      "https://download.qt.io/official_releases/qt/)" if _is_qt(component)
                      else f"https://pypi.org/project/{component.name}/{component.version}/")
            lines += _paragraph(f"{component.title} ({component.licence}). Licence texts: {_texts_line(component)}. "
                                f"Source: {source}", "  * ", "    ")
        lines += [""] + _paragraph(
            f"MirrorBridge's full source code and build scripts are public at {SOURCE_URL}. To rebuild "
            f"MirrorBridge.exe with your own Qt or another of these libraries: get the source of this version "
            f"(tag v{app_version}), put your library in place of the pinned one in packaging/requirements-build.txt, "
            "and run packaging\\build_exe.ps1 (README.md, \"Building MirrorBridge.exe\"). MirrorBridge.exe unpacks "
            "these libraries into a temporary folder and loads them from there each time it starts, so the rebuilt "
            "exe uses yours.") + [""]
        sections.append(("Qt for Python and the other LGPL libraries", lines))

    lines = _paragraph("Screen mirroring to Apple TVs uses FairPlay SAP, the handshake Apple's receivers ask of a "
                       "sender. MirrorBridge's code for it (the mirrorbridge.fairplay package) is a Python port of "
                       "the Go code of doubletake by Omar Roth and its contributors "
                       "(https://github.com/omarroth/doubletake), and like doubletake it is under the LGPL-3.0, not "
                       "MIT. Licence texts: licenses/" + FAIRPLAY_FOLDER + "/. Its source is in MirrorBridge's "
                       f"repository ({SOURCE_URL}, folder mirrorbridge/fairplay); to use MirrorBridge with a changed "
                       "version of it, change that folder and rebuild MirrorBridge.exe as described above for the "
                       "LGPL libraries.") + [""]
    sections.append(("FairPlay SAP, ported from doubletake (LGPL-3.0)", lines))

    lines = _entry(notices.cpython)
    if notices.natives:
        lines += _paragraph("Libraries that come with this Python:") + [""]
        for component in notices.natives.values():
            lines += _entry(component)
    sections.append(("Python (CPython) and the libraries that come with it", lines))

    if notices.microsoft:
        lines = _paragraph("Microsoft's C and C++ runtime DLLs, which Microsoft lets programs built with its "
                           "compilers ship with them. They stay under Microsoft's licence terms; they are not "
                           "open source.") + [""]
        for component in notices.microsoft.values():
            lines += _entry(component, [f"Files: {', '.join(sorted(component.contents))}"])
        sections.append(("Microsoft C and C++ runtime", lines))

    lines = []
    for component in notices.packages.values():
        lines += _entry(component)
    sections.append((f"Python packages ({len(notices.packages)})", lines))

    lines = _paragraph("PyInstaller made MirrorBridge.exe. Its launcher, which starts the exe and unpacks the rest, "
                       "its loader and its runtime hooks are inside every exe it makes; their licences allow "
                       "that for programs under any licence.") + [""]
    for component in notices.launcher.values():
        lines += _entry(component)
    sections.append(("PyInstaller", lines))

    lines = _paragraph("Extend screen uses Parsec's free virtual display driver. It is not inside MirrorBridge.exe "
                       "and not part of this release: when you choose Extend screen and agree, MirrorBridge downloads "
                       "Parsec's own installer from parsec.app, and you install the driver under Parsec's terms "
                       "(https://parsec.app/terms).") + [""]
    sections.append(("Not included: Parsec Virtual Display Driver", lines))

    title = f"MirrorBridge {app_version}: third-party software inside MirrorBridge.exe"
    out = [title, "=" * len(title), ""]
    out += _paragraph("MirrorBridge itself is under the MIT licence (see LICENSE). MirrorBridge.exe also contains the "
                      "software below, each under its own licence, whose full texts are in the licenses folder next "
                      "to this file. The list was made from the build itself, by packaging/third_party_notices.py.")
    out += [""] + _paragraph(f"MirrorBridge's source code, with everything needed to build MirrorBridge.exe: {SOURCE_URL}")
    out += [""]
    for number, (heading, _) in enumerate(sections, 1):
        out.append(f"  {number}. {heading}")
    out.append("")
    for number, (heading, body) in enumerate(sections, 1):
        out += ["", f"{number}. {heading}", "-" * (len(heading) + len(str(number)) + 2), ""] + body
    return "\n".join(line.rstrip() for line in out).rstrip() + "\n"


def write(notices: Notices, out: Path, app_version: str) -> Path:
    """THIRD-PARTY-NOTICES.txt and licenses/ in out; returns the notices file."""
    out.mkdir(parents=True, exist_ok=True)
    licenses = out / "licenses"
    if licenses.exists():
        shutil.rmtree(licenses)
    for component in [notices.ffmpeg, notices.cpython, *notices.natives.values(), *notices.microsoft.values(),
                      *notices.packages.values(), *notices.launcher.values()]:
        for name, source in component.texts.items():
            dest = licenses / component.folder / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
    (licenses / FAIRPLAY_FOLDER).mkdir(parents=True, exist_ok=True)
    for name in FAIRPLAY_TEXTS:
        shutil.copyfile(FAIRPLAY / name, licenses / FAIRPLAY_FOLDER / name)
    path = out / "THIRD-PARTY-NOTICES.txt"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(render(notices, app_version))
    return path


def ffmpeg_in_build(entries: list[Entry]) -> str:
    source = next((e.source for e in entries if e.name.replace("\\", "/") == FFMPEG_IN_EXE), None)
    if source is None:
        raise SystemExit(f"The build has no {FFMPEG_IN_EXE}")
    return source


def ffmpeg_origin(ffmpeg: str, build: FFmpegBuild, pin: dict | None = None) -> str:
    """Where the bundled FFmpeg came from, when it is the pinned download (packaging/ffmpeg_pin.json)
    or the one in the imageio-ffmpeg wheel."""
    pin = pin or load_pin()
    with open(ffmpeg, "rb") as f:
        pinned = hashlib.file_digest(f, "sha256").hexdigest() == pin["exe_sha256"]
    if pinned:
        made_by = build.provider[0] if build.provider else pin["provider"]
        return (f"This is the ffmpeg.exe from {pin['archive_url']} (SHA-256 {pin['archive_sha256']}), the "
                f"{pin['build']} build of FFmpeg {pin['version']} made by {made_by} from FFmpeg's git commit "
                f"{pin['source_commit']} (tag n{pin['version']}). MirrorBridge only starts it as a "
                "separate program.")
    try:
        import imageio_ffmpeg  # pylint: disable=import-outside-toplevel

        theirs = imageio_ffmpeg.get_ffmpeg_exe()
        same = _record_hash(theirs) == _record_hash(ffmpeg)
        wheel = metadata.version("imageio-ffmpeg")
    except Exception:  # pylint: disable=broad-except
        return ""
    if not same:
        return ""
    made_by = f", which ships the build made by {build.provider[0]}" if build.provider else ""
    return (f"This is the ffmpeg.exe from the imageio-ffmpeg {wheel} Python package{made_by}. MirrorBridge only "
            "starts it as a separate program.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--build", type=Path, default=BUILD, help="PyInstaller's work folder for the exe")
    parser.add_argument("--exe", type=Path, default=EXE)
    parser.add_argument("--out", type=Path, help="where to write THIRD-PARTY-NOTICES.txt and licenses/")
    parser.add_argument("--ffmpeg-version", action="store_true",
                        help="only print the FFmpeg release inside the exe (fails for a git snapshot)")
    args = parser.parse_args(argv)
    entries = read_build(args.build)
    ffmpeg = ffmpeg_in_build(entries)
    build = parse_ffmpeg_version(run_ffmpeg_version(ffmpeg))
    if args.ffmpeg_version:
        if not build.release:
            raise SystemExit(f"The FFmpeg inside the exe is not a release, so it has no source tarball: {build.banner}")
        print(build.release)
        return 0
    if args.out is None:
        parser.error("--out is required")
    check_exe(args.build, args.exe)
    notices = collect(entries, Environment(build=args.build), build, ffmpeg_origin(ffmpeg, build))
    path = write(notices, args.out, code_version())
    print(f"{path}: FFmpeg {build.release or build.banner} ({notices.ffmpeg.licence}), "
          f"{len(notices.packages)} Python packages, CPython {notices.cpython.version}, "
          f"{len(notices.natives)} other libraries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
