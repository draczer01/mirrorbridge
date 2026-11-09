"""Updates in the app: a check a minute after it starts and once a day after that, a tray item
when a newer MirrorBridge is out, and its download, check and install (mirrorbridge.update)."""

from __future__ import annotations

import logging
import time
from typing import Optional

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox

from .. import APP_NAME, __version__, update
from .i18n import tr

_LOGGER = logging.getLogger(__name__)

FIRST_CHECK_MS = 60_000  # after starting: the network and the TVs come first
TICK_MS = 3600_000  # how often it looks whether a day has gone by since the last check


class Updates(QObject):
    changed = Signal()  # available, busy or progress changed: the tray shows it again

    def __init__(self, controller, quit_app) -> None:
        super().__init__()
        self.c = controller
        self.quit_app = quit_app
        self.available: Optional[update.Release] = None
        self.busy = ""  # "checking" | "downloading" | ""
        self.progress = 0  # percent, while downloading
        self._cancel = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(TICK_MS)
        QTimer.singleShot(FIRST_CHECK_MS, self._tick)
        try:
            update.clean()
        except OSError:
            pass

    # -- checking ----------------------------------------------------------

    def _tick(self) -> None:
        s = self.c.settings
        if s.check_updates and time.time() - s.update_checked_at >= update.CHECK_EVERY:
            self.check(by_hand=False)

    def check(self, by_hand: bool = True) -> None:
        if self.busy:
            return
        self.busy = "checking"
        self.changed.emit()
        prereleases = self.c.settings.update_prereleases

        def done(release: Optional[update.Release]) -> None:
            self.busy = ""
            self.c.update_settings(update_checked_at=time.time())
            self.available = release
            self.changed.emit()
            if release is None:
                if by_hand:
                    self.c.notify.emit(APP_NAME, tr("update_none", version=__version__), False)
            elif by_hand or self.c.settings.update_notified != release.version:
                self.c.update_settings(update_notified=release.version)
                self.c.notify.emit(tr("update_title", version=release.version), tr("update_ready_hint"), False)

        def failed(exc: BaseException) -> None:
            self.busy = ""
            self.changed.emit()
            _LOGGER.info("Update check failed: %s", exc)
            if isinstance(exc, update.UpdateError) and not exc.retry:
                self.c.update_settings(update_checked_at=time.time())  # rate limit: wait a day
            if by_hand:
                self.c.notify.emit(APP_NAME, tr("update_check_failed", reason=str(exc)), True)

        self.c.run_blocking(update.check, __version__, prereleases, on_ok=done, on_err=failed)

    # -- installing --------------------------------------------------------

    def install(self) -> None:
        release = self.available
        if release is None or self.busy:
            return
        if not update.can_install():
            QDesktopServices.openUrl(QUrl(release.page))
            return
        if self.c.is_active:
            box = QMessageBox(QMessageBox.Question, tr("update_title", version=release.version),
                              tr("update_stops_session"), parent=self.c.dialog_parent())
            yes = box.addButton(tr("update_now"), QMessageBox.AcceptRole)
            box.addButton(tr("cancel"), QMessageBox.RejectRole)
            self.c.bring_to_front(box)
            box.exec()
            if box.clickedButton() is not yes:
                return
        self.busy = "downloading"
        self.progress = 0
        self._cancel = False
        self.changed.emit()
        _LOGGER.info("Update: downloading %s %s", APP_NAME, release.version)

        def progress(have: int, size: int) -> None:  # on a worker thread
            percent = int(have * 100 / size) if size else 0
            if percent != self.progress:
                self.progress = percent
                self.c.bridge.call(self.changed.emit)

        def done(installer) -> None:
            self.busy = ""
            self.changed.emit()
            try:
                update.clean(keep=installer.name)
                update.start_installer(installer, relaunch=True)
            except OSError as ex:
                _LOGGER.warning("Update: the installer did not start: %s", ex)
                self.c.notify.emit(tr("update_title", version=release.version),
                                   tr("update_failed", reason=str(ex)), True)
                return
            self.quit_app()  # the installer waits for this copy to quit, then starts the new one

        def failed(exc: BaseException) -> None:
            self.busy = ""
            self.changed.emit()
            _LOGGER.warning("Update: %s", exc)
            self.c.notify.emit(tr("update_title", version=release.version), tr("update_failed", reason=str(exc)), True)

        self.c.run_blocking(lambda: update.download(release, progress=progress, cancelled=lambda: self._cancel),
                            on_ok=done, on_err=failed)

    def stop(self) -> None:
        self._cancel = True
        self._timer.stop()

    def open_notes(self) -> None:
        QDesktopServices.openUrl(QUrl(self.available.page if self.available else update.RELEASES_PAGE))
