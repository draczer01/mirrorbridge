"""Application state shared by the tray menu and the main window.

Everything that touches the network or AirPlay runs on a background asyncio loop;
results come back to the Qt thread as signals, so both views stay in sync.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import logging.handlers
import os
import threading
import time
from collections import deque
from typing import Any, Callable, Coroutine, Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit, QMessageBox, QWidget

from .. import airplay_patch, ffmpeg, probe, vdisplay
from ..config import Settings, app_data_dir
from ..devices import Device, DeviceManager
from ..session import CastSession, State
from . import i18n, winintegration
from .extend import ExtendScreenSetup, error_text
from .i18n import say, state_text, tr

_LOGGER = logging.getLogger(__name__)

LOG_FILE_NAME = "mirrorbridge.log"


class UiBridge(QObject):
    """Marshal callables from any thread onto the Qt main thread."""

    invoke = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.invoke.connect(lambda fn: fn(), Qt.QueuedConnection)

    def call(self, fn: Callable[[], None]) -> None:
        self.invoke.emit(fn)


class AsyncRunner:
    """A dedicated asyncio loop thread; results are delivered back on the Qt thread."""

    def __init__(self, bridge: UiBridge) -> None:
        self.bridge = bridge
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="mirrorbridge-async", daemon=True)
        self.thread.start()

    def submit(
        self,
        coro: Coroutine,
        on_ok: Callable[[Any], None] | None = None,
        on_err: Callable[[BaseException], None] | None = None,
    ) -> concurrent.futures.Future:
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def done(f: concurrent.futures.Future) -> None:
            if f.cancelled():
                if on_err:
                    self.bridge.call(lambda: on_err(concurrent.futures.CancelledError()))
                return
            exc = f.exception()
            if exc is not None:
                if on_err:
                    self.bridge.call(lambda: on_err(exc))
                else:
                    _LOGGER.error("Background task failed: %s", exc)
            elif on_ok:
                self.bridge.call(lambda: on_ok(f.result()))

        fut.add_done_callback(done)
        return fut

    def shutdown(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


class _SignalLogHandler(logging.Handler):
    def __init__(self, emit_line: Callable[[str], None]) -> None:
        super().__init__(logging.INFO)
        self.emit_line = emit_line
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.emit_line(self.format(record))
        except RuntimeError:  # Qt object already deleted at shutdown
            pass


def setup_file_logging() -> str:
    """Log to %APPDATA%\\MirrorBridge\\mirrorbridge.log (the windowed exe has no console)."""
    path = os.path.join(app_data_dir(), LOG_FILE_NAME)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    for noisy in ("pyatv", "zeroconf", "aiohttp", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.INFO)
    return path


class Controller(QObject):
    devices_changed = Signal()
    scanning_changed = Signal(bool)
    cast_state_changed = Signal(object, object)  # State, what the session said (a str or Text) or the error
    activity_changed = Signal()  # started/stopped casting or file playback
    encoders_detected = Signal(list)
    settings_changed = Signal()
    log_line = Signal(str)
    notify = Signal(str, str, bool)  # title, message, is_error
    language_changed = Signal()  # i18n speaks another language: show every text again

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.bridge = UiBridge()
        self.runner = AsyncRunner(self.bridge)
        self.manager = DeviceManager(self.runner.loop)
        self.devices: list[Device] = []
        self.scanning = False
        self.last_scan = 0.0
        self.encoders: list[str] = []
        self._probing = False
        self._probed_at = 0.0
        self._ffmpeg_warned = False
        self.ffmpeg_path: Optional[str] = None
        self.session: Optional[CastSession] = None
        self.session_future: Optional[concurrent.futures.Future] = None
        self.file_future: Optional[concurrent.futures.Future] = None
        self.probe_future: Optional[concurrent.futures.Future] = None  # More options > Test this TV
        self.probe_step = ""
        self.cast_state = State.IDLE
        self.cast_message = ""
        self.casting_to = ""
        self.pairing = False
        self.vdd_status: Optional[vdisplay.DriverStatus] = None  # last known, for the menu
        self.extend = ExtendScreenSetup(self)
        # The main window sets this so dialogs are parented to it while it is open.
        self.dialog_parent: Callable[[], Optional[QWidget]] = lambda: None

        # Recent lines, so a window opened later still shows what already happened.
        self._recent_log: deque[str] = deque(maxlen=300)
        self._recent_lock = threading.Lock()
        self._log_handler = _SignalLogHandler(self._log)
        logging.getLogger().addHandler(self._log_handler)

    def _log(self, line: str) -> None:  # called from any thread
        with self._recent_lock:
            self._recent_log.append(line)
        self.log_line.emit(line)

    @property
    def recent_log(self) -> list[str]:
        with self._recent_lock:
            return list(self._recent_log)

    # -- state -------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        return self.session_future is not None or self.file_future is not None or self.probe_future is not None

    def status(self) -> tuple[str, str]:
        """(icon state, one-line description) shared by the tray and the window."""
        if self.probe_future is not None:
            return "busy", tr("status_probing", device=self.casting_to, step=self.probe_step)
        if self.file_future is not None:
            return "active", tr("status_playing_file", device=self.casting_to)
        if self.session_future is not None:
            if self.cast_state == State.CASTING:
                return "active", tr("status_casting", device=self.casting_to)
            return "busy", tr("status_busy", state=state_text(self.cast_state))
        return "idle", tr("status_idle")

    @property
    def selected_device(self) -> Optional[Device]:
        for dev in self.devices:
            if dev.identifier == self.settings.last_device_id:
                return dev
        return None

    def select_device(self, identifier: str) -> None:
        self.settings.last_device_id = identifier
        self.save_settings()
        self.devices_changed.emit()

    def set_language(self, code: str) -> None:
        """Show the menu, the window, and every dialog and notification from now on in `code`
        (an i18n.LANGUAGES code), and save the choice."""
        if code == i18n.language():
            return
        code = i18n.set_language(code)
        _LOGGER.info("Language: %s", i18n.LANGUAGES[code])
        self.update_settings(language=code)
        self.language_changed.emit()

    def update_settings(self, **changes: Any) -> None:
        for key, value in changes.items():
            setattr(self.settings, key, value)
        self.save_settings()

    def save_settings(self) -> None:
        try:
            self.settings.save()
        except OSError as ex:
            _LOGGER.warning("Could not save settings: %s", ex)
        self.settings_changed.emit()

    def run_blocking(
        self,
        fn: Callable[..., Any],
        *args: Any,
        on_ok: Callable[[Any], None] | None = None,
        on_err: Callable[[BaseException], None] | None = None,
    ) -> concurrent.futures.Future:
        """Run a blocking call (Win32, disk, network) on the background loop's executor, so the
        menu never freezes; on_ok / on_err run on the Qt thread."""

        async def call():
            return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

        return self.runner.submit(call(), on_ok, on_err)

    # -- startup -----------------------------------------------------------

    def detect_capabilities(self) -> None:
        if self._probing:
            return
        self._probing = True

        def probe():
            ff = ffmpeg.find_ffmpeg(self.settings.ffmpeg_path)
            return ff, ffmpeg.available_encoders(ff)

        async def probe_async():
            return await asyncio.get_running_loop().run_in_executor(None, probe)

        def done(result):
            self._probing = False
            self._probed_at = time.monotonic()
            self.ffmpeg_path, self.encoders = result
            _LOGGER.info("FFmpeg: %s", self.ffmpeg_path)
            _LOGGER.info("Working encoders: %s", ", ".join(self.encoders) or "none")
            self.encoders_detected.emit(self.encoders)

        def failed(exc):
            self._probing = False
            self._probed_at = time.monotonic()
            _LOGGER.error("%s", exc)
            if not self._ffmpeg_warned:  # once; the menu re-probes later on its own
                self._ffmpeg_warned = True
                self.notify.emit(tr("ffmpeg_missing"), error_text(exc), True)

        self.runner.submit(probe_async(), done, failed)

    def check_virtual_screen(self) -> None:
        """Remove a virtual screen a MirrorBridge that crashed left behind, and learn whether the
        driver is installed (the menu offers to remove it only then)."""
        if not vdisplay.available():
            return

        def check() -> vdisplay.DriverStatus:
            vdisplay.recover_stale()
            return vdisplay.backend().status()

        def done(status: vdisplay.DriverStatus) -> None:
            self.vdd_status = status
            _LOGGER.debug("Virtual screen driver: %s", status.value)

        self.run_blocking(check, on_ok=done, on_err=lambda exc: _LOGGER.debug("Virtual screen check failed: %s", exc))

    def refresh_encoders_if_stale(self, max_age: float = 120) -> None:
        """Re-test GPU encoders that failed earlier (busy at logon, driver update...).
        Only failures are re-run; encoders that worked once are remembered."""
        missing = len(self.encoders) < len(ffmpeg.ENCODER_PREFERENCE)
        if missing and not self.is_active and time.monotonic() - self._probed_at > max_age:
            self.detect_capabilities()

    # -- devices -----------------------------------------------------------

    def refresh_devices(self, *_args) -> None:
        if self.scanning:
            return
        self.scanning = True
        self.scanning_changed.emit(True)
        hosts = list(self.settings.manual_hosts)

        async def scan():
            found = await self.manager.scan(timeout=5)
            if hosts:
                extra = await self.manager.scan(timeout=3, hosts=hosts)
                known = {d.identifier for d in found}
                found += [d for d in extra if d.identifier not in known]
            return found

        def done(devices):
            self.devices = devices
            self.scanning = False
            self.last_scan = time.monotonic()
            usable = [d for d in devices if d.can_cast_video]
            if self.selected_device is None and len(usable) == 1:
                self.settings.last_device_id = usable[0].identifier
                self.save_settings()
            _LOGGER.info(tr("found_n", n=len(devices)) if devices else tr("found_none"))
            self.scanning_changed.emit(False)
            self.devices_changed.emit()

        def failed(exc):
            self.scanning = False
            self.scanning_changed.emit(False)
            _LOGGER.error(tr("scan_failed", error=exc))

        self.runner.submit(scan(), done, failed)

    def refresh_if_stale(self, max_age: float = 60) -> None:
        if not self.is_active and time.monotonic() - self.last_scan > max_age:
            self.refresh_devices()

    def add_host(self, ip: str) -> None:
        ip = ip.strip()
        if ip and ip not in self.settings.manual_hosts:
            self.settings.manual_hosts.append(ip)
            self.save_settings()
        self.refresh_devices()

    def ask_add_host(self, *_args) -> None:
        ip = self._ask_text(tr("add_ip"), tr("add_ip_prompt"))
        if ip:
            self.add_host(ip)

    # -- dialogs -----------------------------------------------------------

    def _parent(self) -> Optional[QWidget]:
        return self.dialog_parent()

    @staticmethod
    def bring_to_front(dialog: QWidget) -> None:
        # Modality must be set before the first show(); exec() cannot change it afterwards.
        dialog.setWindowModality(Qt.ApplicationModal)
        dialog.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _ask_text(self, title: str, label: str, password: bool = False) -> Optional[str]:
        dialog = QInputDialog(self._parent())
        dialog.setWindowTitle(title)
        dialog.setLabelText(label)
        dialog.setTextEchoMode(QLineEdit.Password if password else QLineEdit.Normal)
        dialog.setOkButtonText(tr("ok"))  # Qt's own words would be English
        dialog.setCancelButtonText(tr("cancel"))
        self.bring_to_front(dialog)
        ok = dialog.exec()
        return dialog.textValue().strip() if ok else None

    def _ask_pin(self, device: Device):
        """Return the PIN callback DeviceManager.pair awaits (runs on the asyncio loop)."""

        async def ask(device_provides_pin: bool, our_pin: Optional[str]) -> Optional[str]:
            fut = self.runner.loop.create_future()

            def show():
                if device_provides_pin:
                    value = self._ask_text(tr("pair_title", device=device.name), tr("pair_prompt"))
                else:
                    box = QMessageBox(QMessageBox.Information, tr("pair_title", device=device.name),
                                      tr("pair_show_pin", pin=our_pin), parent=self._parent())
                    box.addButton(tr("ok"), QMessageBox.AcceptRole)
                    self.bring_to_front(box)
                    box.exec()
                    value = our_pin
                self.runner.loop.call_soon_threadsafe(fut.set_result, value)

            self.bridge.call(show)
            return await fut

        return ask

    def show_error(self, title: str, message: str) -> None:
        box = QMessageBox(QMessageBox.Warning, title, message, parent=self._parent())
        box.addButton(tr("ok"), QMessageBox.AcceptRole)
        self.bring_to_front(box)
        box.exec()

    # -- pairing -----------------------------------------------------------

    def pair(self, device: Optional[Device] = None, then: Optional[Callable[[], None]] = None) -> None:
        """Pair (and set the AirPlay password if the receiver has one), then run `then`."""
        device = device or self.selected_device
        if device is None or self.pairing:
            return
        self.pairing = True
        try:
            self._pair(device, then)
        except Exception:
            self.pairing = False
            raise

    def _pair(self, device: Device, then: Optional[Callable[[], None]]) -> None:
        password = None
        if device.needs_password:
            password = self._ask_text(tr("password_title"), tr("password_prompt", device=device.name), True)
            if password is None:
                self.pairing = False
                return
        explicit = then is None  # the Pair menu item always pairs; casting only when required
        _LOGGER.info(tr("pairing", device=device.name))

        async def run():
            if password is not None:
                await self.manager.set_password(device, password)
            if explicit or device.needs_pairing:
                await self.manager.pair(device, self._ask_pin(device))

        def done(_):
            self.pairing = False
            _LOGGER.info(tr("paired", device=device.name))
            self.devices_changed.emit()
            if then:
                then()
            else:
                self.notify.emit(tr("paired", device=device.name), "", False)

        def failed(exc):
            self.pairing = False
            _LOGGER.error("%s: %s", tr("pair_failed"), exc)
            self.devices_changed.emit()
            self.show_error(tr("pair_failed"), tr("pair_failed_hint", error=error_text(exc)))

        self.runner.submit(run(), done, failed)

    def forget(self, *_args) -> None:
        device = self.selected_device
        if device:
            self.runner.submit(self.manager.forget(device), lambda _: self.devices_changed.emit())

    # -- casting -----------------------------------------------------------

    def toggle_cast(self, *_args) -> None:
        if self.is_active:
            self.stop_cast()
        else:
            self.start_cast()

    @property
    def virtual_device(self) -> str:
        """\\\\.\\DISPLAYn of the virtual screen the current cast added, if any."""
        return self.session.virtual_device if self.session else ""

    def choose_extend_screen(self, *_args) -> None:
        self.extend.choose()

    def start_cast(self, *_args) -> None:
        device = self.selected_device
        if self.is_active or device is None or not device.can_cast_video:
            return
        if device.needs_pairing or device.needs_password:
            self.pair(device, then=self.start_cast)
            return

        def on_status(state: State, message: str) -> None:
            if state == State.FAILED and session.error is not None:
                message = session.error  # the window explains it in the user's language
            self.bridge.call(lambda: self._on_cast_state(state, message, device))

        self.casting_to = device.name
        self.cast_state, self.cast_message = State.STARTING, ""
        self.session = session = CastSession(self.manager, device, self.settings, on_status)

        def finished(_=None):
            self.session_future = None
            self.session = None
            self.casting_to = ""
            self.activity_changed.emit()

        def failed(exc):
            finished()
            if isinstance(exc, concurrent.futures.CancelledError):
                return
            if isinstance(exc, vdisplay.VddError) and exc.status is not None:
                self.extend.handle_status(exc.status)  # driver missing or broken: offer the fix
            else:
                self.notify.emit(tr("cast_failed"), error_text(exc), True)

        self.session_future = self.runner.submit(self.session.run(), finished, failed)
        self.activity_changed.emit()

    def _on_cast_state(self, state: State, message: Any, device: Device) -> None:
        self.cast_state = state
        self.cast_message = message
        self.cast_state_changed.emit(state, message)
        if state == State.CASTING:
            tip = ""
            if self.virtual_device and not self.settings.extend_tip_shown:
                tip = tr("extend_tip")  # once: where the new screen is
                self.update_settings(extend_tip_shown=True)
            self.notify.emit(tr("cast_started", device=device.name), tip, False)
        elif state == State.STOPPED:
            self.notify.emit(tr("cast_stopped"), "", False)

    def stop_cast(self, *_args) -> None:
        if self.session:
            self.runner.submit(self.session.stop())
        if self.file_future:
            self.file_future.cancel()
        if self.probe_future:
            self.probe_future.cancel()

    def play_file(self, path: str) -> None:
        device = self.selected_device
        if self.is_active or device is None or not path:
            return
        if device.needs_pairing or device.needs_password:
            self.pair(device, then=lambda: self.play_file(path))
            return

        async def play():
            airplay_patch.apply()
            atv = await self.manager.connect(device)
            try:
                cast = airplay_patch.CastContext(apple=device.is_apple, live=False, password=device.requires_password,
                                                 device=device.identifier, features=device.features)
                await airplay_patch.play_url(atv, path, cast)
            finally:
                atv.close()

        def finished(_=None):
            self.file_future = None
            self.casting_to = ""
            self.activity_changed.emit()
            self.notify.emit(tr("playback_done"), "", False)

        def failed(exc):
            self.file_future = None
            self.casting_to = ""
            self.activity_changed.emit()
            if not isinstance(exc, concurrent.futures.CancelledError):
                self.notify.emit(tr("playback_failed"), error_text(exc), True)

        _LOGGER.info(tr("playing_file", file=os.path.basename(path), device=device.name))
        self.casting_to = device.name
        self.file_future = self.runner.submit(play(), finished, failed)
        self.activity_changed.emit()

    # -- testing a TV --------------------------------------------------------

    def test_tv(self, *_args) -> None:
        """More options > Test this TV: the probe's three tests, then a dialog with what they found."""
        device = self.selected_device
        if self.is_active or device is None or not device.can_cast_video:
            return
        if device.needs_pairing or device.needs_password:
            self.pair(device, then=self.test_tv)
            return
        box = QMessageBox(QMessageBox.Question, tr("probe_title", device=device.name),
                          tr("probe_intro", device=device.name), parent=self._parent())
        start = box.addButton(tr("probe_start"), QMessageBox.AcceptRole)
        box.addButton(tr("cancel"), QMessageBox.RejectRole)
        self.bring_to_front(box)
        box.exec()
        if box.clickedButton() is not start:
            return

        def progress(number: int, total: int, _name: str) -> None:  # on the asyncio loop
            self.bridge.call(lambda: self._on_probe_step(tr("probe_step", number=number, total=total)))

        def finished(report: probe.ProbeReport) -> None:
            ended()
            self.show_probe_report(report)

        def failed(exc: BaseException) -> None:
            ended()
            if not isinstance(exc, concurrent.futures.CancelledError):
                _LOGGER.error("%s: %s", tr("probe_failed_title"), exc)
                self.show_error(tr("probe_failed_title"), error_text(exc))

        def ended() -> None:
            self.probe_future = None
            self.probe_step = ""
            self.casting_to = ""
            self.activity_changed.emit()

        self.casting_to = device.name
        self.probe_step = tr("probe_step", number=1, total=len(probe.TESTS))
        self.probe_future = self.runner.submit(probe.run(self.manager, device, self.settings, progress=progress),
                                               finished, failed)
        self.activity_changed.emit()

    def _on_probe_step(self, step: str) -> None:
        if self.probe_future is not None:
            self.probe_step = step
            self.activity_changed.emit()

    def probe_report_box(self, report: probe.ProbeReport) -> QMessageBox:
        """What Test this TV found out: the verdict, each test's result, and the report's file."""
        box = QMessageBox(QMessageBox.Information if report.worked else QMessageBox.Warning,
                          tr("probe_done", device=report.device), say(report.conclusion()), parent=self._parent())
        box.setInformativeText("\n".join([*map(say, report.lines()), "", say(report.where())]))
        folder = box.addButton(tr("log_folder"), QMessageBox.ActionRole)
        box.addButton(tr("ok"), QMessageBox.AcceptRole)
        report_file = report.report_file
        folder.clicked.connect(lambda: winintegration.open_folder(str(report_file.parent if report_file
                                                                      else app_data_dir())))
        return box

    def show_probe_report(self, report: probe.ProbeReport) -> None:
        box = self.probe_report_box(report)
        self.bring_to_front(box)
        box.exec()

    # -- shutdown ----------------------------------------------------------

    def shutdown(self, timeout: float = 8) -> None:
        """Stop any cast (so FFmpeg does not linger) and stop the background loop."""
        session, session_future, file_future = self.session, self.session_future, self.file_future
        probe_future = self.probe_future
        try:
            if session is not None:
                try:
                    asyncio.run_coroutine_threadsafe(session.stop(), self.runner.loop).result(2)
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.debug("Stop request failed", exc_info=True)
                # Wait on the session itself: its future can report done before cleanup
                # (stopping FFmpeg, closing the HTTP server) has finished.
                deadline = time.monotonic() + timeout
                while not session.finished.is_set() and time.monotonic() < deadline:
                    QApplication.processEvents()
                    time.sleep(0.05)
                if not session.finished.is_set():
                    _LOGGER.warning("Cast did not stop within %.0fs", timeout)
            elif session_future is not None:
                session_future.cancel()
            if file_future is not None:
                file_future.cancel()
            if probe_future is not None:
                probe_future.cancel()
        finally:
            logging.getLogger().removeHandler(self._log_handler)
            self.runner.shutdown()
