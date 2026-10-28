"""The release package: version check, third-party notices, the zip and SHA256SUMS, the workflows."""

import base64
import fnmatch
import hashlib
import io
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
    assert sorted(p.name for p in licenses.iterdir()) == [
        "CPython-3.12.10", "FFmpeg-7.1", "Qtish-6.9.3", "goodlib-1.2.0", "pyinstaller-6.22.3"]


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
    assert 'Invoke-WebRequest -Uri "https://ffmpeg.org/releases/ffmpeg-$ffmpeg.tar.xz"' in script
    assert "python packaging/third_party_notices.py --ffmpeg-version" in script
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
