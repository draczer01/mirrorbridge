"""The release package: version check, third-party notices, the zip and SHA256SUMS, the workflows."""

import base64
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import types
import zipfile
from pathlib import Path

import pytest

import mirrorbridge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))

import check_version  # noqa: E402
import fetch_ffmpeg  # noqa: E402
import package_release  # noqa: E402
import third_party_notices as tpn  # noqa: E402

VERSION = mirrorbridge.__version__
GYAN = """ffmpeg version 7.1-essentials_build-www.gyan.dev Copyright (c) 2000-2024 the FFmpeg developers
built with gcc 14.2.0 (Rev1, Built by MSYS2 project)
configuration: --enable-gpl --enable-version3 --enable-static --disable-w32threads --disable-autodetect \
--enable-fontconfig --enable-iconv --enable-gnutls --enable-libxml2 --enable-gmp --enable-bzlib --enable-lzma \
--enable-zlib --enable-libx264 --enable-libx265 --enable-mediafoundation --enable-amf --enable-nvenc
libavutil      59. 39.100 / 59. 39.100
"""
# What the bundled FFmpeg 8.1.1 says (gyan.dev's essentials build).
GYAN_811 = """ffmpeg version 8.1.1-essentials_build-www.gyan.dev Copyright (c) 2000-2026 the FFmpeg developers
built with gcc 15.2.0 (Rev13, Built by MSYS2 project)
configuration: --enable-gpl --enable-version3 --enable-static --disable-w32threads --disable-autodetect \
--enable-cairo --enable-fontconfig --enable-iconv --enable-gnutls --enable-libxml2 --enable-gmp \
--enable-bzlib --enable-lzma --enable-zlib --enable-libsrt --enable-libssh --enable-libzmq \
--enable-avisynth --enable-sdl2 --enable-libwebp --enable-libx264 --enable-libx265 --enable-libxvid \
--enable-libaom --enable-libopenjpeg --enable-libvpx --enable-mediafoundation --enable-libass \
--enable-libfreetype --enable-libfribidi --enable-libharfbuzz --enable-libvidstab --enable-libvmaf \
--enable-libzimg --enable-amf --enable-cuda-llvm --enable-cuvid --enable-dxva2 --enable-d3d11va \
--enable-d3d12va --enable-ffnvcodec --enable-libvpl --enable-nvdec --enable-nvenc --enable-vaapi \
--enable-openal --enable-libgme --enable-libopenmpt --enable-libopencore-amrwb --enable-libmp3lame \
--enable-libtheora --enable-libvo-amrwbenc --enable-libgsm --enable-libopencore-amrnb \
--enable-libopus --enable-libspeex --enable-libvorbis --enable-librubberband
"""


# --- check_version.py ---------------------------------------------------------------------------

def test_the_code_has_one_version_and_its_tag_passes():
    assert check_version.code_version() == VERSION
    assert check_version.check(f"v{VERSION}", notes=True) == VERSION
    assert check_version.check(f"refs/tags/v{VERSION}") == VERSION


@pytest.mark.parametrize("tag", ["v9.9.9", VERSION, f"v{VERSION}-rc1", f"V{VERSION}"])
def test_a_tag_for_another_version_fails(tag):
    with pytest.raises(SystemExit, match="does not match"):
        check_version.check(tag)


def _copy_version_files(root: Path) -> Path:
    for name in ("mirrorbridge/__init__.py", "pyproject.toml", "packaging/version_info.txt"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, root / name)
    return root


def test_a_file_with_another_version_fails(tmp_path):
    root = _copy_version_files(tmp_path)
    pyproject = root / "pyproject.toml"
    pyproject.write_text(pyproject.read_text().replace(f'version = "{VERSION}"', 'version = "9.9.9"'))
    with pytest.raises(SystemExit, match="pyproject.toml says 9.9.9"):
        check_version.check(f"v{VERSION}", root=root)


def test_the_exe_version_resource_counts_too(tmp_path):
    root = _copy_version_files(tmp_path)
    info = root / "packaging" / "version_info.txt"
    numbers = ", ".join(VERSION.split(".")) + ", 0"
    info.write_text(info.read_text().replace(f"prodvers=({numbers})", "prodvers=(9, 9, 9, 0)"))
    with pytest.raises(SystemExit, match="prodvers"):
        check_version.code_version(root)


def test_a_release_needs_its_notes(tmp_path):
    root = _copy_version_files(tmp_path)
    assert check_version.check(f"v{VERSION}", root=root) == VERSION
    with pytest.raises(SystemExit, match=f"docs/release-notes/v{VERSION}.md is missing"):
        check_version.check(f"v{VERSION}", notes=True, root=root)


def test_the_command_prints_the_version(capsys):
    assert check_version.main([f"v{VERSION}", "--notes"]) == 0
    assert capsys.readouterr().out == f"{VERSION}\n"


# --- FFmpeg's version line --------------------------------------------------------------------

def test_the_bundled_ffmpeg_is_release_71_under_the_gpl_3():
    build = tpn.parse_ffmpeg_version(GYAN)
    assert build.release == "7.1"
    assert build.licence == "GPL-3.0-or-later"
    assert build.provider == ("Gyan Doshi (www.gyan.dev)", "https://www.gyan.dev/ffmpeg/builds/")
    assert build.built_with == "built with gcc 14.2.0 (Rev1, Built by MSYS2 project)"
    assert {"libx264", "libx265", "libxml2", "gnutls", "zlib"} <= set(build.libraries)
    assert "mediafoundation" not in build.libraries  # part of Windows, not a library it links


def test_ffmpeg_811_also_links_cairo_and_openal():
    build = tpn.parse_ffmpeg_version(GYAN_811)
    assert build.release == "8.1.1" and build.licence == "GPL-3.0-or-later"
    assert build.provider == ("Gyan Doshi (www.gyan.dev)", "https://www.gyan.dev/ffmpeg/builds/")
    assert {"cairo", "openal", "libx264", "libx265", "libvpl", "sdl2", "gnutls", "zlib"} <= set(build.libraries)
    # Parts of Windows and the GPU drivers, and headers: not libraries it links.
    assert not {"mediafoundation", "amf", "nvenc", "ffnvcodec", "cuda-llvm", "d3d11va", "vaapi"} & set(build.libraries)


@pytest.mark.parametrize("version, release", [
    ("7.1", "7.1"), ("n7.1", "7.1"), ("7.1.1-full_build-www.gyan.dev", "7.1.1"), ("6.1.2", "6.1.2"),
    ("n7.1-153-gaeb8631048-20250101", None),  # 153 commits after 7.1: no tarball has that source
    ("N-118000-g1234567890-20250101", None), ("2024-10-10-git-1234abcd-full_build-www.gyan.dev", None),
    ("7.1.git", None),
])
def test_only_a_release_has_a_source_tarball(version, release):
    assert tpn.parse_ffmpeg_version(f"ffmpeg version {version} Copyright\nconfiguration: --enable-gpl\n").release == release


@pytest.mark.parametrize("options, licence", [
    ("--enable-gpl --enable-version3", "GPL-3.0-or-later"), ("--enable-gpl", "GPL-2.0-or-later"),
    ("--enable-version3", "LGPL-3.0-or-later"), ("--disable-autodetect", "LGPL-2.1-or-later"),
])
def test_the_licence_follows_the_configuration(options, licence):
    assert tpn.parse_ffmpeg_version(f"ffmpeg version 7.1\nconfiguration: {options}\n").licence == licence


