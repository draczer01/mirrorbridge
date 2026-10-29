"""The tray icon and its menu: every feature is reachable from here."""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction, QActionGroup, QCursor, QFont
from PySide6.QtWidgets import QFileDialog, QMenu, QSystemTrayIcon

from .. import APP_NAME, ffmpeg, routes, vdisplay
from ..config import CHOICES, app_data_dir
from ..mirror.negotiation import DEFAULT_LEAD_MS
from ..mirror.session import MIRROR_LATENCIES, lead_override, mirror_latency, on_arrival
from . import winintegration
from .controller import Controller
from .extend import EXTEND_SIZES, error_text, size_label, size_name
from .i18n import LANGUAGES, language, language_menu_title, tr
from .latency import LatencyMeter
from .theme import app_icon

_LOGGER = logging.getLogger(__name__)

RESOLUTIONS = [("720p", 720), ("1080p", 1080), ("1440p", 1440), ("4K", 2160), ("native", 0)]
FRAMERATES = [24, 30, 60]  # Quality > Frame rate: for casts (Mirror screen's is MIRROR_FRAMERATES)
LATENCIES = [("lat_low", "low"), ("lat_balanced", "balanced"), ("lat_smooth", "smooth")]  # Quality > Delay: casts
BITRATES = [4, 8, 12, 20, 30]
MIRROR_LEVELS = CHOICES["mirror_latency"]  # Mirror screen settings > Latency: low, normal, smooth
MIRROR_FRAMERATES = CHOICES["mirror_framerate"]  # Mirror screen settings > Frame rate: 0 = Automatic, 30, 60
DEBUG_LEADS = [0, 60, 100, 150, 200, 300]  # More options > Debug > TV wait, in ms; 0 = what is saved for the TV
SCALERS = CHOICES["mirror_scaler"]  # More options > Debug > Scaling: auto (the graphics card when it can), cpu


def display_label(mon: ffmpeg.Monitor) -> str:
    return tr("display_n", n=mon.index + 1, w=mon.width, h=mon.height) + (tr("primary") if mon.primary else "")


def _escape(name: str) -> str:
    """A TV's name as a menu shows it: a "&" in it would mark the next letter as a shortcut."""
    return name.replace("&", "&&")


