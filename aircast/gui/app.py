"""AirCast desktop app: lives in the tray; the full window opens from the menu's "Open"."""

from __future__ import annotations

import getpass
import logging
import os
import sys
from typing import Optional

from PySide6.QtCore import QLocale, QLockFile, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from .. import APP_NAME, __version__
from ..config import Settings, app_data_dir
from .controller import Controller, setup_file_logging
from .i18n import set_language, tr
from .theme import app_icon, apply_dark_theme
from .tray import TrayMenu

_LOGGER = logging.getLogger(__name__)


def _system_language() -> str:
    """The Windows display language (es-MX, en-US...), not the regional format."""
    languages = QLocale.system().uiLanguages()
    return languages[0] if languages else QLocale.system().name()


def _instance_name() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # pylint: disable=broad-except
        user = "user"
    return f"AirCast-{user}"


class AirCastApp:
    def __init__(self, app: QApplication, open_window: bool = False) -> None:
        self.app = app
        self.settings = Settings.load()
        set_language(self.settings.language or _system_language())
        self.controller = Controller(self.settings)
        self.window = None
        self.tray: Optional[TrayMenu] = None
        self.server: Optional[QLocalServer] = None
        self._quitting = False
        self.listen()  # before building the UI, so a second launch always finds us
        app.aboutToQuit.connect(self._shutdown)  # also covers Windows logoff/shutdown

        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = TrayMenu(self.controller, self.open_window, self.quit)
            self.tray.show()
            if not self.settings.tray_hint_shown:
                QTimer.singleShot(1500, self._first_run_hint)
        else:  # no tray (very rare): a normal window app, closing the window quits
            _LOGGER.warning("No system tray available; running as a normal window")
            open_window = True

        self.controller.detect_capabilities()
        self.controller.check_virtual_screen()
        self.controller.refresh_devices()
        if open_window:
            self.open_window()

    # -- single instance ---------------------------------------------------

    def listen(self) -> None:
        self.server = QLocalServer()
        QLocalServer.removeServer(_instance_name())  # stale socket after a crash
        self.server.listen(_instance_name())
        self.server.newConnection.connect(self._second_launch)

    def _second_launch(self) -> None:
        conn = self.server.nextPendingConnection() if self.server else None
        if conn is None:
            return
        conn.waitForReadyRead(500)
        request = bytes(conn.readAll()).decode(errors="ignore").strip()
        conn.disconnectFromServer()
        if self._quitting:
            return
        if request == "open" or self.tray is None:
            self.open_window()
        else:
            self.tray.message(APP_NAME, tr("already_running"))

    # -- window ------------------------------------------------------------

    def open_window(self) -> None:
        if self.window is None:
            from .window import MainWindow

            self.window = MainWindow(
                self.controller,
                on_hidden=self._window_hidden,
                on_close=None if self.tray else self.quit,
            )
        self.window.bring_up()

    def _window_hidden(self) -> None:
        if self.tray and not self.settings.hide_hint_shown:
            self.tray.message(APP_NAME, tr("hidden_hint"))
            self.controller.update_settings(hide_hint_shown=True)

    def _first_run_hint(self) -> None:
        if self.tray:
            self.tray.message(APP_NAME, tr("tray_hint"))
        self.controller.update_settings(tray_hint_shown=True)

    def quit(self) -> None:
        try:
            self._shutdown()
        finally:
            self.app.quit()

    def _shutdown(self) -> None:
        """Stop casting and release everything; safe to call more than once."""
        if self._quitting:
            return
        self._quitting = True
        if self.server:  # first, so a launch from now on starts a fresh copy
            self.server.close()
        try:
            if self.tray:
                self.tray.hide()
            if self.window is not None:
                self.window.hide()
        finally:
            self.controller.shutdown()


def _notify_running_instance(message: str) -> bool:
    sock = QLocalSocket()
    sock.connectToServer(_instance_name())
    if not sock.waitForConnected(500):
        return False
    sock.write(message.encode())
    sock.flush()
    sock.waitForBytesWritten(500)
    sock.disconnectFromServer()
    return True


def run_gui(open_window: bool = False) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)
    apply_dark_theme(app)

    # One copy per user. The lock decides who runs; the local socket is how a second
    # launch asks the running copy to show itself.
    request = "open" if open_window else "hello"
    lock = QLockFile(os.path.join(app_data_dir(), "aircast.lock"))
    lock.setStaleLockTime(0)  # never expire by age; a crashed owner is detected by PID
    if not lock.tryLock(0):
        for _ in range(20):  # the running copy may still be starting, or be quitting
            if _notify_running_instance(request):
                return 0  # it shows its window or an "already running" balloon
            if lock.tryLock(250):
                break  # it has just quit: become the running copy
        else:
            return 0

    log_path = setup_file_logging()
    _LOGGER.info("%s %s starting (log: %s)", APP_NAME, __version__, log_path)
    aircast = AirCastApp(app, open_window=open_window)  # noqa: F841  (keeps the app alive)
    try:
        return app.exec()
    finally:
        _LOGGER.info("Bye")
        lock.unlock()
