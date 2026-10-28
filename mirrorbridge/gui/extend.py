"""The "Extend screen" choice: check the virtual display driver, and install it with consent.

Driver checks, the download and the installer run on the controller's background loop; only
the dialogs run on the Qt thread. Everything is a small dialog or a tray notification, so it
works the same from the tray menu and from the window.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox, QProgressDialog

from .. import vdisplay
from ..text import Text
from ..vdisplay import DriverStatus, VddError
from . import winintegration
from .i18n import say, tr

if TYPE_CHECKING:
    from .controller import Controller

_LOGGER = logging.getLogger(__name__)

# Size choices: (menu label, settings value). The pixel size is added from vdisplay.EXTEND_MODES.
EXTEND_SIZES = [("1080p", "1080p"), ("720p", "720p"), ("4K", "4k")]

_ERROR_KEYS = {"add_failed": "vdd_add_failed", "not_attached": "vdd_not_attached", "lost": "vdd_lost",
               "mismatch": "vdd_mismatch", "unsupported": "vdd_unsupported"}
STATUS_KEYS = {
    DriverStatus.NOT_INSTALLED: "vdd_st_missing",
    DriverStatus.DISABLED: "vdd_st_disabled",
    DriverStatus.RESTART_REQUIRED: "vdd_st_restart",
    DriverStatus.DRIVER_ERROR: "vdd_st_broken",
    DriverStatus.UNKNOWN_PROBLEM: "vdd_st_broken",
    DriverStatus.INACCESSIBLE: "vdd_st_broken",
}


def size_name(mode: str) -> str:
    return {value: label for label, value in EXTEND_SIZES}.get(mode, mode)


def size_label(mode: str) -> str:
    width, height, _hz = vdisplay.EXTEND_MODES[mode]
    return f"{size_name(mode)} ({width}x{height})"


def error_text(exc: BaseException) -> str:
    """Why something failed, in the user's language: a virtual screen problem by its code, any
    other error whose message is a Text by its key. Other errors (Windows', pyatv's) as they are."""
    if isinstance(exc, VddError):
        if exc.status in STATUS_KEYS:
            return tr(STATUS_KEYS[exc.status])
        if exc.code in _ERROR_KEYS:
            return tr(_ERROR_KEYS[exc.code])
    if exc.args and isinstance(exc.args[0], Text):
        return say(exc.args[0])
    return str(exc)


class ExtendScreenSetup:
    """Turns Extend screen on once the driver works, installing the driver first if the user agrees."""

    def __init__(self, controller: "Controller") -> None:
        self.c = controller
        self.busy = False  # a check, a dialog or an install is under way
        self._from_cast = False  # this check started when a cast found the driver not working

    # -- entry points --------------------------------------------------------

    def choose(self, *_args) -> None:
        """The user picked Extend screen, in the menu or in the window."""
        if self.busy:
            return  # the check under way ends by showing the saved choice again
        if not vdisplay.available():
            self.c.settings_changed.emit()  # puts the window's radio buttons back
            return
        self.busy = True
        self.c.run_blocking(vdisplay.backend().status, on_ok=self._checked, on_err=self._check_failed)

    def handle_status(self, status: DriverStatus) -> None:
        """A cast found the driver missing or broken: the same dialogs as picking Extend screen."""
        if not self.busy:
            self.busy = True
            self._from_cast = True
            self._checked(status)

    def remove_driver(self, *_args) -> None:
        """Windows' own uninstaller is in Settings > Apps; the installer registers it there."""
        QDesktopServices.openUrl(QUrl("ms-settings:appsfeatures"))
        self.c.notify.emit(tr("vdd_remove"), tr("vdd_remove_hint"), False)

    # -- driver status ---------------------------------------------------------

    def _check_failed(self, exc: BaseException) -> None:
        _LOGGER.error("Virtual screen driver check failed: %s", exc)
        self._finish()
        self.c.notify.emit(tr("extend_screen"), f"{tr('vdd_check_failed')}\n{error_text(exc)}", True)

    def _checked(self, status: DriverStatus) -> None:
        self.c.vdd_status = status
        _LOGGER.info("Virtual screen driver: %s", status.value)
        if status is DriverStatus.OK:
            self._enable()
        elif status is DriverStatus.NOT_INSTALLED:
            if self._consent_and_install():
                return
        elif status is DriverStatus.RESTART_REQUIRED:
            self._enable()  # works after the restart
            self._message(QMessageBox.Information, tr("vdd_st_restart"))
        elif status is DriverStatus.DISABLED:
            if self._disabled(tr("vdd_st_disabled")):
                return
        elif self._broken(tr("vdd_st_broken")):
            return
        self._finish()

    def _enable(self) -> None:
        self.c.update_settings(extend_screen=True, window_title="")

    def _finish(self) -> None:
        if self._from_cast and self.c.vdd_status is not DriverStatus.OK and self.c.settings.extend_screen:
            # Otherwise every Start would end in the same dialog and cast nothing.
            self.c.update_settings(extend_screen=False)
            self.c.notify.emit(tr("extend_screen"), tr("vdd_extend_off"), False)
        self._from_cast = False
        self.busy = False
        self.c.settings_changed.emit()  # the window's radio buttons follow the saved choice

    # -- dialogs ---------------------------------------------------------------

    def _box(self, icon, text: str, info: str = "") -> QMessageBox:
        box = QMessageBox(icon, tr("extend_screen"), text, parent=self.c.dialog_parent())
        if info:
            box.setInformativeText(info)
        return box

    def _message(self, icon, text: str, info: str = "") -> None:
        box = self._box(icon, text, info)
        self.c.bring_to_front(box)
        box.exec()

    def _consent(self) -> bool:
        box = QMessageBox(QMessageBox.Question, tr("vdd_install_title"), tr("vdd_install_text"),
                          parent=self.c.dialog_parent())
        box.setInformativeText(f'<a href="{vdisplay.PARSEC_TERMS_URL}">{tr("vdd_terms")}</a>')  # opens in the browser
        install = box.addButton(tr("vdd_install"), QMessageBox.AcceptRole)
        box.addButton(tr("vdd_not_now"), QMessageBox.RejectRole)
        box.setDefaultButton(install)
        self.c.bring_to_front(box)
        box.exec()
        return box.clickedButton() is install

    def _consent_and_install(self) -> bool:
        """Install, or reinstall, only after the user agreed to Parsec's download and terms.
        True if the install started."""
        if not self._consent():
            return False
        self._install()
        return True

    def _broken(self, text: str, info: str = "") -> bool:
        """The driver is there but does not work. True if a reinstall started."""
        box = self._box(QMessageBox.Warning, text, info)
        reinstall = box.addButton(tr("vdd_reinstall"), QMessageBox.AcceptRole)
        box.addButton(tr("close"), QMessageBox.RejectRole)
        self.c.bring_to_front(box)
        box.exec()
        return box.clickedButton() is reinstall and self._consent_and_install()

    def _disabled(self, text: str) -> bool:
        """The adapter is turned off in Device Manager. True if a reinstall started."""
        box = self._box(QMessageBox.Warning, text, tr("vdd_enable_hint"))
        devmgmt = box.addButton(tr("vdd_open_devmgmt"), QMessageBox.ActionRole)
        reinstall = box.addButton(tr("vdd_reinstall"), QMessageBox.ActionRole)
        box.addButton(tr("close"), QMessageBox.RejectRole)
        self.c.bring_to_front(box)
        box.exec()
        if box.clickedButton() is devmgmt:
            try:
                winintegration.open_device_manager()
            except OSError as ex:
                self.c.notify.emit(tr("vdd_open_devmgmt"), str(ex), True)
        elif box.clickedButton() is reinstall:
            return self._consent_and_install()
        return False

    def _download_failed(self, exc: VddError) -> None:
        # A file that did not match is never offered again; a network error is only detail.
        mismatch = exc.code == "mismatch"
        if mismatch:
            box = self._box(QMessageBox.Warning, tr("vdd_mismatch"))
        else:
            box = self._box(QMessageBox.Warning, tr("vdd_download_failed"), exc.detail)
        winget = box.addButton(tr("vdd_winget"), QMessageBox.ActionRole)
        manual = None if mismatch else box.addButton(tr("vdd_manual"), QMessageBox.ActionRole)
        box.addButton(tr("close"), QMessageBox.RejectRole)
        self.c.bring_to_front(box)
        box.exec()
        if box.clickedButton() is winget:
            missing = "vdd_winget_missing" if manual is not None else "vdd_winget_missing_only"
            self.c.run_blocking(
                vdisplay.install_with_winget,
                on_ok=lambda _: self.c.notify.emit(tr("vdd_winget"), tr("vdd_choose_again"), False),
                on_err=lambda _exc: self.c.notify.emit(tr("vdd_winget"), tr(missing), True),
            )
        elif manual is not None and box.clickedButton() is manual:
            QDesktopServices.openUrl(QUrl(vdisplay.VDD_INSTALLER_URL))
            self.c.notify.emit(tr("vdd_manual"), tr("vdd_choose_again"), False)

    # -- install ---------------------------------------------------------------

    def _install(self) -> None:
        """Download, verify and run Parsec's installer (one UAC prompt), then wait for the driver."""
        c = self.c
        progress = QProgressDialog(tr("vdd_downloading", pct=0), "", 0, 100, c.dialog_parent())
        progress.setWindowTitle(tr("vdd_install_title"))
        progress.setCancelButton(None)  # the UAC prompt is the place to say no
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setMinimumDuration(0)
        c.bring_to_front(progress)
        hwnd = int(progress.winId())  # in front, so the UAC prompt comes up in front too

        def show(text: str, percent: Optional[int] = None) -> None:  # from the worker thread
            def apply() -> None:
                if percent is None:
                    progress.setRange(0, 0)  # busy
                else:
                    progress.setValue(percent)
                progress.setLabelText(text)

            c.bridge.call(apply)

        def downloaded(done: int, total: int) -> None:
            percent = min(100, done * 100 // total) if total else 0
            show(tr("vdd_downloading", pct=percent), percent)

        def job():
            try:
                installer = vdisplay.download_installer(progress=downloaded)
            except VddError as ex:
                return "download_failed", ex
            show(tr("vdd_asking"))
            if not vdisplay.run_installer_elevated(installer, hwnd, on_started=lambda: show(tr("vdd_installing"))):
                return "cancelled", None
            show(tr("vdd_finishing"))
            return "installed", vdisplay.wait_until_ready()

        def close_progress() -> None:
            progress.close()
            progress.deleteLater()

        def done(result) -> None:
            close_progress()
            outcome, value = result
            if outcome == "download_failed":
                _LOGGER.error("%s", value)
                self._download_failed(value)
            elif outcome == "cancelled":
                _LOGGER.info("Driver install cancelled at the UAC prompt")
                c.notify.emit(tr("vdd_install_title"), tr("vdd_uac_cancelled"), True)
            else:
                self._installed(value)
                return
            self._finish()

        def failed(exc: BaseException) -> None:
            close_progress()
            _LOGGER.error("Driver install failed: %s", exc)
            if isinstance(exc, VddError) and exc.code == "install":
                detail = tr("vdd_installer_error", error=exc.detail)  # the detail is Windows' own text
            else:
                detail = error_text(exc)
            self._message(QMessageBox.Warning, tr("vdd_install_failed"), detail)
            self._finish()

        c.run_blocking(job, on_ok=done, on_err=failed)

    def _installed(self, status: DriverStatus) -> None:
        self.c.vdd_status = status
        _LOGGER.info("Virtual screen driver after install: %s", status.value)
        if status is DriverStatus.OK:
            self._enable()
            self.c.notify.emit(tr("vdd_ready"), tr("vdd_ready_hint"), False)
        elif status is DriverStatus.RESTART_REQUIRED:
            self._enable()
            self._message(QMessageBox.Information, tr("vdd_restart_to_finish"))
        elif status is DriverStatus.DISABLED:
            if self._disabled(tr("vdd_st_disabled")):
                return
        elif self._broken(tr("vdd_no_answer")):
            return
        self._finish()
