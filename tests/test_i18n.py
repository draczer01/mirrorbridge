"""The app's languages: English unless the user picks another under Language / Idioma, and
English for any string a language lacks.

The Qt tests build the real tray menu and window offscreen, on a controller that neither scans
the network nor runs FFmpeg, with the settings file in a temporary folder.
"""

import json
import os
import string

import pytest

from mirrorbridge import ffmpeg, migration
from mirrorbridge.config import Settings
from mirrorbridge.gui import i18n
from mirrorbridge.session import State
from mirrorbridge.text import Text


@pytest.fixture(autouse=True)
def back_to_english():
    yield
    i18n.set_language("en")


@pytest.fixture
def app_data(tmp_path, monkeypatch):
    """%APPDATA% (~/.config off Windows) in tmp_path, so settings are tmp_path/MirrorBridge/settings.json."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def french(monkeypatch):
    """A third language, registered only for this test, with three of its strings."""
    monkeypatch.setitem(i18n.LANGUAGES, "fr", "Français")
    for key, words in (("language_menu", "Langue"), ("quit", "Quitter"), ("state_casting", "Diffusion")):
        monkeypatch.setitem(i18n._STRINGS[key], "fr", words)  # pylint: disable=protected-access


def _fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


# -- English by default ----------------------------------------------------------------------------


def test_english_without_a_settings_file(app_data):
    settings = Settings.load()
    assert settings.language == "en"
    assert i18n.set_language(settings.language) == "en" and i18n.tr("quit") == "Quit"


def test_the_old_follow_windows_setting_is_english(app_data):
    # The settings of the app's old name are brought over, with the "" that meant "as Windows".
    (app_data / migration.OLD_NAME).mkdir()
    (app_data / migration.OLD_NAME / "settings.json").write_text(json.dumps({"language": "", "latency_mode": "smooth"}))
    settings = Settings.load()
    assert (settings.language, settings.latency_mode) == ("", "smooth")
    i18n.set_language("es")
    assert i18n.set_language(settings.language) == "en"
    assert i18n.language() == "en" and i18n.tr("quit") == "Quit"


@pytest.mark.parametrize("code", ["", "fr", "xx"])
def test_a_language_the_app_does_not_speak_is_english(code):
    i18n.set_language("es")
    assert i18n.set_language(code) == "en" and i18n.tr("quit") == "Quit"


# -- the strings -----------------------------------------------------------------------------------


def test_english_is_the_default_and_first_language():
    assert i18n.DEFAULT == "en" and list(i18n.LANGUAGES)[0] == "en"
    assert i18n.LANGUAGES == {"en": "English", "es": "Español"}  # for now
    assert i18n.language_menu_title() == "Language / Idioma"


def test_every_string_is_in_english_and_spanish_with_the_same_values():
    for key, entry in i18n._STRINGS.items():  # pylint: disable=protected-access
        assert entry.get("en") and entry.get("es"), key
        assert set(entry) <= set(i18n.LANGUAGES), key  # no language the app does not list
        for code, words in entry.items():
            assert _fields(words) == _fields(entry["en"]), (key, code)


@pytest.mark.usefixtures("french")
def test_a_language_shows_english_for_any_string_it_lacks():
    assert i18n.set_language("fr") == "fr"
    assert i18n.tr("quit") == "Quitter"
    assert i18n.tr("open") == "Open MirrorBridge"
    assert i18n.tr("status_casting", device="Salon") == "Casting to Salon"
    assert i18n.state_text(State.CASTING) == "Diffusion" and i18n.state_text(State.FAILED) == "Failed"
    # A core module's Text, and the menu path it names, in English too
    assert i18n.say(Text("hint_smooth", "Try Quality > Delay > Smooth.")) == "Try Quality > Delay > Smooth."
    assert i18n._menu_paths()["menu_forget"] == "Device > Forget pairing"  # pylint: disable=protected-access
    assert i18n.language_menu_title() == "Language / Idioma / Langue"
    assert i18n.tr("not_a_key") == "not_a_key"  # a string no language has


# -- the tray menu and the window, offscreen -------------------------------------------------------

_APP = None  # one QApplication for every test here; it must outlive them


@pytest.fixture
def controller(app_data, monkeypatch):
    global _APP  # pylint: disable=global-statement
    # Qt also fails to load without libEGL (ImportError)
    widgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _APP = widgets.QApplication.instance() or widgets.QApplication([])
    from mirrorbridge.gui.controller import Controller

    monkeypatch.setattr(Controller, "refresh_if_stale", lambda self, max_age=60: None)  # no network scan
    monkeypatch.setattr(Controller, "refresh_encoders_if_stale", lambda self, max_age=120: None)  # no FFmpeg
    monkeypatch.setattr(ffmpeg, "list_monitors", lambda: [ffmpeg.Monitor(0, 0, 0, 1920, 1080, True)])
    monkeypatch.setattr(ffmpeg, "list_windows", lambda: [])
    settings = Settings.load()
    i18n.set_language(settings.language)  # as the app starts
    c = Controller(settings)
    yield c
    c.shutdown()


def _menu_texts(menu) -> list[str]:
    """Every item in a menu and its submenus, depth first."""
    found = []
    for action in menu.actions():
        if not action.isSeparator():
            found.append(action.text())
            if action.menu():
                found += _menu_texts(action.menu())
    return found


def _submenu(menu, title: str):
    return next(action.menu() for action in menu.actions() if action.text() == title)


def _window_texts(window) -> list[str]:
    from PySide6.QtWidgets import QAbstractButton, QComboBox, QGroupBox, QLabel, QListWidget, QPlainTextEdit, QWidget

    found = [window.windowTitle()]
    for widget in window.findChildren(QWidget):
        found.append(widget.toolTip())
        if isinstance(widget, (QLabel, QAbstractButton)):
            found.append(widget.text())
        elif isinstance(widget, QGroupBox):
            found.append(widget.title())
        elif isinstance(widget, QComboBox):
            found += [widget.itemText(i) for i in range(widget.count())]
        elif isinstance(widget, QPlainTextEdit):
            found.append(widget.placeholderText())
        elif isinstance(widget, QListWidget):
            found += [widget.item(i).text() for i in range(widget.count())]
    return [text for text in found if text]


def _same_in_every_language() -> set[str]:
    """What the menu and the window show that is not a word: sizes, rates, the languages' names."""
    from mirrorbridge.gui import extend, tray

    same = {i18n.language_menu_title(), *i18n.LANGUAGES.values()}
    same |= {f"{f} fps" for f in tray.FRAMERATES} | {f"{b} Mbps" for b in tray.BITRATES}
    same |= {name for name, height in tray.RESOLUTIONS if height}
    same |= {f"{ms} ms" for ms in tray.DEBUG_LEADS if ms} | {f"{n} s" for n in ffmpeg.MIRROR_KEYFRAME_CHOICES}
    return same | {extend.size_label(mode) for _name, mode in extend.EXTEND_SIZES}