def test_a_nonfree_ffmpeg_stops_the_release():
    with pytest.raises(SystemExit, match="nonfree"):
        _ = tpn.parse_ffmpeg_version("ffmpeg version 7.1\nconfiguration: --enable-gpl --enable-nonfree\n").licence


def test_something_else_is_not_taken_for_ffmpeg():
    with pytest.raises(SystemExit, match="Not FFmpeg"):
        tpn.parse_ffmpeg_version("usage: something else\n")


# --- third_party_notices.py on a made-up build environment --------------------------------------

def _record_hash(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def _dist(site: Path, name: str, version: str, files: dict, metadata: list[str], licences: dict) -> None:
    """A dist-info in site whose RECORD lists files (path in site -> bytes, written unless None)."""
    info = site / f"{name}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("\n".join(["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}",
                                              *metadata]) + "\n")
    for rel, text in licences.items():
        (info / rel).parent.mkdir(parents=True, exist_ok=True)
        (info / rel).write_text(text)
    rows = []
    for rel, data in files.items():
        if isinstance(data, tuple):  # (bytes on disk are someone else's, bytes this RECORD expected)
            data = data[1]
        else:
            (site / rel).parent.mkdir(parents=True, exist_ok=True)
            (site / rel).write_bytes(data)
        rows.append(f"{rel},sha256={_record_hash(data)},{len(data)}")
    rows += [f"{p.relative_to(site).as_posix()},," for p in sorted(info.rglob("*")) if p.is_file()]
    (info / "RECORD").write_text("\n".join(rows + [f"{info.name}/RECORD,,"]) + "\n")


def _toc(path: Path, entries: list[tuple]) -> None:
    """A .toc the way PyInstaller writes them: a Python literal around the list of contents."""
    if path.name.startswith("PKG"):
        data = (str(path.with_suffix(".pkg")), {"BINARY": True}, entries, ("python312.dll", False, False, False, [], None))
    else:
        data = (str(path.with_suffix(".pyz")), entries)
    path.write_text(repr(data), encoding="utf-8")


class FakeBuild:
    """What a PyInstaller build of MirrorBridge looks like, with a few packages of each kind."""

    def __init__(self, tmp: Path):
        self.prefix = tmp / "python"
        self.site = self.prefix / "Lib" / "site-packages"
        self.app = tmp / "app"
        self.build = self.app / "build" / "pyinstaller" / "MirrorBridge"
        files = {
            self.prefix / "LICENSE.txt": b"PSF LICENSE AGREEMENT FOR PYTHON\nThis copy of Python includes a copy of "
                                         b"openssl.\nThis program uses Microsoft Distributable Code.\n",
            self.prefix / "Lib" / "os.py": b"# os\n", self.prefix / "python312.dll": b"MZ python",
            self.prefix / "vcruntime140.dll": b"MZ vc", self.prefix / "DLLs" / "_ssl.pyd": b"MZ ssl",
            self.prefix / "DLLs" / "libcrypto-3.dll": b"MZ crypto",
            self.app / "mirrorbridge" / "__init__.py": b"", self.app / "packaging" / "mirrorbridge_entry.py": b"",
            self.build / "localpycs" / "pyimod01_archive.pyc": b"pyc", self.build / "localpycs" / "struct.pyc": b"pyc",
            self.build / "base_library.zip": b"PK", self.build / "PYZ-00.pyz": b"PYZ\0 modules",
            self.app / "build" / "ffmpeg_stage" / "ffmpeg.exe": b"MZ ffmpeg",
        }
        for path, data in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        mit = ["License-Expression: MIT", "Project-URL: Homepage, https://example.org/goodlib"]
        _dist(self.site, "goodlib", "1.2.0", {"goodlib/__init__.py": b"new = 1\n", "goodlib/_speed.pyd": b"MZ speed"},
              mit, {"licenses/LICENSE": "MIT License, goodlib"})
        # An upgrade left the old dist-info behind; it still lists the files, with their old hashes.
        _dist(self.site, "goodlib", "1.0.0", {"goodlib/__init__.py": (None, b"old = 1\n")}, mit,
              {"licenses/LICENSE": "MIT License, old goodlib"})
        _dist(self.site, "Qtish", "6.9.3", {"Qtish/QtCore.dll": b"MZ qt", "Qtish/MSVCP140.dll": b"MZ msvcp"},
              ["License: LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only"],
              {"licenses/LicenseRef-Qt-Commercial.txt": "Qt commercial"})
        _dist(self.site, "pyinstaller", "6.22.3",
              {"PyInstaller/loader/pyiboot01_bootstrap.py": b"", "PyInstaller/hooks/rthooks/pyi_rth_inspect.py": b""},
              ["License: GPLv2-or-later with a special exception which allows to use PyInstaller to build and "
               "distribute non-free programs (including commercial ones)"],
              {"licenses/COPYING.txt": "The PyInstaller licensing terms"})
        _dist(self.site, "pip", "26.2.2", {"pip/__init__.py": b""}, mit, {"licenses/LICENSE.txt": "pip"})
        _dist(self.site, "imageio_ffmpeg", "0.6.0", {"imageio_ffmpeg/__init__.py": b""},
              ["License: BSD-2-Clause"], {"LICENSE": "imageio-ffmpeg"})
        self.pkg = [
            ("pyi-contents-directory _internal", "", "OPTION"),
            ("PYZ-00.pyz", str(self.build / "PYZ-00.pyz"), "PYZ"),
            ("struct", str(self.build / "localpycs" / "struct.pyc"), "PYMODULE"),
            ("pyimod01_archive", str(self.build / "localpycs" / "pyimod01_archive.pyc"), "PYMODULE"),
            ("pyiboot01_bootstrap", str(self.site / "PyInstaller/loader/pyiboot01_bootstrap.py"), "PYSOURCE"),
            ("pyi_rth_inspect", str(self.site / "PyInstaller/hooks/rthooks/pyi_rth_inspect.py"), "PYSOURCE"),
            ("mirrorbridge_entry", str(self.app / "packaging" / "mirrorbridge_entry.py"), "PYSOURCE"),
            ("ffmpeg\\ffmpeg.exe", str(self.app / "build" / "ffmpeg_stage" / "ffmpeg.exe"), "BINARY"),
            ("python312.dll", str(self.prefix / "python312.dll"), "BINARY"),
            ("VCRUNTIME140.dll", str(self.prefix / "vcruntime140.dll"), "BINARY"),
            ("libcrypto-3.dll", str(self.prefix / "DLLs" / "libcrypto-3.dll"), "BINARY"),
            ("_ssl.pyd", str(self.prefix / "DLLs" / "_ssl.pyd"), "EXTENSION"),
            ("goodlib\\_speed.pyd", str(self.site / "goodlib" / "_speed.pyd"), "EXTENSION"),
            ("Qtish\\QtCore.dll", str(self.site / "Qtish" / "QtCore.dll"), "BINARY"),
            ("Qtish\\MSVCP140.dll", str(self.site / "Qtish" / "MSVCP140.dll"), "BINARY"),
            ("base_library.zip", str(self.build / "base_library.zip"), "DATA"),
        ]
        self.pyz = [("os", str(self.prefix / "Lib" / "os.py"), "PYMODULE"),
                    ("goodlib", str(self.site / "goodlib" / "__init__.py"), "PYMODULE"),
                    ("mirrorbridge", str(self.app / "mirrorbridge" / "__init__.py"), "PYMODULE"),
                    ("nspkg", "-", "PYMODULE")]
        self.write_tocs()

    def write_tocs(self):
        _toc(self.build / "PKG-00.toc", self.pkg)
        _toc(self.build / "PYZ-00.toc", self.pyz)

    def environment(self) -> tpn.Environment:
        return tpn.Environment(paths=[str(self.site)], base_prefix=str(self.prefix),
                               app_dirs=[self.app / "mirrorbridge", self.app / "packaging"], build=self.build,
                               versions={"CPython": "3.12.10", "OpenSSL": "3.0.16"})

    def notices(self) -> tpn.Notices:
        return tpn.collect(tpn.read_build(self.build), self.environment(), tpn.parse_ffmpeg_version(GYAN))


