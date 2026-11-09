"""Updates from GitHub Releases (mirrorbridge/update.py), against a local server that plays GitHub."""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from mirrorbridge import update

DL = update.DOWNLOADS
INSTALLER = b"MZ" + os.urandom(300_000)


def _release(tag="v0.5.0", prerelease=False, draft=False, assets=None):
    version = tag[1:]
    if assets is None:
        assets = [
            {"name": f"MirrorBridge-Setup-{version}.exe", "size": len(INSTALLER),
             "browser_download_url": f"{DL}{tag}/MirrorBridge-Setup-{version}.exe"},
            {"name": "SHA256SUMS", "size": 200, "browser_download_url": f"{DL}{tag}/SHA256SUMS"},
        ]
    return {"tag_name": tag, "prerelease": prerelease, "draft": draft, "assets": assets,
            "html_url": f"https://github.com/draczer01/mirrorbridge/releases/tag/{tag}"}


# -- choosing a release ------------------------------------------------------


def test_the_newest_newer_release_is_chosen():
    releases = [_release("v0.4.1"), _release("v0.6.0"), _release("v0.5.0")]
    chosen = update.newest(releases, "0.4.0")
    assert chosen.version == "0.6.0" and chosen.installer_name == "MirrorBridge-Setup-0.6.0.exe"
    assert chosen.installer_size == len(INSTALLER)


@pytest.mark.parametrize("current", ["0.5.0", "0.6.0", "1.0.0"])
def test_the_same_or_an_older_version_is_no_update(current):
    assert update.newest([_release("v0.5.0")], current) is None


def test_versions_compare_as_numbers_not_text():
    assert update.newest([_release("v0.10.0")], "0.9.0").version == "0.10.0"
    assert update.newest([_release("v0.9.0")], "0.10.0") is None


def test_pre_releases_only_when_allowed():
    releases = [_release("v0.6.0", prerelease=True), _release("v0.5.0")]
    assert update.newest(releases, "0.4.0", prereleases=True).version == "0.6.0"
    assert update.newest(releases, "0.4.0", prereleases=False).version == "0.5.0"


def test_drafts_odd_tags_and_releases_without_their_files_are_skipped():
    no_sums = _release("v0.7.0")
    no_sums["assets"] = no_sums["assets"][:1]
    other_repo = _release("v0.8.0")
    other_repo["assets"][0]["browser_download_url"] = "https://evil.example/MirrorBridge-Setup-0.8.0.exe"
    wrong_name = _release("v0.9.0")
    wrong_name["assets"][0]["name"] = "MirrorBridge-Setup-0.8.0.exe"
    releases = [_release("v1.0.0", draft=True), _release("v2.0.0-beta"), {"tag_name": "nightly"}, no_sums,
                other_repo, wrong_name, "junk", _release("v0.5.0")]
    assert update.newest(releases, "0.4.0").version == "0.5.0"


@pytest.mark.parametrize("junk", [None, {}, "x", [None, 3]])
def test_a_strange_answer_is_no_update(junk):
    assert update.newest(junk, "0.4.0") is None


def test_sha256sums_lines_are_read_in_both_forms():
    a, b = "a" * 64, "B" * 64
    assert update.parse_sums(f"{a}  one.exe\n{b} *two.zip\nnot a line\n") == {"one.exe": a, "two.zip": "b" * 64}


# -- a GitHub of our own ----------------------------------------------------


