"""What the core modules say reaches the GUI in English and in Spanish.

Every Text("key", "English", ...) in the code is looked up here: gui.i18n must have the key
in both languages, its English must be the core module's (which the command line and the
log use), and its Spanish must take the same values.
"""

import ast
import string
from pathlib import Path

import pytest

from mirrorbridge import ffmpeg
from mirrorbridge.gui import i18n
from mirrorbridge.text import Text

PACKAGE = Path(__file__).resolve().parents[1] / "mirrorbridge"


def _texts() -> list[tuple[str, str, str]]:
    """(file:line, key, English) of every Text(...) the code makes."""
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Text":
                where = f"{path.relative_to(PACKAGE.parent).as_posix()}:{node.lineno}"
                key, english = node.args[:2]
                assert isinstance(key, ast.Constant) and isinstance(english, ast.Constant), where
                found.append((where, key.value, english.value))
    return found


TEXTS = _texts()


def _fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


class _Unfilled(dict):
    def __missing__(self, name: str) -> str:
        return "{" + name + "}"


def test_the_code_has_texts_to_check():
    assert len(TEXTS) > 30


@pytest.mark.parametrize("where, key, english", TEXTS, ids=[key for _, key, _ in TEXTS])
def test_each_text_is_in_english_and_spanish(where, key, english):
    entry = i18n._STRINGS[key]  # pylint: disable=protected-access
    menus = i18n._menu_paths()  # pylint: disable=protected-access
    # The GUI's English is the command line's, menu paths included.
    assert entry["en"].format_map(_Unfilled(menus)) == english, where
    assert _fields(entry["es"]) == _fields(entry["en"]), where
    assert {name for name in _fields(entry["en"]) if name.startswith("menu_")} <= set(menus), where


def test_say_shows_a_text_in_spanish_and_leaves_other_words_alone():
    advice = Text("hint_smooth", "Try Quality > Delay > Smooth.")
    nested = Text("tv_rejected", "The TV downloaded the playlist but none of the video{asked}, so it probably "
                  "does not accept this video format. {hint}", asked="", hint=advice)
    assert i18n.say(nested) == str(nested)
    i18n.set_language("es")
    try:
        assert i18n.say(nested).endswith("este formato de video. Prueba Calidad > Retraso > Fluido.")
        assert i18n.say("192.168.1.40") == "192.168.1.40"
        assert i18n.say(Text("not_a_key", "Only {what}", what="English")) == "Only English"
    finally:
        i18n.set_language("en")


def test_the_gui_shows_an_errors_text_in_spanish():
    pytest.importorskip("PySide6.QtWidgets")  # mirrorbridge.gui.extend shows Qt dialogs
    from mirrorbridge.gui.extend import error_text

    with pytest.raises(RuntimeError) as err:
        ffmpeg.window_region("A window MirrorBridge will not find", [])
    assert error_text(err.value) == str(err.value)
    i18n.set_language("es")
    try:
        assert error_text(err.value) == ('La ventana "A window MirrorBridge will not find" no está en la pantalla. '
                                         "Restáurala si está minimizada, o elige otra.")
        assert error_text(OSError(13, "Acceso denegado")) == "[Errno 13] Acceso denegado"  # Windows' own words
    finally:
        i18n.set_language("en")