def _section(text: str, heading: str, flat: bool = True) -> str:
    """One numbered section of THIRD-PARTY-NOTICES.txt, with its lines joined unless flat is False."""
    match = re.search(rf"^\d+\. {re.escape(heading)}.*?(?=^\d+\. |\Z)", text, re.M | re.S)
    assert match, heading
    return " ".join(match.group(0).split()) if flat else match.group(0)


@pytest.fixture
def fake_build(tmp_path):
    return FakeBuild(tmp_path)


def test_the_notices_name_every_kind_of_thing_inside_the_exe(fake_build, tmp_path):
    out = tmp_path / "notices"
    text = tpn.write(fake_build.notices(), out, VERSION).read_text(encoding="utf-8")

    ffmpeg = _section(text, "FFmpeg 7.1")
    assert "Licence: GPL-3.0-or-later, because it was configured with --enable-gpl and --enable-version3" in ffmpeg
    assert "ffmpeg-7.1.tar.xz" in ffmpeg and "https://ffmpeg.org/releases/ffmpeg-7.1.tar.xz" in ffmpeg
    assert "https://www.gyan.dev/ffmpeg/builds/" in ffmpeg and "libx264" in ffmpeg
    assert "licenses/FFmpeg-7.1/GPL-3.0.txt" in ffmpeg

    fairplay = _section(text, "FairPlay SAP, ported from doubletake (LGPL-3.0)")
    assert "https://github.com/omarroth/doubletake" in fairplay and "licenses/doubletake-fairplay/" in fairplay
    assert "mirrorbridge/fairplay" in fairplay

    python = _section(text, "Python (CPython)")
    assert "CPython 3.12.10 Licence: PSF-2.0" in python
    assert "libmpdec in _decimal" in python and "https://docs.python.org/3.12/license.html" in python
    assert "OpenSSL 3.0.16" in python and "in licenses/CPython-3.12.10/LICENSE.txt, with CPython's" in python

    microsoft = _section(text, "Microsoft C and C++ runtime")
    assert "Microsoft Visual C++ Redistributable" in microsoft
    assert "Files: Qtish/MSVCP140.dll, VCRUNTIME140.dll" in microsoft

    packages = _section(text, "Python packages (2)", flat=False)
    assert re.search(r"^goodlib 1\.2\.0\n  Licence:  MIT\n  Website:  https://example\.org/goodlib\n", packages, re.M)
    assert "1.0.0" not in text  # the dist-info an upgrade left behind
    assert "Qtish 6.9.3" in packages and "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only" in packages
    for build_tool in ("pip", "imageio", "pyinstaller", "PyInstaller"):
        assert build_tool not in packages
    assert not re.search(r"\bpip\b", text) and "imageio_ffmpeg" not in text

    assert "pyinstaller 6.22.3" in _section(text, "PyInstaller")
    assert "Qtish 6.9.3 (LGPL-3.0-only" in _section(text, "Qt for Python and the other LGPL libraries")
    assert "not inside MirrorBridge.exe" in _section(text, "Not included: Parsec Virtual Display Driver")


def test_the_licence_texts_are_copied_next_to_the_notices(fake_build, tmp_path):
    out = tmp_path / "notices"
    tpn.write(fake_build.notices(), out, VERSION)
    licenses = out / "licenses"
    assert (licenses / "goodlib-1.2.0" / "LICENSE").read_text() == "MIT License, goodlib"
    assert (licenses / "CPython-3.12.10" / "LICENSE.txt").read_bytes() == (fake_build.prefix / "LICENSE.txt").read_bytes()
    assert (licenses / "pyinstaller-6.22.3" / "COPYING.txt").is_file()
    # Qt's packages ship only their commercial licence; the LGPL v3 needs the GPL v3 text with it.
    gnu = Path(tpn.__file__).parent / "licenses"
    assert (licenses / "Qtish-6.9.3" / "LicenseRef-Qt-Commercial.txt").is_file()
    assert (licenses / "Qtish-6.9.3" / "LGPL-3.0.txt").read_bytes() == (gnu / "LGPL-3.0.txt").read_bytes()
    assert (licenses / "Qtish-6.9.3" / "GPL-3.0.txt").read_bytes() == (gnu / "GPL-3.0.txt").read_bytes()
    assert (licenses / "FFmpeg-7.1" / "GPL-3.0.txt").read_bytes() == (gnu / "GPL-3.0.txt").read_bytes()
    # The FairPlay port is LGPL-3.0 (doubletake's licence and the GPL it builds on).
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in (licenses / "doubletake-fairplay" / "LICENSE").read_text()
    assert "GNU GENERAL PUBLIC LICENSE" in (licenses / "doubletake-fairplay" / "COPYING.GPL").read_text()
    assert sorted(p.name for p in licenses.iterdir()) == [
        "CPython-3.12.10", "FFmpeg-7.1", "Qtish-6.9.3", "doubletake-fairplay", "goodlib-1.2.0", "pyinstaller-6.22.3"]


def test_the_vendored_gnu_texts_are_the_fsf_ones():
    gnu = Path(tpn.__file__).parent / "licenses"
    digests = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in gnu.iterdir()}
    assert digests == {"GPL-3.0.txt": "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986",
                       "LGPL-3.0.txt": "e3a994d82e644b03a792a930f574002658412f62407f5fee083f2555c5f23118"}


def test_a_file_nobody_owns_stops_the_notices(fake_build, tmp_path):
    stray = tmp_path / "elsewhere" / "mystery.dll"
    stray.parent.mkdir()
    stray.write_bytes(b"MZ")
    fake_build.pkg.append(("mystery.dll", str(stray), "BINARY"))
    fake_build.write_tocs()
    with pytest.raises(SystemExit, match="mystery.dll"):
        fake_build.notices()


def test_an_ffmpeg_whose_licence_text_is_missing_stops_the_notices(fake_build):
    lgpl = tpn.parse_ffmpeg_version("ffmpeg version 7.1\nconfiguration: --disable-autodetect\n")
    with pytest.raises(SystemExit, match="LGPL-2.1"):
        tpn.collect(tpn.read_build(fake_build.build), fake_build.environment(), lgpl)