class FakeGitHub(http.server.ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.files: dict[str, bytes] = {}
        self.status: dict[str, list[int]] = {}  # path -> statuses to answer first, one per request
        self.cut_after: dict[str, int] = {}  # path -> bytes sent before the connection drops (once)
        self.ranges = True
        self.requests: list[tuple[str, str]] = []
        self.base = f"http://127.0.0.1:{self.server_address[1]}"


class Handler(http.server.BaseHTTPRequestHandler):
    server: FakeGitHub

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        srv = self.server
        srv.requests.append((self.path, self.headers.get("Range", "")))
        queued = srv.status.get(self.path)
        if queued:
            self.send_error(queued.pop(0))
            return
        data = srv.files.get(self.path)
        if data is None:
            self.send_error(404)
            return
        start = 0
        wanted = self.headers.get("Range")
        if wanted and srv.ranges:
            start = int(wanted.removeprefix("bytes=").rstrip("-"))
            self.send_response(206)
        else:
            self.send_response(200)
        body = data[start:]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        cut = srv.cut_after.pop(self.path, None)
        if cut is not None:
            self.wfile.write(body[:cut])
            self.wfile.flush()
            self.connection.shutdown(2)
            return
        self.wfile.write(body)


@pytest.fixture
def github(monkeypatch):
    srv = FakeGitHub()
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(update.time, "sleep", lambda _s: None)
    monkeypatch.setattr(update, "TIMEOUT", 5)
    yield srv
    srv.shutdown()
    srv.server_close()


def _publish(srv: FakeGitHub, version="0.5.0", installer=INSTALLER, sums_for=None, prerelease=False):
    """A release on the fake GitHub; returns what update.newest makes of its list entry."""
    name = f"MirrorBridge-Setup-{version}.exe"
    digest = hashlib.sha256(sums_for if sums_for is not None else installer).hexdigest()
    srv.files[f"/dl/{name}"] = installer
    srv.files["/dl/SHA256SUMS"] = f"{'0' * 64}  MirrorBridge.exe\n{digest}  {name}\n".encode()
    entry = _release(f"v{version}", prerelease)
    srv.files["/releases"] = json.dumps([entry]).encode()
    release = update.newest([entry], "0.4.0")
    return update.Release(**{**release.__dict__, "installer_url": f"{srv.base}/dl/{name}",
                             "sums_url": f"{srv.base}/dl/SHA256SUMS"})


def test_check_reads_the_release_list(github):
    _publish(github, "0.5.0", prerelease=True)
    assert update.check("0.4.0", url=f"{github.base}/releases").version == "0.5.0"
    assert update.check("0.4.0", prereleases=False, url=f"{github.base}/releases") is None
    assert update.check("0.5.0", url=f"{github.base}/releases") is None


def test_the_rate_limit_is_not_hammered(github):
    github.files["/releases"] = b"[]"
    github.status["/releases"] = [403, 403, 403]
    with pytest.raises(update.UpdateError) as caught:
        update.check("0.4.0", url=f"{github.base}/releases")
    assert caught.value.retry is False
    assert len(github.requests) == 1


def test_a_server_error_is_tried_again(github):
    _publish(github)
    github.status["/releases"] = [502, 503]
    assert update.check("0.4.0", url=f"{github.base}/releases").version == "0.5.0"
    assert len(github.requests) == 3


def test_no_network_is_an_update_error(monkeypatch):
    monkeypatch.setattr(update.time, "sleep", lambda _s: None)
    with pytest.raises(update.UpdateError):
        update.check("0.4.0", url="http://127.0.0.1:9/releases")


def test_a_list_that_is_not_json_is_an_update_error(github):
    github.files["/releases"] = b"<html>"
    with pytest.raises(update.UpdateError):
        update.check("0.4.0", url=f"{github.base}/releases")


def test_the_environment_can_point_elsewhere(github, monkeypatch):
    _publish(github)
    monkeypatch.setenv("MIRRORBRIDGE_UPDATE_URL", f"{github.base}/releases")
    assert update.check("0.4.0").version == "0.5.0"


# -- downloading -------------------------------------------------------------


def test_the_installer_is_downloaded_and_checked(github, tmp_path):
    release = _publish(github)
    seen = []
    path = update.download(release, tmp_path, progress=lambda have, size: seen.append((have, size)))
    assert path == tmp_path / "MirrorBridge-Setup-0.5.0.exe"
    assert path.read_bytes() == INSTALLER
    assert seen[-1] == (len(INSTALLER), len(INSTALLER))
    assert not list(tmp_path.glob("*.part"))


def test_a_checked_installer_is_not_downloaded_again(github, tmp_path):
    release = _publish(github)
    update.download(release, tmp_path)
    github.requests.clear()
    update.download(release, tmp_path)
    assert [path for path, _ in github.requests] == ["/dl/SHA256SUMS"]


def test_a_broken_download_resumes_where_it_stopped(github, tmp_path):
    release = _publish(github)
    github.cut_after["/dl/MirrorBridge-Setup-0.5.0.exe"] = 100_000
    path = update.download(release, tmp_path)
    assert path.read_bytes() == INSTALLER
    ranges = [r for p, r in github.requests if p.endswith(".exe")]
    assert ranges[0] == "" and ranges[-1].startswith("bytes=") and int(ranges[-1][6:-1]) > 0


def test_a_server_that_cannot_resume_starts_again(github, tmp_path):
    release = _publish(github)
    github.ranges = False
    github.cut_after["/dl/MirrorBridge-Setup-0.5.0.exe"] = 100_000
    assert update.download(release, tmp_path).read_bytes() == INSTALLER


def test_an_installer_that_does_not_match_sha256sums_is_deleted(github, tmp_path):
    release = _publish(github, sums_for=b"something else")
    with pytest.raises(update.UpdateError, match="did not match"):
        update.download(release, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_a_file_that_is_not_a_program_is_refused(github, tmp_path):
    fake = b"<html>" + INSTALLER[6:]
    release = _publish(github, installer=fake)
    with pytest.raises(update.UpdateError):
        update.download(release, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_an_installer_larger_than_github_said_is_refused(github, tmp_path):
    release = _publish(github, installer=INSTALLER + b"more")
    release = update.Release(**{**release.__dict__, "installer_size": len(INSTALLER)})
    with pytest.raises(update.UpdateError):
        update.download(release, tmp_path)
    assert not (tmp_path / release.installer_name).exists()


def test_sha256sums_without_the_installer_is_refused(github, tmp_path):
    release = _publish(github)
    github.files["/dl/SHA256SUMS"] = f"{'0' * 64}  MirrorBridge.exe\n".encode()
    with pytest.raises(update.UpdateError, match="no line"):
        update.download(release, tmp_path)


def test_a_stopped_download_stops(github, tmp_path):
    release = _publish(github)
    with pytest.raises(update.UpdateError, match="stopped"):
        update.download(release, tmp_path, cancelled=lambda: True)


def test_clean_keeps_only_the_installer_in_use(tmp_path):
    for name in ("MirrorBridge-Setup-0.4.0.exe", "MirrorBridge-Setup-0.5.0.exe", "MirrorBridge-Setup-0.6.0.exe.part",
                 "settings.json"):
        (tmp_path / name).write_bytes(b"x")
    update.clean(tmp_path, keep="MirrorBridge-Setup-0.5.0.exe")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["MirrorBridge-Setup-0.5.0.exe", "settings.json"]


def test_the_installer_is_started_as_an_update(tmp_path):
    command = update.install_command(tmp_path / "MirrorBridge-Setup-0.5.0.exe")
    assert command[0].endswith("MirrorBridge-Setup-0.5.0.exe")
    assert {"/SILENT", "/SUPPRESSMSGBOXES", "/UPDATE", "/RELAUNCH"} <= set(command)
    assert "/RELAUNCH" not in update.install_command(tmp_path / "x.exe", relaunch=False)


@pytest.mark.skipif(sys.platform == "win32", reason="not the Windows exe")
def test_from_source_nothing_is_installed():
    assert not update.can_install()


# -- the installer script and the release agree -------------------------------


ROOT = Path(__file__).resolve().parents[1]


def test_the_installer_script_and_the_app_use_the_same_names():
    script = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
    assert f'#define AppMutex "{update.APP_MUTEX}"' in script
    app_id = update.UNINSTALL_KEY.rsplit("\\", 1)[1].removesuffix("_is1")
    assert f"AppId={{{app_id}" in script
    assert "OutputBaseFilename=MirrorBridge-Setup-{#AppVersion}" in script
    assert "PrivilegesRequired=lowest" in script
    for switch in ("/UPDATE", "/RELAUNCH"):
        assert f"'{switch}'" in script
    # A portable copy that updates itself must not come back at the next logon: the installer points
    # Start with Windows (the Run value the app writes, gui/winintegration.py) at the installed exe.
    post = script[script.index("procedure CurStepChanged"):script.index("function InitializeUninstall")]
    assert "ssPostInstall" in post and "RegWriteStringValue" in post and "{app}\\MirrorBridge.exe" in post
    test = (ROOT / "packaging" / "test_installer.ps1").read_text(encoding="utf-8")
    assert "Run value" in test
    assert update.APP_MUTEX in test and app_id in test


def test_the_release_publishes_the_installer():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "build_installer.ps1" in workflow and "test_installer.ps1" in workflow
    assert '--installer "$env:SETUP"' in workflow
    assert "tests/test_update.py" in workflow


# -- installed already: update in place, never a second copy -------------------


@pytest.mark.parametrize("installed, version, hands_over", [
    (Path("C:/x/MirrorBridge.exe"), "0.4.0", True),   # the same version installed: start that one
    (Path("C:/x/MirrorBridge.exe"), "0.5.0", True),   # a newer one installed
    (Path("C:/x/MirrorBridge.exe"), "0.3.9", False),  # an older one: this copy runs, and its updates go over it
    (None, "0.5.0", False), (Path("C:/x/MirrorBridge.exe"), None, False), (Path("C:/x/MirrorBridge.exe"), "junk", False),
])
def test_a_copy_from_elsewhere_hands_over_to_the_installed_one(installed, version, hands_over):
    assert update.hand_over_to(installed, version, "0.4.0") is hands_over


def test_installing_from_a_portable_copy_takes_a_newer_release_or_its_own():
    releases = [_release("v0.4.0", prerelease=True), _release("v0.3.0")]
    assert update.for_install(releases, "0.4.0").version == "0.4.0"  # its own installer, even a pre-release
    assert update.for_install(releases + [_release("v0.5.0")], "0.4.0").version == "0.5.0"
    assert update.for_install(releases, "0.4.1") is None  # not published (a test build)
    assert update.for_install("junk", "0.4.0") is None


def test_the_installer_updates_in_place_and_never_downgrades():
    script = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
    assert "UsePreviousAppDir=yes" in script and "DisableDirPage=auto" in script
    app_id = update.UNINSTALL_KEY.rsplit("\\", 1)[1]
    assert app_id in script  # it reads the version installed before from its own uninstall entry
    setup = script[script.index("function InitializeSetup"):script.index("function PrepareToInstall")]
    assert "CompareVersions(InstalledVersion, '{#AppVersion}') > 0" in setup and "/ALLOWDOWNGRADE" in setup
    assert "UpdateFrom" in script and "UpdateButton" in script
    test = (ROOT / "packaging" / "test_installer.ps1").read_text(encoding="utf-8")
    assert '"99.0.0"' in test and "entries in Apps" in test
