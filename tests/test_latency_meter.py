"""More options > Debug > Latency meter: a big clock on every screen, closed with Esc, which shows
the last "Mirroring:" line of the log. Qt runs offscreen, on a controller that neither scans the
network nor tests encoders."""

import logging
import os

import pytest

from mirrorbridge import ffmpeg
from mirrorbridge.config import Settings
from mirrorbridge.gui import i18n

_APP = None  # one QApplication for every test here; it must outlive them


@pytest.fixture
def controller(tmp_path, monkeypatch):
    global _APP  # pylint: disable=global-statement
    widgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _APP = widgets.QApplication.instance() or widgets.QApplication([])
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from mirrorbridge.gui.controller import Controller

    monkeypatch.setattr(Controller, "refresh_if_stale", lambda self, max_age=60: None)
    monkeypatch.setattr(Controller, "refresh_encoders_if_stale", lambda self, max_age=120: None)
    monkeypatch.setattr(ffmpeg, "list_monitors", lambda: [ffmpeg.Monitor(0, 0, 0, 1920, 1080, True)])
    monkeypatch.setattr(ffmpeg, "list_windows", lambda: [])
    c = Controller(Settings.load())
    yield c
    c.shutdown()
    i18n.set_language("en")


def _latency_action(tray):
    more = next(a.menu() for a in tray.menu.actions() if a.text() == i18n.tr("more"))
    debug = next(a.menu() for a in more.actions() if a.text() == i18n.tr("debug_menu"))
    return next(a for a in debug.actions() if a.text() == i18n.tr("latency_meter"))


def test_the_clock_shows_seconds_and_milliseconds():
    from mirrorbridge.gui.latency import clock_text

    assert clock_text(0) == "00.000"
    assert clock_text(7.0425) == "07.042"
    assert clock_text(123.4) == "23.400"  # the minutes are left out


def test_the_log_line_is_cut_to_the_frame_rate_the_age_and_the_lead():
    from mirrorbridge.gui.latency import short_stats

    line = ("Mirroring: 24.0 fps (of 24), 1.7 Mbit/s; ms p50/p95: pipe 8.9/16.0, behind 9.2/16.5, queue 0.2/0.3, "
            "pack 0.1/0.1, send 0.1/0.1, age 9.3/16.5 (after capture and encoding); lead 100 ms; up to 0 KB waiting "
            "to go; keyframes 16 (largest 97 KB); dropped 0; the TV asked for the time 2 times")
    assert short_stats(line) == "24.0 fps (of 24) · age p50/p95 9.3/16.5 ms · lead 100 ms"
    assert short_stats("Mirroring: 0.0 fps (of 24), 0.0 Mbit/s; ms p50/p95: no frames") == (
        "Mirroring: 0.0 fps (of 24), 0.0 Mbit/s; ms p50/p95: no frames")


def test_the_debug_menu_opens_a_clock_on_every_screen_and_esc_closes_them_all(controller, monkeypatch):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QGuiApplication, QKeyEvent

    from mirrorbridge.gui.tray import TrayMenu

    tray = TrayMenu(controller, lambda: None, lambda: None)
    tray._rebuild()  # pylint: disable=protected-access
    _latency_action(tray).trigger()
    meter = tray._meter  # pylint: disable=protected-access
    assert meter is not None
    windows = list(meter.windows.values())
    assert len(windows) == len(QGuiApplication.screens())
    assert all(window.isFullScreen() for window in windows)
    assert not windows[0].grab().isNull()  # paints: the clock, the tenths and the hints
    assert windows[0].frames >= 1

    _latency_action(tray).trigger()  # once is enough: it does not open a second set
    assert tray._meter is meter  # pylint: disable=protected-access

    stats_logger = logging.getLogger("mirrorbridge.mirror.stats")
    monkeypatch.setattr(stats_logger, "disabled", False)  # as the app logs, whatever tests before set
    level = stats_logger.level
    stats_logger.setLevel(logging.INFO)  # setLevel, not the attribute: it also clears the logger's cache
    try:
        stats_logger.info("Mirroring: %.1f fps (of %d)", 24.0, 24)
    finally:
        stats_logger.setLevel(level)
    assert meter.stats == "Mirroring: 24.0 fps (of 24)"

    windows[0].keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    assert tray._meter is None and not meter.windows  # pylint: disable=protected-access
    assert not any(window.isVisible() for window in windows)
    assert meter._handler not in logging.getLogger("mirrorbridge.mirror.stats").handlers  # pylint: disable=protected-access


def test_the_menu_is_in_spanish_too(controller):
    from mirrorbridge.gui.tray import TrayMenu

    controller.set_language("es")
    tray = TrayMenu(controller, lambda: None, lambda: None)
    tray._rebuild()  # pylint: disable=protected-access
    assert _latency_action(tray).text() == "Medidor de latencia"