def test_the_notices_say_where_the_pinned_ffmpeg_comes_from(fake_build, tmp_path):
    ffmpeg = fake_build.app / "build" / "ffmpeg_stage" / "ffmpeg.exe"
    pin = {**fetch_ffmpeg.load_pin(), "exe_sha256": hashlib.sha256(ffmpeg.read_bytes()).hexdigest()}
    build = tpn.parse_ffmpeg_version(GYAN_811)
    origin = tpn.ffmpeg_origin(str(ffmpeg), build, pin)
    notices = tpn.collect(tpn.read_build(fake_build.build), fake_build.environment(), build, origin)
    text = tpn.write(notices, tmp_path / "notices", VERSION).read_text(encoding="utf-8")

    ffmpeg_section = _section(text, "FFmpeg 8.1.1")
    assert ("This is the ffmpeg.exe from https://github.com/GyanD/codexffmpeg/releases/download/8.1.1/"
            "ffmpeg-8.1.1-essentials_build.zip (SHA-256 6f58ce889f59c311410f7d2b18895b33c03456463486f3b1ebc93d97a0f54541), "
            "the essentials build of FFmpeg 8.1.1 made by Gyan Doshi (www.gyan.dev) from FFmpeg's git commit "
            "239f2c733de417201d7ad3b3b8b0d9b63285b2b1 (tag n8.1.1).") in ffmpeg_section
    assert "https://ffmpeg.org/releases/ffmpeg-8.1.1.tar.xz" in ffmpeg_section
    assert "cairo" in ffmpeg_section and "openal" in ffmpeg_section
    assert "licenses/FFmpeg-8.1.1/GPL-3.0.txt" in ffmpeg_section
    # Any other ffmpeg.exe (this one is not the pinned one, nor imageio-ffmpeg's) gets no such note.
    assert tpn.ffmpeg_origin(str(ffmpeg), build) == ""


def test_the_ffmpeg_version_command_names_the_bundled_ffmpeg(fake_build, monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(tpn, "run_ffmpeg_version", lambda path: ran.append(path) or GYAN)
    assert tpn.main(["--build", str(fake_build.build), "--ffmpeg-version"]) == 0
    assert capsys.readouterr().out == "7.1\n"
    assert ran == [str(fake_build.app / "build" / "ffmpeg_stage" / "ffmpeg.exe")]

    monkeypatch.setattr(tpn, "run_ffmpeg_version", lambda path: "ffmpeg version N-118000-g1234567890\n")
    with pytest.raises(SystemExit, match="not a release"):
        tpn.main(["--build", str(fake_build.build), "--ffmpeg-version"])


def _fake_reader(monkeypatch, toc: dict):
    """PyInstaller's archive reader, answering with toc (name -> unpacked size), which packs the
    Python modules as PYZ.pyz like the real one."""
    readers = types.ModuleType("PyInstaller.archive.readers")
    entries = {("PYZ.pyz" if n == "PYZ-00.pyz" else n): (0, 0, size, 1, "z" if n == "PYZ-00.pyz" else "b")
               for n, size in toc.items()}
    readers.CArchiveReader = lambda path: types.SimpleNamespace(toc=entries)
    for name, module in (("PyInstaller", types.ModuleType("PyInstaller")),
                         ("PyInstaller.archive", types.ModuleType("PyInstaller.archive")),
                         ("PyInstaller.archive.readers", readers)):
        monkeypatch.setitem(sys.modules, name, module)


def test_the_notices_are_only_made_for_the_exe_the_build_folder_describes(fake_build, monkeypatch):
    sizes = {name: os.path.getsize(source) if os.path.isfile(source) else 0
             for name, source, kind in fake_build.pkg if kind != "OPTION"}
    _fake_reader(monkeypatch, sizes)
    tpn.check_exe(fake_build.build, Path("MirrorBridge.exe"))

    _fake_reader(monkeypatch, {**sizes, "PYZ-00.pyz": 1})
    with pytest.raises(SystemExit, match="the Python modules"):
        tpn.check_exe(fake_build.build, Path("MirrorBridge.exe"))

    _fake_reader(monkeypatch, {**sizes, "python312.dll": 12345})
    with pytest.raises(SystemExit, match="python312.dll"):
        tpn.check_exe(fake_build.build, Path("MirrorBridge.exe"))
    _fake_reader(monkeypatch, {name: size for name, size in sizes.items() if name != "base_library.zip"})
    with pytest.raises(SystemExit, match="base_library.zip"):
        tpn.check_exe(fake_build.build, Path("MirrorBridge.exe"))


# --- fetch_ffmpeg.py ------------------------------------------------------------------------------

def test_the_pin_is_gyans_ffmpeg_811_essentials():
    pin = fetch_ffmpeg.load_pin()
    assert pin["version"] == "8.1.1" and (pin["provider"], pin["build"]) == ("gyan.dev", "essentials")
    assert pin["archive_url"] == ("https://github.com/GyanD/codexffmpeg/releases/download/8.1.1/"
                                  "ffmpeg-8.1.1-essentials_build.zip")
    assert pin["archive_size"] == 109_282_242
    assert pin["archive_sha256"] == "6f58ce889f59c311410f7d2b18895b33c03456463486f3b1ebc93d97a0f54541"
    assert pin["exe_member"] == "ffmpeg-8.1.1-essentials_build/bin/ffmpeg.exe"
    assert pin["exe_size"] == 101_457_920
    assert pin["exe_sha256"] == "228d7a8556258de907fdb55f36850078ebc7680b84ec30d84ea02e99bec1d1eb"
    assert pin["source_commit"] == "239f2c733de417201d7ad3b3b8b0d9b63285b2b1"  # what the tag n8.1.1 points at
    assert pin["source_url"] == "https://ffmpeg.org/releases/ffmpeg-8.1.1.tar.xz"
    assert package_release.FFMPEG_SOURCE.fullmatch(pin["source_url"].rsplit("/", 1)[1])
    assert pin["source_sha256"] == "" or re.fullmatch(r"[0-9a-f]{64}", pin["source_sha256"])
    assert fetch_ffmpeg.exe_path(pin) == ROOT / "build" / "ffmpeg_811" / "ffmpeg.exe"


@pytest.mark.parametrize("field, value, error", [
    ("archive_sha256", "6F58CE88", "archive_sha256"), ("exe_size", "101457920", "exe_size"),
    ("exe_member", "../../ffmpeg.exe", "exe_member"), ("archive_url", "http://example.org/f.zip", "archive_url"),
    ("source_url", "https://ffmpeg.org/releases/ffmpeg-latest.tar.xz", "source_url"),
    ("source_key_fingerprint", "D67658D8", "source_key_fingerprint"), ("version", None, "version"),
])
def test_a_pin_that_does_not_look_right_stops_everything(tmp_path, field, value, error):
    pin = fetch_ffmpeg.load_pin()
    pin[field] = value
    path = tmp_path / "pin.json"
    path.write_text(json.dumps(pin), encoding="utf-8")
    with pytest.raises(SystemExit, match=error):
        fetch_ffmpeg.load_pin(path)


def _first_packet(data: bytes) -> tuple[int, bytes]:
    """The tag and body of the first OpenPGP packet in data (RFC 4880, 4.2)."""
    if data[0] & 0x40:  # new format
        tag, first = data[0] & 0x3F, data[1]
        if first < 192:
            length, start = first, 2
        elif first < 224:
            length, start = ((first - 192) << 8) + data[2] + 192, 3
        else:
            length, start = int.from_bytes(data[2:6], "big"), 6
    else:
        tag, size = (data[0] >> 2) & 0x0F, (1, 2, 4)[data[0] & 0x03]
        length, start = int.from_bytes(data[1:1 + size], "big"), 1 + size
    return tag, data[start:start + length]


def test_the_release_key_is_the_one_ffmpeg_lists():
    tag, body = _first_packet(fetch_ffmpeg.dearmor(fetch_ffmpeg.KEY.read_text(encoding="ascii")))
    assert tag == 6 and body[0] == 4  # a version 4 public key
    fingerprint = hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body).hexdigest().upper()
    # As https://ffmpeg.org/download.html#release-verification lists it.
    assert fingerprint == fetch_ffmpeg.load_pin()["source_key_fingerprint"] == "FCF986EA15E6E293A5644F10B4322F04D67658D8"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def fake_zip(tmp_path):
    """A small zip laid out like gyan.dev's, a pin (as a dict) that names it, and a repo folder."""
    folder = tmp_path / "downloads"
    folder.mkdir()
    archive = folder / "ffmpeg-9.9-test_build.zip"
    exe = b"MZ" + os.urandom(50_000)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("ffmpeg-9.9-test_build/README.txt", "Version: 9.9-test_build")
        z.writestr("ffmpeg-9.9-test_build/bin/ffmpeg.exe", exe)
        z.writestr("ffmpeg-9.9-test_build/bin/ffplay.exe", b"MZ ffplay")
    data = archive.read_bytes()
    pin = {**fetch_ffmpeg.load_pin(), "version": "9.9", "archive_url": archive.as_uri(), "archive_size": len(data),
           "archive_sha256": _sha256(data), "exe_member": "ffmpeg-9.9-test_build/bin/ffmpeg.exe",
           "exe_size": len(exe), "exe_sha256": _sha256(exe), "source_url": "https://ffmpeg.org/releases/ffmpeg-9.9.tar.xz"}
    root = tmp_path / "repo"
    root.mkdir()
    return types.SimpleNamespace(archive=archive, exe=exe, pin=pin, root=root,
                                 target=root / "build" / "ffmpeg_99" / "ffmpeg.exe")


