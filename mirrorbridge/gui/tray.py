"""The tray icon and its menu: every feature is reachable from here."""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction, QActionGroup, QCursor, QFont
from PySide6.QtWidgets import QFileDialog, QMenu, QSystemTrayIcon

from .. import APP_NAME, ffmpeg, vdisplay
from ..config import app_data_dir
from . import winintegration
from .controller import Controller
from .extend import EXTEND_SIZES, error_text, size_label, size_name
from .i18n import LANGUAGES, language, language_menu_title, tr
from .theme import app_icon

_LOGGER = logging.getLogger(__name__)

RESOLUTIONS = [("720p", 720), ("1080p", 1080), ("1440p", 1440), ("4K", 2160), ("native", 0)]
FRAMERATES = [24, 30, 60]
LATENCIES = [("lat_low", "low"), ("lat_balanced", "balanced"), ("lat_smooth", "smooth")]
BITRATES = [4, 8, 12, 20, 30]


def display_label(mon: ffmpeg.Monitor) -> str:
    return tr("display_n", n=mon.index + 1, w=mon.width, h=mon.height) + (tr("primary") if mon.primary else "")


class TrayMenu(QObject):
    def __init__(self, controller: Controller, open_window: Callable[[], None], quit_app: Callable[[], None]) -> None:
        super().__init__()
        self.c = controller
        self.open_window = open_window
        self.quit_app = quit_app
        self.icons = {state: app_icon(state) for state in ("idle", "active", "busy")}
        self.tray = QSystemTrayIcon(self.icons["idle"])
        self.menu = QMenu()
        self._submenus: list[QMenu] = []
        self.menu.aboutToShow.connect(self._rebuild)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._activated)
        self.tray.messageClicked.connect(lambda: None)

        for signal in (self.c.devices_changed, self.c.activity_changed, self.c.settings_changed, self.c.encoders_detected):
            signal.connect(self._refresh_status)
        self.c.scanning_changed.connect(self._refresh_status)
        self.c.cast_state_changed.connect(self._refresh_status)
        self.c.language_changed.connect(self._rebuild)  # it is also rebuilt each time it opens
        self.c.language_changed.connect(self._refresh_status)
        self.c.notify.connect(self._notify)
        self._rebuild()
        self._refresh_status()

    def show(self) -> None:
        self.tray.show()

    def hide(self) -> None:
        self.tray.hide()

    def message(self, title: str, text: str = "", error: bool = False) -> None:
        icon = QSystemTrayIcon.Warning if error else QSystemTrayIcon.Information
        self.tray.showMessage(title, text or title, icon, 6000)

    # -- events ------------------------------------------------------------

    def _activated(self, reason) -> None:
        # Left click opens the same menu as right click; the window only opens via "Open".
        if reason == QSystemTrayIcon.Trigger:
            self.menu.popup(QCursor.pos())
            self.menu.activateWindow()

    def _notify(self, title: str, text: str, is_error: bool) -> None:
        self.message(title, text, is_error)

    def _refresh_status(self, *_args) -> None:
        state, tip = self.c.status()
        self.tray.setIcon(self.icons[state])
        self.tray.setToolTip(f"{APP_NAME} - {tip}")

    # -- menu --------------------------------------------------------------

    def _action(self, menu: QMenu, text: str, slot: Callable[..., Any] | None = None, *,
                checkable: bool = False, checked: bool = False, enabled: bool = True,
                group: QActionGroup | None = None) -> QAction:
        action = QAction(text, menu)
        action.setCheckable(checkable)
        if checkable:
            action.setChecked(checked)
        action.setEnabled(enabled)
        if group is not None:
            group.addAction(action)
        if slot is not None:
            action.triggered.connect(slot)
        menu.addAction(action)
        return action

    def _submenu(self, parent: QMenu, title: str) -> QMenu:
        sub = QMenu(title, parent)
        parent.addMenu(sub)
        self._submenus.append(sub)
        return sub

    def _rebuild(self) -> None:
        c, s = self.c, self.c.settings
        menu = self.menu
        menu.clear()
        for sub in self._submenus:  # menus are rebuilt on every open; free the old ones
            sub.deleteLater()
        self._submenus = []
        self.c.refresh_if_stale()
        self.c.refresh_encoders_if_stale()

        open_action = self._action(menu, tr("open"), lambda: self.open_window())
        font = QFont(open_action.font())
        font.setBold(True)
        open_action.setFont(font)
        menu.setDefaultAction(open_action)
        menu.addSeparator()

        self._action(menu, c.status()[1], enabled=False)

        device = c.selected_device
        if c.is_active:
            self._action(menu, tr("stop"), c.stop_cast)
        elif device and device.can_cast_video:
            self._action(menu, tr("start_to", device=device.name), c.start_cast)
        else:
            self._action(menu, tr("start_no_device"), enabled=False)
        menu.addSeparator()

        self._device_menu(self._submenu(menu, tr("device_menu") + (f": {device.name}" if device else "")))
        self._source_menu(self._submenu(menu, tr("source_menu")))
        self._quality_menu(self._submenu(menu, tr("quality_menu")))
        self._action(menu, tr("audio"), lambda on: c.update_settings(capture_audio=on),
                     checkable=True, checked=s.capture_audio)
        self._action(menu, tr("play_file"), self._choose_file, enabled=bool(device) and not c.is_active)
        menu.addSeparator()

        more = self._submenu(menu, tr("more"))
        self._action(more, tr("autostart"), self._toggle_autostart, checkable=True,
                     checked=winintegration.autostart_enabled())
        self._action(more, tr("desktop_shortcut"), self._create_shortcut)
        self._action(more, tr("log_folder"), lambda: winintegration.open_folder(str(app_data_dir())))
        if vdisplay.available():
            installed = c.vdd_status not in (None, vdisplay.DriverStatus.NOT_INSTALLED)
            self._action(more, tr("vdd_remove"), c.extend.remove_driver, enabled=installed and not c.is_active)
        self._language_menu(self._submenu(menu, language_menu_title()))
        menu.addSeparator()
        self._action(menu, tr("quit"), lambda: self.quit_app())

    def _device_menu(self, sub: QMenu) -> None:
        c = self.c
        selected = c.selected_device
        if c.scanning:
            self._action(sub, tr("searching"), enabled=False)
        elif not c.devices:
            self._action(sub, tr("no_devices"), enabled=False)
        group = QActionGroup(sub)
        group.setExclusive(True)
        for dev in c.devices:
            label = f"{dev.name}  ({tr('st_' + dev.status_key)})"
            self._action(sub, label, (lambda _=False, i=dev.identifier: c.select_device(i)),
                         checkable=True, checked=dev is selected, group=group,
                         enabled=dev.can_cast_video and not c.is_active)
        sub.addSeparator()
        self._action(sub, tr("refresh"), c.refresh_devices, enabled=not c.scanning)
        self._action(sub, tr("add_ip"), c.ask_add_host)
        self._action(sub, tr("pair"), lambda: c.pair(),
                     enabled=bool(selected and selected.can_cast_video) and not c.is_active)
        self._action(sub, tr("forget"), c.forget, enabled=bool(selected and selected.is_paired) and not c.is_active)

    def _source_menu(self, sub: QMenu) -> None:
        c, s = self.c, self.c.settings
        group = QActionGroup(sub)
        for mon in ffmpeg.list_monitors():
            if c.virtual_device and mon.device == c.virtual_device:
                continue  # the virtual screen this cast added; it is gone once the cast ends
            self._action(sub, display_label(mon),
                         (lambda _=False, i=mon.index: c.update_settings(monitor_index=i, window_title="",
                                                                         extend_screen=False)),
                         checkable=True, checked=not s.extend_screen and not s.window_title and s.monitor_index == mon.index,
                         group=group)
        # Checks the driver (and offers to install it) before it is really selected.
        self._action(sub, tr("extend_screen"), c.choose_extend_screen, checkable=True, checked=s.extend_screen,
                     group=group, enabled=vdisplay.available() and not c.extend.busy)
        sizes = self._submenu(sub, f"{tr('extend_size')}: {size_name(s.extend_mode)}")
        sgroup = QActionGroup(sizes)
        for _name, mode in EXTEND_SIZES:
            self._action(sizes, size_label(mode), (lambda _=False, m=mode: c.update_settings(extend_mode=m)),
                         checkable=True, checked=s.extend_mode == mode, group=sgroup)
        windows = self._submenu(sub, tr("window_menu") + (": " + s.window_title[:40].replace("&", "&&") if s.window_title else ""))
        titles = [t for t in ffmpeg.list_windows() if not t.startswith(APP_NAME)]
        if not titles:
            self._action(windows, tr("no_windows"), enabled=False)
        wgroup = QActionGroup(windows)
        for title in titles[:40]:
            text = (title if len(title) <= 60 else title[:57] + "…").replace("&", "&&")  # & = mnemonic
            self._action(windows, text, (lambda _=False, t=title: c.update_settings(window_title=t, extend_screen=False)),
                         checkable=True, checked=not s.extend_screen and s.window_title == title, group=wgroup)
        if titles:
            windows.addSeparator()
            self._action(windows, tr("window_moved_hint"), enabled=False)
        sub.addSeparator()
        self._action(sub, tr("cursor"), lambda on: c.update_settings(show_cursor=on),
                     checkable=True, checked=s.show_cursor)

    def _language_menu(self, sub: QMenu) -> None:
        """Each language in its own words; choosing one shows everything in it at once."""
        group = QActionGroup(sub)
        for code, name in LANGUAGES.items():
            self._action(sub, name, (lambda _=False, code=code: self.c.set_language(code)),
                         checkable=True, checked=language() == code, group=group)

    def _quality_menu(self, sub: QMenu) -> None:
        c, s = self.c, self.c.settings

        def radio(parent: QMenu, title: str, options, current, key: str):
            menu = self._submenu(parent, title)
            group = QActionGroup(menu)
            for label, value in options:
                self._action(menu, label, (lambda _=False, v=value: c.update_settings(**{key: v})),
                             checkable=True, checked=current == value, group=group)

        radio(sub, tr("resolution"), [(tr("native") if v == 0 else n, v) for n, v in RESOLUTIONS],
              s.output_height, "output_height")
        radio(sub, tr("framerate"), [(f"{f} fps", f) for f in FRAMERATES], s.framerate, "framerate")
        radio(sub, tr("latency"), [(tr(k), v) for k, v in LATENCIES], s.latency_mode, "latency_mode")
        radio(sub, tr("bitrate"), [(f"{b} Mbps", b * 1000) for b in BITRATES], s.video_bitrate_kbps,
              "video_bitrate_kbps")
        radio(sub, tr("encoder"), [(tr("auto_encoder"), "auto")] +
              [(ffmpeg.ENCODER_LABELS[e], e) for e in c.encoders], s.encoder, "encoder")

    # -- actions -----------------------------------------------------------

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            None, tr("choose_video"), os.path.expanduser("~"), "Video (*.mp4 *.m4v *.mov);;* (*)"
        )
        if path:
            self.c.play_file(path)

    def _toggle_autostart(self, enabled: bool) -> None:
        try:
            winintegration.set_autostart(enabled)
        except OSError as ex:
            self.message(tr("autostart"), str(ex), True)

    def _create_shortcut(self) -> None:
        try:
            path = winintegration.create_desktop_shortcut(tr("shortcut_description"))
            _LOGGER.info("Shortcut: %s", path)
            self.message(tr("shortcut_done"))
        except Exception as ex:  # pylint: disable=broad-except
            self.message(tr("desktop_shortcut"), error_text(ex), True)

