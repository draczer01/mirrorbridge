"""The full window, opened from the tray menu's "Open" item. Closing it hides it.

It is laid out for someone who has never used it: three numbered steps (choose your TV, what to
show, show it) and one big button that picks the best way to show the screen on the chosen TV.
What it will do, or why it cannot and what to do about it, is written under the button rather
than in a tooltip. The other ways to show, the quality settings and the activity log are folded
away below.
"""

from __future__ import annotations

import contextlib
import os
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QPlainTextEdit, QPushButton, QRadioButton, QScrollArea, QSlider,
    QStyle, QToolButton, QVBoxLayout, QWidget,
)

from .. import APP_NAME, ffmpeg, routes, vdisplay
from ..devices import Device
from ..session import State
from .controller import Controller
from .extend import EXTEND_SIZES, error_text, size_label
from .i18n import LANGUAGES, language, language_menu_title, say, tr
from .theme import app_icon
from ..mirror.negotiation import DEFAULT_LEAD_MS
from ..mirror.session import MIRROR_LATENCIES, mirror_latency
from .tray import LATENCIES, MIRROR_FRAMERATES, MIRROR_LEVELS, RESOLUTIONS, display_label

_CONNECTING = (State.STARTING, State.BUFFERING, State.CONNECTING)


class Section(QWidget):
    """A heading that folds its content away, closed until it is clicked."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.toggle = QToolButton()
        self.toggle.setObjectName("section")
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(Qt.RightArrow)
        self.toggle.setCheckable(True)
        self.toggle.toggled.connect(self.set_open)
        self.body = QWidget()
        self.body.setVisible(False)
        self.content = QVBoxLayout(self.body)
        self.content.setContentsMargins(18, 4, 0, 4)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.toggle)
        layout.addWidget(self.body)

    def set_title(self, title: str) -> None:
        self.toggle.setText(title)

    def set_open(self, on: bool) -> None:
        self.toggle.setChecked(on)
        self.toggle.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        self.body.setVisible(on)

    def is_open(self) -> bool:
        return self.toggle.isChecked()


def _hint() -> QLabel:
    """A line of explanation in smaller, softer words, under the control it explains."""
    label = QLabel()
    label.setObjectName("hint")
    label.setWordWrap(True)
    return label


def _step_title() -> QLabel:
    label = QLabel()
    label.setFont(QFont(label.font().family(), 12, QFont.DemiBold))
    return label


class MainWindow(QMainWindow):
    def __init__(self, controller: Controller, on_hidden=None, on_close=None) -> None:
        super().__init__()
        self.c = controller
        self.on_hidden = on_hidden
        self.on_close = on_close  # set when there is no tray: closing then quits the app
        self._loading = False  # True while code (not the user) fills the controls
        self._saving = False  # True while our own change is being written to settings
        self._status: Callable[[], str] = lambda: ""  # what the status line says (_show_status)
        self._monitors: list[ffmpeg.Monitor] = []
        self._screens: list = []  # the TVs as the list shows them, row by row
        self.setWindowIcon(app_icon())
        self.resize(620, 760)
        self._build_ui()
        self._load_settings()
        self._render_devices()
        self._render_encoders(self.c.encoders)
        self._update_buttons()

        c = self.c
        c.devices_changed.connect(self._render_devices)
        c.scanning_changed.connect(self._scanning)
        c.activity_changed.connect(self._activity_changed)
        c.cast_state_changed.connect(self._cast_state)
        c.encoders_detected.connect(self._render_encoders)
        c.settings_changed.connect(self._load_settings)
        c.language_changed.connect(self._language_changed)
        for line in c.recent_log:
            self.log_view.appendPlainText(line)
        c.log_line.connect(self.log_view.appendPlainText)
        c.dialog_parent = lambda: self if self.isVisible() else None

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        """The controls; _retranslate gives them their words."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(8)

        # The same choice as the tray menu's, named in every language so anyone finds it.
        lrow = QHBoxLayout()
        lrow.addStretch(1)
        lrow.addWidget(QLabel(language_menu_title()))
        self.language_combo = QComboBox()
        for code, name in LANGUAGES.items():
            self.language_combo.addItem(name, code)
        self.language_combo.setCurrentIndex(max(0, self.language_combo.findData(language())))
        lrow.addWidget(self.language_combo)
        layout.addLayout(lrow)

        # 1. Choose your TV
        self.step1_title = _step_title()
        layout.addWidget(self.step1_title)
        self.device_list = QListWidget()
        self.device_list.setMinimumHeight(120)
        self.device_list.currentRowChanged.connect(self._device_clicked)
        layout.addWidget(self.device_list, 1)
        self.devices_hint = _hint()
        layout.addWidget(self.devices_hint)
        row = QHBoxLayout()
        self.refresh_btn = QPushButton()
        self.refresh_btn.clicked.connect(self.c.refresh_devices)
        self.add_ip_btn = QPushButton()
        self.add_ip_btn.clicked.connect(self.c.ask_add_host)
        row.addWidget(self.refresh_btn)
        row.addWidget(self.add_ip_btn)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addSpacing(6)

        # 2. What to show
        self.step2_title = _step_title()
        layout.addWidget(self.step2_title)
        self.rb_display = QRadioButton()
        self.monitor_combo = QComboBox()
        drow = QHBoxLayout()
        drow.addWidget(self.rb_display)
        drow.addWidget(self.monitor_combo, 1)
        layout.addLayout(drow)
        self.rb_window = QRadioButton()
        self.window_combo = QComboBox()
        self.window_combo.setEditable(True)
        self.window_combo.setInsertPolicy(QComboBox.NoInsert)
        self.refresh_win = QPushButton()
        self.refresh_win.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.refresh_win.clicked.connect(self._fill_windows)
        wrow = QHBoxLayout()
        wrow.addWidget(self.rb_window)
        wrow.addWidget(self.window_combo, 1)
        wrow.addWidget(self.refresh_win)
        layout.addLayout(wrow)
        self.rb_extend = QRadioButton()
        self.rb_extend.setEnabled(vdisplay.available())
        layout.addWidget(self.rb_extend)
        self.extend_hint = _hint()
        self.extend_hint.setContentsMargins(24, 0, 0, 0)
        layout.addWidget(self.extend_hint)
        layout.addSpacing(6)

        # 3. Show it
        self.step3_title = _step_title()
        layout.addWidget(self.step3_title)
        self.show_btn = QPushButton()
        self.show_btn.setObjectName("cast")
        self.show_btn.setMinimumHeight(48)
        self.show_btn.clicked.connect(self._show_clicked)
        layout.addWidget(self.show_btn)
        self.action_hint = _hint()
        layout.addWidget(self.action_hint)
        self.status_label = QLabel()
        self.status_label.setObjectName("status")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.another_btn = QPushButton()  # the TV stays black: the next way, without stopping first
        self.another_btn.clicked.connect(self.c.try_another_way)
        arow = QHBoxLayout()
        arow.addWidget(self.another_btn)
        arow.addStretch(1)
        layout.addLayout(arow)
        self.file_btn = QPushButton()
        self.file_btn.clicked.connect(self._play_file)
        frow = QHBoxLayout()
        frow.addWidget(self.file_btn)
        frow.addStretch(1)
        layout.addLayout(frow)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setObjectName("rule")
        layout.addWidget(line)

        # Other ways to show: the fallbacks, and the pairing by hand
        self.other_section = Section()
        self.cast_btn = QPushButton()
        self.cast_btn.clicked.connect(self._cast_clicked)
        self.cast_hint = _hint()
        self.pair_btn = QPushButton()
        self.pair_btn.clicked.connect(lambda: self.c.pair())
        self.forget_btn = QPushButton()
        self.forget_btn.clicked.connect(self.c.forget)
        prow = QHBoxLayout()
        prow.addWidget(self.pair_btn)
        prow.addWidget(self.forget_btn)
        prow.addStretch(1)
        for widget in (self.cast_btn, self.cast_hint):
            self.other_section.content.addWidget(widget)
        self.other_section.content.addLayout(prow)
        layout.addWidget(self.other_section)

        # Advanced settings: what most people never need to touch
        self.advanced_section = Section()
        af = QFormLayout()
        self.extend_combo = QComboBox()
        for _name, mode in EXTEND_SIZES:
            self.extend_combo.addItem(size_label(mode), mode)
        self.res_combo = QComboBox()
        for label, h in RESOLUTIONS:
            self.res_combo.addItem(label, h)
        self.fps_combo = QComboBox()
        for f in (24, 30, 60):
            self.fps_combo.addItem(f"{f} fps", f)
        self.bitrate = QSlider(Qt.Horizontal)
        self.bitrate.setRange(2, 40)
        self.bitrate_label = QLabel()
        brow = QHBoxLayout()
        brow.addWidget(self.bitrate, 1)
        brow.addWidget(self.bitrate_label)
        self.latency_combo = QComboBox()
        for _key, value in LATENCIES:
            self.latency_combo.addItem("", value)
        # Mirror screen's own two (the tray's Mirror screen settings)
        self.mirror_latency_combo = QComboBox()
        for level in MIRROR_LEVELS:
            self.mirror_latency_combo.addItem("", level)
        self.mirror_latency_hint = QLabel()
        self.mirror_latency_hint.setObjectName("hint")
        self.mirror_latency_hint.setWordWrap(True)
        self.mirror_fps_combo = QComboBox()
        for f in MIRROR_FRAMERATES:
            self.mirror_fps_combo.addItem(f"{f} fps" if f else "", f)
        self.encoder_combo = QComboBox()
        self._row_labels: dict[str, QLabel] = {}  # i18n key -> the label in front of its control
        for key, field in (("extend_size", self.extend_combo), ("resolution", self.res_combo),
                           ("ui_cast_framerate", self.fps_combo), ("bitrate", brow), ("ui_cast_latency", self.latency_combo),
                           ("ui_mirror_latency", self.mirror_latency_combo), ("ui_mirror_framerate", self.mirror_fps_combo),
                           ("encoder", self.encoder_combo)):
            self._row_labels[key] = QLabel()
            af.addRow(self._row_labels[key], field)
            if key == "ui_mirror_latency":
                af.addRow("", self.mirror_latency_hint)
        self.audio_cb = QCheckBox()
        self.cursor_cb = QCheckBox()
        self.mute_pc_cb = QCheckBox()
        af.addRow(self.audio_cb)
        af.addRow(self.mute_pc_cb)
        af.addRow(self.cursor_cb)
        self.advanced_section.content.addLayout(af)
        layout.addWidget(self.advanced_section)

        # Technical details: the activity log, for a bug report
        self.details_section = Section()
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setMinimumHeight(160)
        self.details_section.content.addWidget(self.log_view)
        layout.addWidget(self.details_section)
        layout.addStretch(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(page)
        self.setCentralWidget(scroll)
        self._retranslate()

        # Every control writes straight to the shared settings, so the tray menu agrees.
        self.rb_display.toggled.connect(self._source_changed)
        self.rb_extend.toggled.connect(self._source_changed)
        self.extend_combo.currentIndexChanged.connect(lambda _i: self._save(extend_mode=self.extend_combo.currentData()))
        self.monitor_combo.currentIndexChanged.connect(self._source_changed)
        self.window_combo.activated.connect(self._source_changed)
        self.window_combo.lineEdit().editingFinished.connect(self._source_changed)
        self.audio_cb.toggled.connect(lambda on: self._save(capture_audio=on))
        self.cursor_cb.toggled.connect(lambda on: self._save(show_cursor=on))
        self.mute_pc_cb.toggled.connect(lambda on: self._save(mute_pc_audio=on))
        self.res_combo.currentIndexChanged.connect(lambda _i: self._save(output_height=int(self.res_combo.currentData())))
        self.fps_combo.currentIndexChanged.connect(lambda _i: self._save(framerate=int(self.fps_combo.currentData())))
        self.bitrate.valueChanged.connect(self._bitrate_changed)
        self.bitrate.sliderReleased.connect(lambda: self._bitrate_changed(self.bitrate.value()))
        self.latency_combo.currentIndexChanged.connect(lambda _i: self._save(latency_mode=self.latency_combo.currentData()))
        # A Latency puts the tray's Debug > TV wait back on Automatic, which would win over it.
        self.mirror_latency_combo.currentIndexChanged.connect(self._mirror_latency_chosen)
        self.mirror_fps_combo.currentIndexChanged.connect(
            lambda _i: self._save(mirror_framerate=int(self.mirror_fps_combo.currentData())))
        self.encoder_combo.currentIndexChanged.connect(
            lambda _i: self.encoder_combo.currentData() and self._save(encoder=self.encoder_combo.currentData())
        )
        self.language_combo.currentIndexChanged.connect(self._language_chosen)

    def _retranslate(self) -> None:
        """Every fixed word in the window, in the app's language."""
        self.setWindowTitle(tr("window_title"))
        for label, key in ((self.step1_title, "ui_step_tv"), (self.step2_title, "ui_step_what"),
                           (self.step3_title, "ui_step_show")):
            label.setText(tr(key))
        for button, key in ((self.refresh_btn, "refresh"), (self.add_ip_btn, "add_ip"), (self.pair_btn, "pair"),
                            (self.forget_btn, "forget"), (self.rb_display, "ui_show_display"),
                            (self.rb_window, "ui_show_window"), (self.rb_extend, "ui_show_extend"),
                            (self.audio_cb, "audio"), (self.cursor_cb, "cursor"), (self.mute_pc_cb, "ui_mute_pc"),
                            (self.file_btn, "play_file"),
                            (self.cast_btn, "ui_cast_instead"), (self.another_btn, "ui_try_another")):
            button.setText(tr(key))
        self.refresh_win.setToolTip(tr("ui_refresh_windows"))
        self.window_combo.setToolTip(tr("window_moved_hint"))
        self.extend_hint.setText(tr("ui_extend_hint") if vdisplay.available() else tr("vdd_unsupported"))
        self.cast_hint.setText(tr("ui_cast_hint"))
        self.other_section.set_title(tr("ui_other_ways"))
        self.advanced_section.set_title(tr("ui_advanced"))
        self.details_section.set_title(tr("ui_details"))
        for key, label in self._row_labels.items():
            label.setText(tr(key))
        self.res_combo.setItemText(self.res_combo.findData(0), tr("native"))
        for i, (key, _value) in enumerate(LATENCIES):
            self.latency_combo.setItemText(i, tr(key))
        for i, level in enumerate(MIRROR_LEVELS):
            self.mirror_latency_combo.setItemText(i, tr("mirror_lat_" + level))
        self.mirror_fps_combo.setItemText(self.mirror_fps_combo.findData(0), tr("mirror_fps_auto"))
        self._show_mirror_latency_hint()
        self._show_status(self._status)
        self.log_view.setPlaceholderText(tr("log"))

    def _language_chosen(self, _index: int) -> None:
        if not self._loading:
            self.c.set_language(self.language_combo.currentData())

    def _language_changed(self) -> None:
        """Language / Idioma, here or in the tray menu: every word again, the lists' too."""
        self._retranslate()
        with self._filling():
            self.language_combo.setCurrentIndex(max(0, self.language_combo.findData(language())))
        self._show_monitors(self._monitors)
        self._fill_devices()
        self._render_encoders(self.c.encoders)
        self._update_buttons()

    # -- settings <-> UI ---------------------------------------------------

    @contextlib.contextmanager
    def _filling(self):
        previous, self._loading = self._loading, True
        try:
            yield
        finally:
            self._loading = previous

    def _save(self, **changes) -> None:
        if self._loading:
            return
        self._saving = True
        try:
            self.c.update_settings(**changes)
        finally:
            self._saving = False

    def _mirror_latency_chosen(self, _index: int) -> None:
        self._show_mirror_latency_hint()
        self._save(mirror_latency=self.mirror_latency_combo.currentData(), mirror_lead_ms=0)

    def _show_mirror_latency_hint(self) -> None:
        level = self.mirror_latency_combo.currentData() or "normal"
        self.mirror_latency_hint.setText(tr(f"mirror_lat_{level}_hint", ms=MIRROR_LATENCIES[level] or DEFAULT_LEAD_MS))

    def _bitrate_changed(self, value: int) -> None:
        self.bitrate_label.setText(f"{value} Mbps")
        if not self.bitrate.isSliderDown():  # dragging saves once, on release
            self._save(video_bitrate_kbps=value * 1000)

    def _source_changed(self, *_args) -> None:
        if self._loading:
            return
        window_mode = self.rb_window.isChecked()
        self.monitor_combo.setEnabled(self.rb_display.isChecked())
        self.window_combo.setEnabled(window_mode)
        if self.rb_extend.isChecked():
            if not self.c.settings.extend_screen:
                self.c.choose_extend_screen()  # checks (or installs) the driver, then saves the choice
        elif window_mode:
            title = self.window_combo.currentText().strip()
            if not title and self.window_combo.count():
                self.window_combo.setCurrentIndex(0)
                title = self.window_combo.currentText().strip()
            if title:  # with no window picked yet, keep the radio on and wait for one
                self._save(window_title=title, extend_screen=False)
        else:
            self._save(window_title="", monitor_index=int(self.monitor_combo.currentData() or 0), extend_screen=False)

    def _fill_monitors(self) -> None:
        self._show_monitors(ffmpeg.list_monitors())

    def _show_monitors(self, monitors: list[ffmpeg.Monitor]) -> None:
        self._monitors = monitors
        virtual = self.c.virtual_device  # the virtual screen a cast or Mirror screen added; it goes when that ends
        with self._filling():
            self.monitor_combo.clear()
            for mon in monitors:
                if not (virtual and mon.device == virtual):
                    self.monitor_combo.addItem(display_label(mon), mon.index)
            self.monitor_combo.setCurrentIndex(max(0, self.monitor_combo.findData(self.c.settings.monitor_index)))
            # One screen needs no choosing: the combo only says which it is.
            self.monitor_combo.setVisible(self.monitor_combo.count() > 1)

    def _fill_windows(self) -> None:
        current = self.c.settings.window_title or self.window_combo.currentText()
        with self._filling():
            self.window_combo.clear()
            self.window_combo.addItems([t for t in ffmpeg.list_windows() if not t.startswith(APP_NAME)])
            self.window_combo.setCurrentText(current)

    def _load_settings(self) -> None:
        if self._saving:  # our own change coming back; the controls already show it
            return
        s = self.c.settings
        with self._filling():
            if self.monitor_combo.count() == 0:
                self._fill_monitors()
                self._fill_windows()
            (self.rb_extend if s.extend_screen else self.rb_window if s.window_title else self.rb_display).setChecked(True)
            if s.window_title:
                self.window_combo.setCurrentText(s.window_title)
            self.monitor_combo.setCurrentIndex(max(0, self.monitor_combo.findData(s.monitor_index)))
            self.extend_combo.setCurrentIndex(max(0, self.extend_combo.findData(s.extend_mode)))
            self.audio_cb.setChecked(s.capture_audio)
            self.cursor_cb.setChecked(s.show_cursor)
            self.mute_pc_cb.setChecked(s.mute_pc_audio)
            self.res_combo.setCurrentIndex(max(0, self.res_combo.findData(s.output_height)))
            self.fps_combo.setCurrentIndex(max(0, self.fps_combo.findData(s.framerate)))
            self.bitrate.setValue(max(2, s.video_bitrate_kbps // 1000))
            self.bitrate_label.setText(f"{self.bitrate.value()} Mbps")
            self.latency_combo.setCurrentIndex(max(0, self.latency_combo.findData(s.latency_mode)))
            self.mirror_latency_combo.setCurrentIndex(max(0, self.mirror_latency_combo.findData(mirror_latency(s))))
            self._show_mirror_latency_hint()
            self.mirror_fps_combo.setCurrentIndex(max(0, self.mirror_fps_combo.findData(s.mirror_framerate)))
            self.encoder_combo.setCurrentIndex(max(0, self.encoder_combo.findData(s.encoder)))
            self.monitor_combo.setEnabled(not s.window_title and not s.extend_screen)
            self.window_combo.setEnabled(bool(s.window_title) and not s.extend_screen)

    def _render_encoders(self, encoders) -> None:
        with self._filling():
            self.encoder_combo.clear()
            self.encoder_combo.addItem(tr("auto_encoder"), "auto")
            for enc in encoders:
                self.encoder_combo.addItem(ffmpeg.ENCODER_LABELS[enc], enc)
            self.encoder_combo.setCurrentIndex(max(0, self.encoder_combo.findData(self.c.settings.encoder)))

    # -- devices -----------------------------------------------------------

    def _render_devices(self) -> None:
        self._fill_devices()
        self._update_buttons()

    def _fill_devices(self) -> None:
        with self._filling():
            self.device_list.clear()
            selected = self.c.selected_screen
            self._screens = self.c.screens
            for screen in self._screens:
                item = QListWidgetItem(f"{screen.name}\n{self.c.device_line(screen)}")
                item.setToolTip(f"{screen.model} - {screen.host}")
                icon = QStyle.SP_ComputerIcon if screen.shows_video else QStyle.SP_MediaVolume
                item.setIcon(self.style().standardIcon(icon))
                if not screen.shows_video:  # a speaker shows no picture: it cannot be chosen
                    item.setForeground(QColor(140, 140, 150))
                    item.setFlags(item.flags() & ~(Qt.ItemIsSelectable | Qt.ItemIsEnabled))
                self.device_list.addItem(item)
                if selected is not None and screen.identifier == selected.identifier:
                    self.device_list.setCurrentItem(item)
        if self.c.scanning and not self._screens:
            self.devices_hint.setText(tr("ui_searching"))
        elif not self._screens:
            self.devices_hint.setText(tr("ui_no_tv"))
        elif not any(screen.shows_video for screen in self._screens):
            self.devices_hint.setText(tr("ui_only_speakers"))
        elif selected is None:
            self.devices_hint.setText(tr("ui_pick_tv"))
        else:
            self.devices_hint.setText(tr("ui_same_wifi"))

    def _device_clicked(self, row: int) -> None:
        if not self._loading and 0 <= row < len(self._screens):
            self.c.select_device(self._screens[row].identifier)

    def _scanning(self, scanning: bool) -> None:
        self.refresh_btn.setEnabled(not scanning)
        self.refresh_btn.setText(tr("searching") if scanning else tr("refresh"))
        self._fill_devices()

    def _show_status(self, text: Callable[[], str]) -> None:
        """Show a status line, from a function so that another language can say it again."""
        self._status = text
        self.status_label.setText(text())
        self.status_label.setVisible(bool(self.status_label.text()))

    # -- casting -----------------------------------------------------------

    def _show_clicked(self) -> None:
        if self.c.is_active:
            self.c.stop_cast()
        else:
            self.c.start_primary()

    def _cast_clicked(self) -> None:
        self.c.toggle_cast()

    def _play_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, tr("choose_video"), os.path.expanduser("~"), "Video (*.mp4 *.m4v *.mov);;* (*)"
        )
        if path:
            self.c.play_file(path)

    def _cast_state(self, state: State, message) -> None:
        device = self.c.casting_to
        mirroring = self.c.mirroring

        def line() -> str:
            if state in _CONNECTING:
                return tr("ui_connecting", device=device) if device else tr("ui_connecting_tv")
            if state == State.EXTENDING:
                return tr("state_extending") + "…"
            if state == State.CASTING:
                return tr("ui_on_tv_mirror" if mirroring else "ui_on_tv_cast", device=device)
            if state == State.STOPPING:
                return tr("state_stopping") + "…"
            if state == State.FAILED:
                said = error_text(message) if isinstance(message, BaseException) else say(message)
                return tr("ui_failed", error=said) if said else tr("ui_failed_plain")
            return ""

        self._show_status(line)

    def _activity_changed(self) -> None:
        self._update_buttons()
        if not self.c.is_active:  # a cast or Mirror screen that ended may have removed its virtual screen
            self.c.run_blocking(ffmpeg.list_monitors, on_ok=self._show_monitors)

    def action_text(self, screen) -> str:
        """The line under the big button: what it will do, or why it cannot and what to do."""
        c = self.c
        if c.is_active:
            return tr("ui_hint_stop")
        if not c.screens:
            return tr("ui_hint_no_tv")
        if screen is None:
            return tr("ui_hint_pick")
        steps = c.plan(screen)
        if not steps:
            return c.device_line(screen)
        first = steps[0]
        if first == routes.AIRPLAY_MIRROR and not screen.airplay.is_paired:
            return tr("ui_hint_mirror_first") + " " + tr("ui_hint_mirror")
        if first == routes.CAST_MIRROR:
            return tr("ui_hint_cast_mirror")
        if first in routes.MIRRORING:
            return tr("ui_hint_mirror")
        dev = screen.airplay
        hint = tr("ui_hint_apple") if c.is_apple(dev) else tr("ui_hint_cast")
        if dev.needs_pairing or dev.needs_password:
            hint = tr("ui_hint_cast_first") + " " + hint
        return hint

    def _update_buttons(self) -> None:
        c = self.c
        dev = c.selected_device
        screen = c.selected_screen
        active = c.is_active
        usable = bool(dev and dev.can_cast_video)  # the AirPlay ways
        shows = bool(screen and screen.shows_video)  # any way
        self.pair_btn.setEnabled(usable and not active)
        self.forget_btn.setEnabled(bool(dev and dev.is_paired) and not active)
        self.device_list.setEnabled(not active)
        for widget in (self.rb_display, self.rb_window, self.monitor_combo, self.window_combo, self.refresh_win):
            widget.setEnabled(not active)
        self.rb_extend.setEnabled(vdisplay.available() and not active)
        if not active:
            self.monitor_combo.setEnabled(self.rb_display.isChecked())
            self.window_combo.setEnabled(self.rb_window.isChecked())

        if active:
            name = c.casting_to
            self.show_btn.setText(tr("ui_stop_on", device=name) if name else tr("ui_stop"))
        elif shows:
            self.show_btn.setText(tr("ui_show_on", device=screen.name))
        else:
            self.show_btn.setText(tr("ui_show_on_tv"))
        self.show_btn.setEnabled(active or shows)
        self.show_btn.setProperty("casting", "true" if active else "false")
        self.show_btn.style().unpolish(self.show_btn)
        self.show_btn.style().polish(self.show_btn)
        self.action_hint.setText(self.action_text(screen))
        self.another_btn.setVisible(c.can_try_another_way())

        # The fallbacks, for a TV where the big button's way does not work
        self.cast_btn.setEnabled(usable and not active)
        fallback = usable and c.primary_action() != "cast"  # with an Apple TV the big button already casts
        self.cast_btn.setVisible(fallback)
        self.cast_hint.setVisible(fallback)
        self.file_btn.setEnabled(usable and not active)

    # -- window behaviour --------------------------------------------------

    def bring_up(self) -> None:
        self._fill_windows()
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:  # noqa: N802
        event.ignore()  # the app lives in the tray; Quit is in the tray menu
        if self.on_close:
            self.on_close()
            return
        self.hide()
        if self.on_hidden:
            self.on_hidden()