def _files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if p.is_file())


def test_a_zip_given_with_zip_gives_only_its_ffmpeg_exe(fake_zip, tmp_path, capsys):
    pin_file = tmp_path / "pin.json"  # a pin file holds an https URL; --zip does not use it
    pin_file.write_text(json.dumps({**fake_zip.pin, "archive_url": "https://example.invalid/ffmpeg-9.9-test_build.zip"}))
    assert fetch_ffmpeg.main(["--zip", str(fake_zip.archive), "--pin", str(pin_file), "--root", str(fake_zip.root)]) == 0
    # The path, and nothing else, on standard output (main resolves --root, as Windows spells it out).
    assert capsys.readouterr().out.splitlines() == [str(fake_zip.target.resolve())]
    assert fake_zip.target.read_bytes() == fake_zip.exe
    assert _files(fake_zip.root) == [fake_zip.target]  # neither ffplay.exe, nor the README, nor a .part


def test_the_zip_is_downloaded_once_and_then_the_exe_is_reused(fake_zip):
    assert fetch_ffmpeg.fetch(fake_zip.pin, root=fake_zip.root) == fake_zip.target
    cached = fake_zip.root / "build" / "ffmpeg-download" / "ffmpeg-9.9-test_build.zip"
    assert cached.read_bytes() == fake_zip.archive.read_bytes() and fake_zip.target.read_bytes() == fake_zip.exe
    fake_zip.archive.unlink()  # a download now would fail
    fake_zip.target.unlink()
    assert fetch_ffmpeg.fetch(fake_zip.pin, root=fake_zip.root) == fake_zip.target  # out of the downloaded zip
    cached.unlink()
    fake_zip.target.write_bytes(fake_zip.exe)
    assert fetch_ffmpeg.fetch(fake_zip.pin, root=fake_zip.root) == fake_zip.target  # already in place
    assert _files(fake_zip.root) == [fake_zip.target]


def test_an_ffmpeg_exe_that_is_not_the_pinned_one_is_replaced(fake_zip):
    fake_zip.target.parent.mkdir(parents=True)
    fake_zip.target.write_bytes(b"MZ an older ffmpeg")
    fetch_ffmpeg.fetch(fake_zip.pin, fake_zip.archive, fake_zip.root)
    assert fake_zip.target.read_bytes() == fake_zip.exe


@pytest.mark.parametrize("field, change, error", [
    ("archive_sha256", lambda v: "0" * 64, "has SHA-256 [0-9a-f]{64}, not 0{64}: it is not the pinned FFmpeg 9.9 zip"),
    ("archive_size", lambda v: v + 1, "bytes, not"),
    ("exe_sha256", lambda v: "0" * 64, "has SHA-256 [0-9a-f]{64}, not 0{64}"),
    ("exe_size", lambda v: v - 1, "bytes, not"),
    ("exe_member", lambda v: "ffmpeg-9.9-test_build/ffmpeg.exe", "has no ffmpeg-9.9-test_build/ffmpeg.exe"),
])
@pytest.mark.parametrize("download", [False, True])
def test_a_zip_or_exe_that_does_not_match_the_pin_is_refused(fake_zip, field, change, error, download):
    pin = {**fake_zip.pin, field: change(fake_zip.pin[field])}
    with pytest.raises(SystemExit, match=error):
        fetch_ffmpeg.fetch(pin, None if download else fake_zip.archive, fake_zip.root)
    assert not fake_zip.target.exists() and not fake_zip.target.with_name("ffmpeg.exe.part").exists()


def test_a_file_that_is_not_a_zip_is_refused(fake_zip, tmp_path):
    other = tmp_path / "ffmpeg-9.9-test_build.7z"
    other.write_bytes(b"7z\xbc\xaf\x27\x1c" + os.urandom(1000))
    pin = {**fake_zip.pin, "archive_size": other.stat().st_size, "archive_sha256": _sha256(other.read_bytes())}
    with pytest.raises(SystemExit, match="not a good zip file"):
        fetch_ffmpeg.fetch(pin, other, fake_zip.root)
    assert not fake_zip.target.exists()


def test_the_spec_bundles_the_pinned_exe_whatever_the_environment_says(fake_zip, tmp_path):
    with pytest.raises(SystemExit, match="run python -I packaging/fetch_ffmpeg.py first"):
        fetch_ffmpeg.bundled({}, fake_zip.pin, fake_zip.root)
    fetch_ffmpeg.fetch(fake_zip.pin, fake_zip.archive, fake_zip.root)
    path, why = fetch_ffmpeg.bundled({}, fake_zip.pin, fake_zip.root)
    assert path == fake_zip.target and why.startswith("the pinned FFmpeg 9.9 (gyan.dev essentials)")
    # A MIRRORBRIDGE_FFMPEG left in the shell from earlier builds does not count without the opt-in.
    other = tmp_path / "ffmpeg71.exe"
    other.write_bytes(b"MZ 7.1")
    environ = {"MIRRORBRIDGE_FFMPEG": str(other), "AIRCAST_FFMPEG": str(other), "MIRRORBRIDGE_OTHER_FFMPEG": "yes"}
    path, why = fetch_ffmpeg.bundled(environ, fake_zip.pin, fake_zip.root)
    assert path == fake_zip.target
    assert "MIRRORBRIDGE_FFMPEG and AIRCAST_FFMPEG left alone without MIRRORBRIDGE_OTHER_FFMPEG=1" in why
    fake_zip.target.write_bytes(b"MZ changed")
    with pytest.raises(SystemExit, match="is not the pinned FFmpeg 9.9"):
        fetch_ffmpeg.bundled({}, fake_zip.pin, fake_zip.root)


