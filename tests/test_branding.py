"""The app is MirrorBridge. Its old name, AirCast, is left only where the move from it needs it."""

import re
from pathlib import Path

import mirrorbridge

ROOT = Path(__file__).resolve().parents[1]
OLD_NAME = re.compile("aircast", re.IGNORECASE)

# Files that are about the old name as a whole.
ALLOWED_FILES = {"mirrorbridge/migration.py", "tests/test_migration.py", "tests/test_branding.py"}
# Lines elsewhere that may name it: the old build variable that still works.
ALLOWED_LINES = re.compile(r"\bAIRCAST_FFMPEG\b")
# Sections of the docs for people who had AirCast.
ALLOWED_SECTIONS = {"README.md": "## Formerly AirCast", "LEEME.md": "## Antes se llamaba AirCast"}


def _files() -> list[Path]:
    found = [ROOT / name for name in ("README.md", "LEEME.md", "LICENSE", "pyproject.toml", "run.bat")]
    for pattern in ("mirrorbridge/**/*.py", "tests/*.py", "packaging/*", ".github/workflows/*"):
        found += sorted(ROOT.glob(pattern))
    return [path for path in found if path.is_file() and path.suffix not in (".ico", ".png")]


def _section(lines: list[str], heading: str) -> range:
    """Line numbers of a Markdown section, from its heading to the next heading of its level."""
    start = lines.index(heading)
    level = heading.split(" ")[0] + " "
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith(level)), len(lines))
    return range(start, end)


def test_the_old_name_appears_only_where_the_move_from_it_needs_it():
    stray = []
    for path in _files():
        name = path.relative_to(ROOT).as_posix()
        if name in ALLOWED_FILES:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        allowed = _section(lines, ALLOWED_SECTIONS[name]) if name in ALLOWED_SECTIONS else range(0)
        stray += [f"{name}:{number + 1}: {line.strip()}" for number, line in enumerate(lines)
                  if OLD_NAME.search(line) and number not in allowed and not ALLOWED_LINES.search(line)]
    assert not stray, "\n".join(stray)


def test_the_docs_tell_aircast_users_what_happens():
    for name, heading in ALLOWED_SECTIONS.items():
        lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
        text = "\n".join(lines[i] for i in _section(lines, heading))
        for words in ("AirCast.exe", "settings.json", "pairings.json"):
            assert words in text, (name, words)


def test_the_version_is_the_same_everywhere():
    version = mirrorbridge.__version__
    assert re.search(rf'^version = "{re.escape(version)}"$', (ROOT / "pyproject.toml").read_text(), re.M)
    info = (ROOT / "packaging" / "version_info.txt").read_text(encoding="utf-8")
    numbers = tuple(int(n) for n in version.split(".")) + (0,)
    assert f"filevers={numbers}" in info and f"prodvers={numbers}" in info
    for field in ("FileVersion", "ProductVersion"):
        assert f"StringStruct('{field}', '{version}')" in info


def test_the_exe_says_it_is_mirrorbridge():
    info = (ROOT / "packaging" / "version_info.txt").read_text(encoding="utf-8")
    for field, value in (("CompanyName", "MirrorBridge"), ("InternalName", "MirrorBridge"),
                         ("ProductName", "MirrorBridge"), ("OriginalFilename", "MirrorBridge.exe")):
        assert f"StringStruct('{field}', '{value}')" in info
    assert mirrorbridge.APP_NAME == "MirrorBridge"


def test_hints_name_the_command_people_type():
    """`mirrorbridge` from source; the exe is MirrorBridge.exe (checked on the built exe)."""
    from mirrorbridge import cli, vdisplay

    assert mirrorbridge.CLI_NAME == "mirrorbridge"
    assert cli.build_parser().prog == "mirrorbridge"
    assert "'mirrorbridge vdd install'" in str(vdisplay.VddError("driver:not_installed"))
    assert "'mirrorbridge vdd install'" in cli._VDD_STATUS_TEXT[vdisplay.DriverStatus.NOT_INSTALLED]  # pylint: disable=protected-access