@pytest.mark.usefixtures("french")
def test_the_language_menu_lists_every_registered_language(controller):
    """French exists only in i18n (the fixture): the menu and the window offer it with no other change."""
    from mirrorbridge.gui.tray import TrayMenu
    from mirrorbridge.gui.window import MainWindow

    tray = TrayMenu(controller, lambda: None, lambda: None)
    languages = _submenu(tray.menu, "Language / Idioma / Langue")
    assert [a.text() for a in languages.actions()] == ["English", "Español", "Français"]
    assert [a.isChecked() for a in languages.actions()] == [True, False, False]
    window = MainWindow(controller)
    assert [window.language_combo.itemText(i) for i in range(window.language_combo.count())] == \
        ["English", "Español", "Français"]

    languages.actions()[2].trigger()
    assert i18n.language() == "fr" and Settings.load().language == "fr"
    texts = _menu_texts(tray.menu)
    assert "Quitter" in texts and "Open MirrorBridge" in texts  # its own words, and English for the rest
    assert [a.isChecked() for a in _submenu(tray.menu, "Language / Idioma / Langue").actions()] == [False, False, True]
    assert window.language_combo.currentText() == "Français"


def test_choosing_spanish_retranslates_the_tray_menu_at_once(controller):
    from mirrorbridge.gui.tray import TrayMenu

    tray = TrayMenu(controller, lambda: None, lambda: None)
    english = _menu_texts(tray.menu)
    assert english[0] == "Open MirrorBridge" and english[-1] == "Quit" and "More options" in english
    assert tray.tray.toolTip() == "MirrorBridge - Not casting"

    _submenu(tray.menu, "Language / Idioma").actions()[1].trigger()  # Español
    spanish = _menu_texts(tray.menu)
    assert spanish[0] == "Abrir MirrorBridge" and spanish[-1] == "Salir" and "Más opciones" in spanish
    assert [a.isChecked() for a in _submenu(tray.menu, "Language / Idioma").actions()] == [False, True]
    assert tray.tray.toolTip() == "MirrorBridge - Sin transmitir"
    assert set(english) & set(spanish) <= _same_in_every_language()  # no English left

    _submenu(tray.menu, "Language / Idioma").actions()[0].trigger()  # English
    assert _menu_texts(tray.menu) == english