def test_with_the_opt_in_the_spec_bundles_another_ffmpeg(fake_zip, tmp_path, monkeypatch):
    other = tmp_path / "ffmpeg71.exe"
    other.write_bytes(b"MZ 7.1")
    opt_in = {"MIRRORBRIDGE_OTHER_FFMPEG": "1"}
    assert fetch_ffmpeg.bundled({**opt_in, "MIRRORBRIDGE_FFMPEG": str(other)}, fake_zip.pin, fake_zip.root) == (
        other, "MIRRORBRIDGE_FFMPEG, as MIRRORBRIDGE_OTHER_FFMPEG=1 allows")
    assert fetch_ffmpeg.bundled({**opt_in, "AIRCAST_FFMPEG": str(other)}, fake_zip.pin, fake_zip.root)[0] == other
    with pytest.raises(SystemExit, match="does not exist"):
        fetch_ffmpeg.bundled({**opt_in, "MIRRORBRIDGE_FFMPEG": str(tmp_path / "nothing.exe")}, fake_zip.pin, fake_zip.root)
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", types.SimpleNamespace(get_ffmpeg_exe=lambda: str(other)))
    path, why = fetch_ffmpeg.bundled(opt_in, fake_zip.pin, fake_zip.root)
    assert path == other and why.startswith("the imageio-ffmpeg package's")


def test_bundled_prints_only_the_path_on_standard_output(fake_zip, tmp_path, monkeypatch, capsys):
    pin_file = tmp_path / "pin.json"
    pin_file.write_text(json.dumps({**fake_zip.pin, "archive_url": "https://example.invalid/ffmpeg-9.9-test_build.zip"}))
    fetch_ffmpeg.fetch(fake_zip.pin, fake_zip.archive, fake_zip.root)
    for name in ("MIRRORBRIDGE_OTHER_FFMPEG", "MIRRORBRIDGE_FFMPEG", "AIRCAST_FFMPEG"):
        monkeypatch.delenv(name, raising=False)
    capsys.readouterr()
    assert fetch_ffmpeg.main(["--bundled", "--pin", str(pin_file), "--root", str(fake_zip.root)]) == 0
    out, err = capsys.readouterr()
    assert out.splitlines() == [str(fake_zip.target.resolve())] and "MirrorBridge.spec will bundle" in err


FFMPEG_KEY = "FCF986EA15E6E293A5644F10B4322F04D67658D8"
GOOD_SIGNATURE = ("[GNUPG:] NEWSIG ffmpeg-devel@ffmpeg.org\n"
                  "[GNUPG:] GOODSIG B4322F04D67658D8 FFmpeg release signing key <ffmpeg-devel@ffmpeg.org>\n"
                  f"[GNUPG:] VALIDSIG {FFMPEG_KEY} 2026-06-17 1781664539 0 4 0 1 10 00 {FFMPEG_KEY}\n")


def _fake_gpg(monkeypatch, stdout: str = GOOD_SIGNATURE, returncode: int = 0) -> list[dict]:
    """gpgv or gpg, answering with stdout; each call's arguments and the keyring it was given."""
    calls = []

    def run(args, **kwargs):
        keyring = Path(args[args.index("--keyring") + 1]).read_bytes()
        calls.append({"args": args, "keyring": keyring})
        return subprocess.CompletedProcess(args, returncode, stdout, "gpgv: Signature made ...\n")

    monkeypatch.setattr(fetch_ffmpeg.subprocess, "run", run)
    return calls


@pytest.fixture
def source_mirror(tmp_path):
    """ffmpeg-9.9.tar.xz and its .asc where a pin can download them from (file: URLs)."""
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    tarball = _ffmpeg_source(mirror / "ffmpeg-9.9.tar.xz", "9.9")
    (mirror / "ffmpeg-9.9.tar.xz.asc").write_bytes(b"-----BEGIN PGP SIGNATURE-----\n...\n")
    pin = {**fetch_ffmpeg.load_pin(), "version": "9.9", "source_url": tarball.as_uri(), "source_sha256": ""}
    return types.SimpleNamespace(tarball=tarball, pin=pin, folder=tmp_path / "build" / "ffmpeg-source")


@pytest.mark.parametrize("gpg", ["/usr/bin/gpgv", "C:/Program Files/Git/usr/bin/gpg.exe"])
def test_the_source_is_downloaded_and_its_signature_checked(source_mirror, monkeypatch, capsys, gpg):
    calls = _fake_gpg(monkeypatch)
    path = fetch_ffmpeg.fetch_source(source_mirror.pin, source_mirror.folder, gpg=gpg)
    assert path == source_mirror.folder / "ffmpeg-9.9.tar.xz"
    assert path.read_bytes() == source_mirror.tarball.read_bytes()
    assert (source_mirror.folder / "ffmpeg-9.9.tar.xz.asc").is_file()
    [call] = calls
    args = call["args"]
    assert args[0] == gpg and args[-2:] == [(path.parent / "ffmpeg-9.9.tar.xz.asc").as_posix(), path.as_posix()]
    assert call["keyring"] == fetch_ffmpeg.dearmor(fetch_ffmpeg.KEY.read_text(encoding="ascii"))  # binary, for gpgv
    if "gpgv" in gpg:
        assert "--verify" not in args
    else:  # gpg looks only at that keyring and trusts it: the fingerprint is the check
        assert {"--batch", "--no-default-keyring", "--verify"} <= set(args) and "always" in args
    digest = _sha256(path.read_bytes())
    assert f"put {digest} in packaging/ffmpeg_pin.json" in capsys.readouterr().err


def test_a_pinned_source_sha256_must_match(source_mirror, monkeypatch):
    _fake_gpg(monkeypatch)
    digest = _sha256(source_mirror.tarball.read_bytes())
    pin = {**source_mirror.pin, "source_sha256": digest}
    assert fetch_ffmpeg.fetch_source(pin, source_mirror.folder, gpg="gpgv").read_bytes() == source_mirror.tarball.read_bytes()
    with pytest.raises(SystemExit, match=f"has SHA-256 {digest}, not the pinned 0{{64}}"):
        fetch_ffmpeg.fetch_source({**pin, "source_sha256": "0" * 64}, source_mirror.folder, gpg="gpgv")


@pytest.mark.parametrize("stdout, returncode", [
    (GOOD_SIGNATURE.replace(f"00 {FFMPEG_KEY}", "00 " + "A" * 40), 0),  # good, but by another key
    ("[GNUPG:] BADSIG B4322F04D67658D8 FFmpeg release signing key\n", 1),
    (GOOD_SIGNATURE, 2),
    (GOOD_SIGNATURE + "[GNUPG:] REVKEYSIG B4322F04D67658D8 FFmpeg release signing key\n", 0),
    ("[GNUPG:] ERRSIG B4322F04D67658D8 1 10 00 1781664539 9 -\n[GNUPG:] NO_PUBKEY B4322F04D67658D8\n", 2),
    ("", 0),
])
def test_a_source_without_a_good_signature_by_ffmpegs_key_is_refused(source_mirror, monkeypatch, stdout, returncode):
    _fake_gpg(monkeypatch, stdout, returncode)
    with pytest.raises(SystemExit, match="is not a good signature of ffmpeg-9.9.tar.xz by FFmpeg's release key"):
        fetch_ffmpeg.fetch_source(source_mirror.pin, source_mirror.folder, gpg="gpgv")