class TrayMenu(QObject):
    def __init__(self, controller: Controller, open_window: Callable[[], None], quit_app: Callable[[], None]) -> None:
        super().__init__()
        self.c = controller
        self.open_window = open_window
        self.quit_app = quit_app
        self.icons = {state: app_icon(state) for state in ("idle", "active", "busy")}
        self.tray = QSystemTrayIcon(self.icons["idle"])
        self.menu = QMenu()
        self.menu.setToolTipsVisible(True)  # the main item says what it does, or why it is off
        self._submenus: list[QMenu] = []
        self._meter: LatencyMeter | None = None  # More options > Debug > Latency meter, while open
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

        # The main item: the best way to show the screen on the chosen TV (Mirror screen, or a
        # cast to an Apple TV), as the window's big button; while anything runs, its Stop, and
        # while the big button's way runs with more to try, the window's "Try another way".
        device, screen = c.selected_device, c.selected_screen
        if c.is_active:
            self._action(menu, tr("stop_mirror") if c.mirroring else tr("stop"), c.stop_cast)
            if c.can_try_another_way():
                self._action(menu, tr("ui_try_another"), c.try_another_way)
        else:
            text = tr("ui_show_on", device=_escape(screen.name)) if screen else tr("show_no_device")
            show = self._action(menu, text, c.start_primary, enabled=bool(c.primary_action()))
            show.setToolTip(self._show_tip(screen))
        menu.addSeparator()

        self._device_menu(self._submenu(menu, tr("device_menu") + (f": {_escape(screen.name)}" if screen else "")))
        self._source_menu(self._submenu(menu, tr("source_menu")))
        self._mirror_menu(self._submenu(menu, tr("mirror_settings")))
        self._quality_menu(self._submenu(menu, tr("quality_menu")))
        # Mirror screen has no sound yet
        self._action(menu, tr("audio_cast"), lambda on: c.update_settings(capture_audio=on),
                     checkable=True, checked=s.capture_audio)
        self._action(menu, tr("play_file"), self._choose_file, enabled=bool(device) and not c.is_active)
        menu.addSeparator()

        more = self._submenu(menu, tr("more"))
        more.setToolTipsVisible(True)
        old_way = self._action(more, tr("cast_old_way"), c.start_cast,
                               enabled=bool(device and device.can_cast_video) and not c.is_active)
        old_way.setToolTip(tr("cast_old_way_hint"))
        more.addSeparator()
        self._action(more, tr("autostart"), self._toggle_autostart, checkable=True,
                     checked=winintegration.autostart_enabled())
        self._action(more, tr("desktop_shortcut"), self._create_shortcut)
        self._action(more, tr("log_folder"), lambda: winintegration.open_folder(str(app_data_dir())))
        self._action(more, tr("probe"), c.test_tv, enabled=bool(device and device.can_cast_video) and not c.is_active)
        if vdisplay.available():
            installed = c.vdd_status not in (None, vdisplay.DriverStatus.NOT_INSTALLED)
            self._action(more, tr("vdd_remove"), c.extend.remove_driver, enabled=installed and not c.is_active)
        debug = self._submenu(more, tr("debug_menu"))
        self._action(debug, tr("latency_meter"), self._show_latency_meter)
        self._mirror_switches(debug)
        self._language_menu(self._submenu(menu, language_menu_title()))
        menu.addSeparator()
        self._action(menu, tr("quit"), lambda: self.quit_app())

    def _show_tip(self, screen) -> str:
        """What the main item does on `screen` (a routes.Screen, or None), or why it is off: the
        window's line under its big button, said for the menu."""
        c = self.c
        if screen is None:
            if c.screens:
                return ""  # the item itself says to choose one
            return tr("ui_searching") if c.scanning else tr("ui_no_tv")
        steps = c.plan(screen)
        if not steps:
            return c.device_line(screen)  # a speaker, or AirPlay restricted to some senders
        if steps[0] in routes.MIRRORING:
            first = steps[0] == routes.AIRPLAY_MIRROR and not screen.airplay.is_paired
            return (tr("ui_hint_mirror_first") + " " if first else "") + tr("ui_hint_mirror")
        dev = screen.airplay
        hint = tr("ui_hint_apple") if c.is_apple(dev) else tr("ui_hint_cast")
        return (tr("ui_hint_cast_first") + " " if dev.needs_pairing or dev.needs_password else "") + hint

    def _device_menu(self, sub: QMenu) -> None:
        """Every TV once, however it was found (AirPlay, Google Cast or both), with what choosing
        it means; then the search, and the pairing and Forget of the chosen TV's AirPlay side."""
        c = self.c
        screens = c.screens
        chosen = c.selected_screen
        selected = c.selected_device
        if c.scanning:
            self._action(sub, tr("searching"), enabled=False)
        elif not screens:
            self._action(sub, tr("no_devices"), enabled=False)
        group = QActionGroup(sub)
        group.setExclusive(True)
        for screen in screens:
            label = f"{_escape(screen.name)}  ({c.device_line(screen)})"
            self._action(sub, label, (lambda _=False, i=screen.identifier: c.select_device(i)),
                         checkable=True, checked=chosen is not None and screen.identifier == chosen.identifier,
                         group=group, enabled=screen.shows_video and not c.is_active)
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
                continue  # the virtual screen this cast or Mirror screen added; it goes when that ends
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

    def _radio(self, parent: QMenu, title: str, options, current, key: str, **also: Any) -> QMenu:
        """A submenu of (label, value) choices for the setting `key`, the current one checked;
        choosing one also sets the settings in `also`."""
        menu = self._submenu(parent, title)
        group = QActionGroup(menu)
        for label, value in options:
            self._action(menu, label, (lambda _=False, v=value: self.c.update_settings(**{key: v}, **also)),
                         checkable=True, checked=current == value, group=group)
        return menu

    def _mirror_menu(self, sub: QMenu) -> None:
        """Mirror screen settings, for the sessions after they are set; each shows what a session
        would use, whatever a hand-edited settings.json holds. Choosing a Latency puts More options >
        Debug > TV wait back on Automatic, which would win over it."""
        s = self.c.settings
        levels = self._radio(sub, tr("mirror_latency"), [(tr("mirror_lat_" + level), level) for level in MIRROR_LEVELS],
                             mirror_latency(s), "mirror_latency", mirror_lead_ms=0)
        levels.setToolTipsVisible(True)
        for action, level in zip(levels.actions(), MIRROR_LEVELS):
            action.setToolTip(tr(f"mirror_lat_{level}_hint", ms=MIRROR_LATENCIES[level] or DEFAULT_LEAD_MS))
        fps = s.mirror_framerate if s.mirror_framerate in MIRROR_FRAMERATES else 0
        rates = self._radio(sub, tr("mirror_framerate"), [(f"{f} fps" if f else tr("mirror_fps_auto"), f)
                                                          for f in MIRROR_FRAMERATES], fps, "mirror_framerate")
        rates.setToolTipsVisible(True)
        rates.actions()[MIRROR_FRAMERATES.index(0)].setToolTip(tr("mirror_fps_auto_hint"))

    def _quality_menu(self, sub: QMenu) -> None:
        """The picture's size, bitrate and encoder; its frame rate and delay are the casts' only
        (Mirror screen has its own, in Mirror screen settings)."""
        c, s = self.c, self.c.settings
        radio = self._radio
        radio(sub, tr("resolution"), [(tr("native") if v == 0 else n, v) for n, v in RESOLUTIONS],
              s.output_height, "output_height")
        radio(sub, tr("framerate_cast"), [(f"{f} fps", f) for f in FRAMERATES], s.framerate, "framerate")
        radio(sub, tr("latency"), [(tr(k), v) for k, v in LATENCIES], s.latency_mode, "latency_mode")
        radio(sub, tr("bitrate"), [(f"{b} Mbps", b * 1000) for b in BITRATES], s.video_bitrate_kbps,
              "video_bitrate_kbps")
        radio(sub, tr("encoder"), [(tr("auto_encoder"), "auto")] +
              [(ffmpeg.ENCODER_LABELS[e], e) for e in c.encoders], s.encoder, "encoder")

    def _mirror_switches(self, debug: QMenu) -> None:
        """Mirror screen's test switches, for the sessions after they are set (the mirror command
        too; Test this TV and mirror --probe leave them out): how long the TV waits to show a
        frame, how often a keyframe comes, where the picture is scaled, and frames shown as they
        come instead of at their time. Each shows what a session would use, whatever a
        hand-edited settings.json holds."""
        s = self.c.settings
        debug.addSeparator()
        self._radio(debug, tr("debug_lead"), [(f"{ms} ms" if ms else tr("debug_lead_auto"), ms) for ms in DEBUG_LEADS],
                    lead_override(s), "mirror_lead_ms")
        keyframes = [(tr("debug_kf_every", n=n) if n else tr("debug_kf_needed"), n)
                     for n in ffmpeg.MIRROR_KEYFRAME_CHOICES]
        self._radio(debug, tr("debug_keyframes"), keyframes, ffmpeg.mirror_keyframe_seconds(s.mirror_keyframes),
                    "mirror_keyframes")
        self._radio(debug, tr("debug_scaler"), [(tr("debug_scaler_" + scaler), scaler) for scaler in SCALERS],
                    "cpu" if s.mirror_scaler == "cpu" else "auto", "mirror_scaler")
        self._action(debug, tr("debug_on_arrival"), lambda on: self.c.update_settings(mirror_on_arrival=on),
                     checkable=True, checked=on_arrival(s))

    # -- actions -----------------------------------------------------------

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            None, tr("choose_video"), os.path.expanduser("~"), "Video (*.mp4 *.m4v *.mov);;* (*)"
        )
        if path:
            self.c.play_file(path)

    def _show_latency_meter(self) -> None:
        if self._meter is None:
            self._meter = LatencyMeter()
            self._meter.closed.connect(self._latency_meter_closed)
            self._meter.show()

    def _latency_meter_closed(self) -> None:
        self._meter = None

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

