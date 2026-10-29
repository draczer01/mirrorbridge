"""More options > Debug > Latency meter: a big clock on every screen, closed with Esc, which shows
the last "Mirroring:" line of the log; and next to it Mirror screen's test switches (the TV's wait,
how often a keyframe comes, frames shown on arrival). Qt runs offscreen, on a controller that
neither scans the network nor tests encoders."""

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


def _debug(tray) -> dict:
    more = next(a.menu() for a in tray.menu.actions() if a.text() == i18n.tr("more"))
    debug = next(a.menu() for a in more.actions() if a.text() == i18n.tr("debug_menu"))
    return {a.text(): a for a in debug.actions() if not a.isSeparator()}


def _latency_action(tray):
    return _debug(tray)[i18n.tr("latency_meter")]


def _choices(item) -> dict:
    return {a.text(): a for a in item.menu().actions()}


def _checked(item) -> list[str]:
    return [text for text, action in _choices(item).items() if action.isChecked()]


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
    assert windows[0].paints >= 1
    assert all(window._timer.isActive() for window in windows)  # pylint: disable=protected-access

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
    assert not any(window._timer.isActive() for window in windows)  # pylint: disable=protected-access


def test_each_window_repaints_at_its_screens_refresh_rate():
    from mirrorbridge.gui.latency import tick_ms

    assert tick_ms(200) == 5
    assert tick_ms(60) == 17
    assert tick_ms(1000) == 2  # never more often than every 2 ms
    assert tick_ms(0) == 16  # a screen that does not say


def test_a_tick_paints_only_the_clock_and_the_cells_from_pixmaps(controller):
    """The paint the meter does on every frame of the H27T6 (2560x1440 at 200 Hz), 200 times.
    Painting holds the GIL the mirroring session needs: the whole window with huge text on
    every frame starved it."""
    from PySide6.QtGui import QPixmap, QRegion

    from mirrorbridge.gui.latency import LatencyMeter, _ClockWindow

    window = _ClockWindow(LatencyMeter(), "H27T6", 200)
    window.resize(2560, 1440)
    pix = QPixmap(2560, 1440)
    window.render(pix)
    assert window.last_parts == {"clock", "cells", "text"}  # the first paint draws it all

    updates = []
    window.update = lambda region: updates.append(QRegion(region))
    window.meter.started -= 1.05  # a second and a tenth later: every digit but the first, two cells
    window._tick()  # pylint: disable=protected-access
    assert len(updates) == 1 and not updates[0].isEmpty()
    assert window.tick_rect.contains(updates[0].boundingRect())
    assert not updates[0].intersects(window.text_rect)
    assert not updates[0].intersects(window._glyph_rect(0))  # pylint: disable=protected-access

    paints, spent = window.paints, window.paint_seconds
    for _ in range(200):  # a frame of the H27T6 apart: the last digit or two change on each
        updates.clear()
        window.meter.started -= 0.005
        window._tick()  # pylint: disable=protected-access
        if updates:
            window.render(pix, sourceRegion=updates[0])
            assert window.last_parts <= {"clock", "cells"}
    assert window.paints > paints + 150
    ms = (window.paint_seconds - spent) * 1000 / (window.paints - paints)
    print(f"latency meter: {ms:.3f} ms per tick at 2560x1440")
    assert ms < 5  # well under 1 ms on the PC it was written on; loose for slow CI machines


def test_the_menu_is_in_spanish_too(controller):
    from mirrorbridge.gui.tray import TrayMenu

    controller.set_language("es")
    tray = TrayMenu(controller, lambda: None, lambda: None)
    tray._rebuild()  # pylint: disable=protected-access
    assert _latency_action(tray).text() == "Medidor de latencia"
    items = _debug(tray)
    assert list(items) == ["Medidor de latencia", "Espera de la TV (prueba)", "Cuadros clave",
                           "Mostrar cuadros al llegar (prueba)"]
    assert list(_choices(items["Espera de la TV (prueba)"])) == [
        "Automática (la guardada para la TV)", "60 ms", "100 ms", "150 ms", "200 ms", "300 ms"]
    assert list(_choices(items["Cuadros clave"])) == ["Solo cuando hace falta", "Cada 10 s", "Cada 2 s"]
    assert _checked(items["Cuadros clave"]) == ["Solo cuando hace falta"]


def test_mirror_screens_test_switches_are_next_to_the_latency_meter(controller):
    """They start from the defaults, and each choice is saved for the next Mirror screen."""
    from mirrorbridge.gui.tray import TrayMenu

    tray = TrayMenu(controller, lambda: None, lambda: None)
    tray._rebuild()  # pylint: disable=protected-access
    items = _debug(tray)
    assert list(items) == ["Latency meter", "TV wait (test)", "Keyframes", "Show frames on arrival (test)"]
    waits, keyframes, arrival = items["TV wait (test)"], items["Keyframes"], items["Show frames on arrival (test)"]
    assert list(_choices(waits)) == ["Automatic (saved for the TV)", "60 ms", "100 ms", "150 ms", "200 ms", "300 ms"]
    assert _checked(waits) == ["Automatic (saved for the TV)"]
    assert list(_choices(keyframes)) == ["Only when needed", "Every 10 s", "Every 2 s"]
    assert _checked(keyframes) == ["Only when needed"]
    assert arrival.isCheckable() and not arrival.isChecked()

    _choices(waits)["200 ms"].trigger()
    _choices(keyframes)["Every 10 s"].trigger()
    arrival.trigger()
    s = controller.settings
    assert (s.mirror_lead_ms, s.mirror_keyframes, s.mirror_on_arrival) == (200, 10, True)
    saved = Settings.load()
    assert (saved.mirror_lead_ms, saved.mirror_keyframes, saved.mirror_on_arrival) == (200, 10, True)

    tray._rebuild()  # pylint: disable=protected-access
    items = _debug(tray)
    assert _checked(items["TV wait (test)"]) == ["200 ms"] and _checked(items["Keyframes"]) == ["Every 10 s"]
    assert items["Show frames on arrival (test)"].isChecked()
    _choices(items["TV wait (test)"])["Automatic (saved for the TV)"].trigger()
    items["Show frames on arrival (test)"].trigger()
    assert (s.mirror_lead_ms, s.mirror_on_arrival) == (0, False)

    # A settings.json edited by hand: each shows what a session uses (only when needed, the TV's
    # own lead, at their time), and the menu is still made.
    s.mirror_keyframes, s.mirror_lead_ms, s.mirror_on_arrival = 3, -5, "false"
    tray._rebuild()  # pylint: disable=protected-access
    items = _debug(tray)
    assert _checked(items["Keyframes"]) == ["Only when needed"]
    assert _checked(items["TV wait (test)"]) == ["Automatic (saved for the TV)"]
    assert not items["Show frames on arrival (test)"].isChecked()
