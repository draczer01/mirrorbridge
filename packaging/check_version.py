"""Check that the code says one version everywhere, and that a release tag names that version.

    python packaging/check_version.py                  # prints the version, e.g. 0.3.0
    python packaging/check_version.py v0.3.0           # fails unless the tag is v<that version>
    python packaging/check_version.py v0.3.0 --notes   # ... and docs/release-notes/v0.3.0.md exists

The release workflow runs it before it builds anything, so a tag can never publish an exe that
calls itself another version, or a release without its notes.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Releases are X.Y.Z: the exe's version resource holds four numbers, the last one 0.
VERSION = re.compile(r"\d+\.\d+\.\d+")


def versions(root: Path = ROOT) -> dict[str, str]:
    """The version each file that states one gives, by file."""
    found: dict[str, str] = {}
    init = root / "mirrorbridge" / "__init__.py"
    for node in ast.parse(init.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "__version__" for t in node.targets):
            found["mirrorbridge/__init__.py"] = ast.literal_eval(node.value)
    with open(root / "pyproject.toml", "rb") as f:
        found["pyproject.toml"] = tomllib.load(f)["project"]["version"]
    info = (root / "packaging" / "version_info.txt").read_text(encoding="utf-8")
    for field in ("filevers", "prodvers"):
        numbers = re.search(rf"{field}=\((\d+), (\d+), (\d+), (\d+)\)", info)
        if numbers:
            *xyz, last = numbers.groups()
            found[f"version_info.txt {field}"] = ".".join(xyz) + ("" if last == "0" else f".{last}")
    for field in ("FileVersion", "ProductVersion"):
        text = re.search(rf"StringStruct\('{field}', '([^']*)'\)", info)
        if text:
            found[f"version_info.txt {field}"] = text.group(1)
    return found


def code_version(root: Path = ROOT) -> str:
    """The version, once every file agrees on it; stops with what disagrees otherwise."""
    found = versions(root)
    expected = ("mirrorbridge/__init__.py", "pyproject.toml", "version_info.txt filevers",
                "version_info.txt prodvers", "version_info.txt FileVersion", "version_info.txt ProductVersion")
    missing = [name for name in expected if name not in found]
    if missing:
        raise SystemExit(f"No version found in {', '.join(missing)}")
    if len(set(found.values())) != 1:
        raise SystemExit("The version differs between files: "
                         + ", ".join(f"{name} says {value}" for name, value in found.items()))
    version = found["mirrorbridge/__init__.py"]
    if not VERSION.fullmatch(version):
        raise SystemExit(f"Version {version} is not X.Y.Z")
    return version


def release_notes(version: str, root: Path = ROOT) -> Path:
    return root / "docs" / "release-notes" / f"v{version}.md"


def check(tag: str | None = None, notes: bool = False, root: Path = ROOT) -> str:
    """The code's version, after checking that the tag (v<version>, or refs/tags/v<version>) names it."""
    version = code_version(root)
    if tag is not None:
        tag = tag.removeprefix("refs/tags/")
        if tag != f"v{version}":
            raise SystemExit(f"Tag {tag} does not match the code, which is version {version} (tag v{version})")
    if notes:
        path = release_notes(version, root)
        if not path.is_file() or not path.read_text(encoding="utf-8").strip():
            raise SystemExit(f"{path.relative_to(root).as_posix()} is missing: write the release notes first")
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag", nargs="?", help="the release tag, e.g. v0.3.0")
    parser.add_argument("--notes", action="store_true", help="also require docs/release-notes/v<version>.md")
    args = parser.parse_args(argv)
    print(check(args.tag, args.notes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