def test_without_gpg_the_source_is_refused(source_mirror, monkeypatch):
    monkeypatch.setattr(fetch_ffmpeg, "find_gpg", lambda: None)
    with pytest.raises(SystemExit, match="Neither gpgv nor gpg"):
        fetch_ffmpeg.fetch_source(source_mirror.pin, source_mirror.folder)


def test_the_armor_comes_off_the_key():
    data = b"\x99\x00\x03\x04ab" * 40
    text = base64.b64encode(data).decode()
    armored = "\n".join(["-----BEGIN PGP PUBLIC KEY BLOCK-----", "Comment: a test", "",
                         *(text[i:i + 64] for i in range(0, len(text), 64)), "=abcd",
                         "-----END PGP PUBLIC KEY BLOCK-----"])
    assert fetch_ffmpeg.dearmor(armored) == data
    assert fetch_ffmpeg.dearmor(armored.replace("Comment: a test\n", "")) == data
    with pytest.raises(SystemExit, match="ASCII-armored"):
        fetch_ffmpeg.dearmor("no key here")


def test_build_exe_checks_the_ffmpeg_it_bundles_and_never_redirects_stderr():
    script = (ROOT / "packaging" / "build_exe.ps1").read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in script.splitlines())
    assert '$ErrorActionPreference = "Stop"' in code
    assert "[string]$FFmpegZip" in code and "[switch]$AllowOtherFFmpeg" in code
    assert '$fetch = @("-I", "packaging\\fetch_ffmpeg.py")' in code and '$fetch += @("--zip", $FFmpegZip)' in code
    assert "python -I packaging\\fetch_ffmpeg.py --bundled" in code
    assert "& $ffmpeg -hide_banner -version" in code and "& $ffmpeg -hide_banner -filters" in code
    assert "[regex]::Escape($pin.version)" in code and "\\bscale_d3d11\\b" in code
    assert code.index("--bundled") < code.index("-filters") < code.index("pyinstaller --noconfirm")
    # With "Stop", Windows PowerShell ends the script at the first line PyInstaller writes to a
    # redirected standard error.
    assert not re.search(r"\d?>&\d|\*>|\b2>", code)


def test_the_spec_bundles_what_fetch_ffmpeg_chose():
    spec = (ROOT / "packaging" / "MirrorBridge.spec").read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in spec.splitlines())
    assert "ffmpeg_src, ffmpeg_why = fetch_ffmpeg.bundled()" in code
    assert "environ" not in code and "imageio" not in code.replace('"imageio_ffmpeg"', "")  # bundled() decides
    wine = (ROOT / "packaging" / "build_exe_wine.sh").read_text(encoding="utf-8")
    assert wine.index("packaging/fetch_ffmpeg.py") < wine.index('wine "$WINPY/python.exe"')


# --- package_release.py ---------------------------------------------------------------------------

def _notices(folder: Path, ffmpeg: str = "ffmpeg-7.1.tar.xz") -> Path:
    (folder / "licenses" / "goodlib-1.2.0").mkdir(parents=True)
    (folder / "licenses" / "goodlib-1.2.0" / "LICENSE").write_text("MIT License, goodlib")
    (folder / "THIRD-PARTY-NOTICES.txt").write_text(f"FFmpeg's source is {ffmpeg}.\n")
    return folder


def _ffmpeg_source(path: Path, release: str = "7.1", stray: bool = False) -> Path:
    top = path.name.removesuffix(".tar.xz")
    with tarfile.open(path, "w:xz") as tar:
        for name, data in ((f"{top}/RELEASE", f"{release}\n".encode()), (f"{top}/README.md", b"FFmpeg"),
                           *((("other/file", b"x"),) if stray else ())):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


@pytest.fixture
def exe(tmp_path):
    path = tmp_path / "dist" / "MirrorBridge.exe"
    path.parent.mkdir()
    path.write_bytes(b"MZ" + os.urandom(4096))
    return path


def _sums(folder: Path) -> dict[str, str]:
    lines = (folder / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert all(re.fullmatch(r"[0-9a-f]{64}  \S+", line) for line in lines), lines
    return {line[66:]: line[:64] for line in lines}


def test_the_package_has_the_exe_the_zip_and_their_checksums(exe, tmp_path):
    out = tmp_path / "release"
    written = package_release.package(exe, _notices(tmp_path / "notices"), out, VERSION)
    zip_name = f"MirrorBridge-{VERSION}-windows-x64.zip"
    assert [p.name for p in written] == ["MirrorBridge.exe", zip_name, "SHA256SUMS"]
    assert sorted(p.name for p in out.iterdir()) == sorted(p.name for p in written)
    assert (out / "MirrorBridge.exe").read_bytes() == exe.read_bytes()

    top = f"MirrorBridge-{VERSION}/"
    with zipfile.ZipFile(out / zip_name) as archive:
        assert archive.namelist() == [
            top, f"{top}LEEME.md", f"{top}LICENSE", f"{top}MirrorBridge.exe", f"{top}README.md",
            f"{top}THIRD-PARTY-NOTICES.txt", f"{top}licenses/", f"{top}licenses/goodlib-1.2.0/",
            f"{top}licenses/goodlib-1.2.0/LICENSE"]
        assert archive.read(f"{top}MirrorBridge.exe") == exe.read_bytes()
        assert archive.read(f"{top}LEEME.md") == (ROOT / "LEEME.md").read_bytes()
        assert archive.testzip() is None

    sums = _sums(out)
    assert list(sums) == sorted(["MirrorBridge.exe", zip_name])
    for name, digest in sums.items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="needs sha256sum")
def test_sha256sum_accepts_the_checksums(exe, tmp_path):
    out = tmp_path / "release"
    package_release.package(exe, _notices(tmp_path / "notices"), out, VERSION,
                            _ffmpeg_source(tmp_path / "ffmpeg-7.1.tar.xz"))
    result = subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=out, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count(": OK") == 3


def test_the_zip_is_the_same_every_time(exe, tmp_path, monkeypatch):
    notices = _notices(tmp_path / "notices")
    package_release.package(exe, notices, tmp_path / "one", VERSION)
    for path in [exe, *notices.rglob("*")]:
        os.utime(path, (time.time() - 86400, time.time() - 86400))  # file dates do not count
    package_release.package(exe, notices, tmp_path / "two", VERSION)
    name = f"MirrorBridge-{VERSION}-windows-x64.zip"
    assert (tmp_path / "one" / name).read_bytes() == (tmp_path / "two" / name).read_bytes()
    assert (tmp_path / "one" / "SHA256SUMS").read_bytes() == (tmp_path / "two" / "SHA256SUMS").read_bytes()

    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1761609600")  # the release commit's date
    package_release.package(exe, notices, tmp_path / "three", VERSION)
    with zipfile.ZipFile(tmp_path / "three" / name) as archive:
        assert {info.date_time for info in archive.infolist()} == {time.gmtime(1761609600)[:6]}


def test_a_release_carries_ffmpegs_source(exe, tmp_path):
    out = tmp_path / "release"
    source = _ffmpeg_source(tmp_path / "ffmpeg-7.1.tar.xz")
    written = package_release.package(exe, _notices(tmp_path / "notices"), out, VERSION, source)
    assert "ffmpeg-7.1.tar.xz" in [p.name for p in written]
    assert (out / "ffmpeg-7.1.tar.xz").read_bytes() == source.read_bytes()
    assert sorted(_sums(out)) == sorted(["MirrorBridge.exe", f"MirrorBridge-{VERSION}-windows-x64.zip", "ffmpeg-7.1.tar.xz"])