def test_the_choice_is_kept_for_the_next_start(controller, app_data):
    from mirrorbridge.gui.tray import TrayMenu

    controller.set_language("es")
    saved = json.loads((app_data / "MirrorBridge" / "settings.json").read_text(encoding="utf-8"))
    assert saved["language"] == "es"
    i18n.set_language("en")  # a new start: only the settings file says Spanish
    assert i18n.set_language(Settings.load().language) == "es"
    assert _menu_texts(TrayMenu(controller, lambda: None, lambda: None).menu)[0] == "Abrir MirrorBridge"


def test_choosing_spanish_in_the_window_retranslates_it_and_the_tray_menu(controller):
    from mirrorbridge.gui.tray import TrayMenu
    from mirrorbridge.gui.window import MainWindow

    tray = TrayMenu(controller, lambda: None, lambda: None)
    window = MainWindow(controller)
    controller.cast_state_changed.emit(State.STARTING, Text("checking_ffmpeg", "Checking FFmpeg and encoders"))
    assert window.status_label.text() == "Starting the encoder: Checking FFmpeg and encoders"
    english = _window_texts(window)

    window.language_combo.setCurrentIndex(window.language_combo.findData("es"))
    assert i18n.language() == "es" and Settings.load().language == "es"
    spanish = _window_texts(window)
    assert window.windowTitle() == "MirrorBridge - AirPlay para Windows"
    assert {"Dispositivos AirPlay", "Buscar de nuevo", "Qué transmitir", "Resolución", "Nativa",
            "Bajo (~3 s de retraso)", "Automático (el mejor disponible)", "Pantalla 1: 1920x1080 (principal)",
            "Transmitir"} <= set(spanish)
    # The status line is said again, what the session said included
    assert window.status_label.text() == "Iniciando el codificador: Comprobando FFmpeg y los codificadores"
    assert set(english) & set(spanish) <= _same_in_every_language()  # no English left
    assert _menu_texts(tray.menu)[0] == "Abrir MirrorBridge"

    _submenu(tray.menu, "Language / Idioma").actions()[0].trigger()  # English again, from the tray menu
    assert window.language_combo.currentText() == "English" and _window_texts(window) == english


def test_dialogs_name_their_buttons_in_the_users_language(controller):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QPushButton

    controller.set_language("es")
    buttons = []

    def answer() -> None:  # runs inside the dialog's exec()
        dialog = QApplication.activeModalWidget()
        buttons.append(sorted(button.text() for button in dialog.findChildren(QPushButton)))
        dialog.reject()

    QTimer.singleShot(0, answer)
    assert controller._ask_text(i18n.tr("add_ip"), i18n.tr("add_ip_prompt")) is None  # pylint: disable=protected-access
    QTimer.singleShot(0, answer)
    controller.show_error(i18n.tr("pair_failed"), "PIN")
    assert buttons == [["Aceptar", "Cancelar"], ["Aceptar"]]  # not Qt's own "OK" and "Cancel"