@pytest.mark.parametrize("name, release, stray, error", [
    ("ffmpeg-7.1.tar.xz", "7.0", False, "RELEASE file says '7.0'"),
    ("ffmpeg-7.1.tar.xz", "7.1", True, "files outside ffmpeg-7.1/"),
    ("ffmpeg-latest.tar.xz", "7.1", False, "not named like"),
])
def test_only_ffmpegs_own_source_tarball_is_accepted(exe, tmp_path, name, release, stray, error):
    source = _ffmpeg_source(tmp_path / name, release, stray)
    with pytest.raises(SystemExit, match=re.escape(error)):
        package_release.package(exe, _notices(tmp_path / "notices"), tmp_path / "release", VERSION, source)
    assert not (tmp_path / "release").exists()


def test_the_source_must_be_the_one_the_notices_name(exe, tmp_path):
    source = _ffmpeg_source(tmp_path / "ffmpeg-7.1.tar.xz")
    with pytest.raises(SystemExit, match="does not name ffmpeg-7.1.tar.xz"):
        package_release.package(exe, _notices(tmp_path / "notices", "ffmpeg-7.0.tar.xz"), tmp_path / "release",
                                VERSION, source)


def test_the_package_goes_into_an_empty_folder_only(exe, tmp_path):
    out = tmp_path / "release"
    out.mkdir()
    (out / "old.zip").write_bytes(b"")
    with pytest.raises(SystemExit, match="not empty"):
        package_release.package(exe, _notices(tmp_path / "notices"), out, VERSION)


# --- the workflows, the release notes and the changelog -----------------------------------------

def _workflow(name: str) -> dict:
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
    doc["on"] = doc.pop(True, doc.get("on"))  # YAML 1.1 reads the key `on` as true
    return doc


def _runs(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_a_version_tag_publishes_a_prerelease_and_a_manual_run_does_not():
    doc = _workflow("release.yml")
    assert doc["on"]["push"] == {"tags": ["v*"]} and "workflow_dispatch" in doc["on"]
    assert fnmatch.fnmatch(f"v{VERSION}", doc["on"]["push"]["tags"][0])
    assert doc["permissions"] == {"contents": "read"}
    assert doc["concurrency"]["group"] == "release-${{ github.ref }}"
    package, publish = doc["jobs"]["package"], doc["jobs"]["publish"]
    assert package["runs-on"] == "windows-latest" and "permissions" not in package
    assert publish["permissions"] == {"contents": "write"} and publish["needs"] == "package"
    assert publish["if"] == "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
    script = _runs(publish)
    assert '--prerelease' in script and '--title "MirrorBridge ${VERSION}"' in script
    assert 'notes="docs/release-notes/v${VERSION}.md"' in script and '--notes-file "$notes"' in script
    assert "--clobber" in script and "sha256sum -c SHA256SUMS" in script and "ffmpeg-*.tar.xz" in script
    assert {s["env"]["GH_TOKEN"] for s in publish["steps"] if "GH_TOKEN" in s.get("env", {})} == {"${{ github.token }}"}


def test_the_release_build_checks_the_tag_against_the_code_before_building():
    package = _workflow("release.yml")["jobs"]["package"]
    names = [step.get("name") or step.get("run") or step.get("uses") for step in package["steps"]]
    check = next(s for s in package["steps"] if s.get("id") == "version")
    assert 'python packaging/check_version.py "$env:GITHUB_REF_NAME" --notes' in check["run"]
    assert names.index(check["name"]) < names.index(".\\packaging\\build_exe.ps1")
    # What the step runs on the tag: the code's version comes back, or the step fails.
    assert check_version.check(f"v{VERSION}", notes=True) == VERSION
    script = _runs(package)
    assert "$file = python -I packaging/fetch_ffmpeg.py --source build/ffmpeg-source" in script
    assert "python packaging/third_party_notices.py --ffmpeg-version" in script
    assert "if ($ffmpeg -ne $pin.version)" in script and "Invoke-WebRequest" not in script
    assert '--ffmpeg-source "$env:FFMPEG_SOURCE" --out release' in script
    assert '"MirrorBridge $env:VERSION"' in script and "doctor --timeout 2" in script
    assert all(step.get("shell", "pwsh") == "pwsh" for step in package["steps"])
    assert package["defaults"]["run"]["shell"] == "pwsh"


def _uses(doc: dict) -> dict[str, str]:
    return {step["uses"].split("@")[0]: step["uses"].split("@")[1] for job in doc["jobs"].values()
            for step in job["steps"] if "uses" in step}


def test_both_workflows_use_the_same_actions_and_windows_tests():
    ci, release = _workflow("ci.yml"), _workflow("release.yml")
    ci_uses, release_uses = _uses(ci), _uses(release)
    assert {name: release_uses[name] for name in ci_uses if name in release_uses} == ci_uses
    assert release_uses["actions/download-artifact"] == ci_uses["actions/upload-artifact"]

    def windows_tests(job):
        return next(line for line in _runs(job).splitlines() if line.startswith("pytest"))

    assert windows_tests(ci["jobs"]["windows-build"]) == windows_tests(release["jobs"]["package"])
    assert "tests/test_session.py tests/test_devices.py tests/test_release.py" in windows_tests(release["jobs"]["package"])
    ci_script = _runs(ci["jobs"]["windows-build"])
    assert "python packaging/package_release.py --exe dist/MirrorBridge.exe --notices build/notices --out release" in ci_script


def test_both_windows_builds_fetch_the_pinned_ffmpeg_before_building():
    for job in (_workflow("ci.yml")["jobs"]["windows-build"], _workflow("release.yml")["jobs"]["package"]):
        runs = [step.get("run", "") for step in job["steps"]]
        assert runs.index("python -I packaging/fetch_ffmpeg.py") < runs.index(".\\packaging\\build_exe.ps1")
        tests = next(run for run in runs if run.startswith("pytest"))
        assert "tests/test_capture.py" in tests.split()
        assert "$pinned" in _runs(job) or "$pin.version" in _runs(job)  # the exe's FFmpeg is the pinned one


def test_the_release_notes_are_in_english_then_spanish():
    notes = " ".join(check_version.release_notes(VERSION).read_text(encoding="utf-8").split())  # lines joined
    english, spanish = notes.index("## What is new"), notes.index("## Español")
    assert english < spanish < notes.index("### Novedades")
    assert notes.lstrip().startswith("**English**")
    for words in ("pre-release", "Windows protected your PC", "More info", "Run anyway", "Get-FileHash", "SHA256SUMS",
                  "-v cast", "mirrorbridge.log"):
        assert words in notes[:spanish], words
    for words in ("Windows protegió tu PC", "Más información", "Ejecutar de todas formas", "Get-FileHash",
                  "mirrorbridge.log", "la TV"):
        assert words in notes[spanish:], words


def test_the_changelog_starts_with_this_version():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    versions = re.findall(r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}$", changelog, re.M)
    assert versions[0] == VERSION and versions == sorted(versions, key=lambda v: tuple(map(int, v.split("."))),
                                                          reverse=True)
    for version in versions:
        assert re.search(rf"^\[{re.escape(version)}\]: https://github\.com/draczer01/mirrorbridge/", changelog, re.M)
