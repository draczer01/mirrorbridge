"""The full window, opened from the tray menu's "Open" item. Closing it hides it."""

from __future__ import annotations

import contextlib
import os
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QMainWindow, QPlainTextEdit, QPushButton, QRadioButton,
    QSlider, QSplitter, QStyle, QVBoxLayout, QWidget,
)

from .. import APP_NAME, ffmpeg, vdisplay
from ..session import State
from .controller import Controller
from .extend import EXTEND_SIZES, error_text, size_label
from .i18n import LANGUAGES, language, language_menu_title, say, state_text, tr
from .theme import app_icon
from .tray import LATENCIES, RESOLUTIONS, display_label


class MainWindow(QMainWindow):
    def __init__(self, controller: Controller, on_hidden=None, on_close=None) -> None:
        super().__init__()
        self.c = controller
        self.on_hidden = on_hidden
        self.on_close = on_close  # set when there is no tray: closing then quits the app
        self._loading = False  # True while code (not the user) fills the controls
        self._saving = False  # True while our own change is being written to settings
        self._status: Callable[[], str] = lambda: tr("select_device")  # what the status line says (_show_status)
        self._monitors: list[ffmpeg.Monitor] = []
        self.setWindowIcon(app_icon())
        self.resize(980, 640)
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
        root = QWidget()
        layout = QHBoxLayout(root)

        left = QVBoxLayout()
        self.devices_title = QLabel()
        self.devices_title.setFont(QFont(self.devices_title.font().family(), 13, QFont.DemiBold))
        left.addWidget(self.devices_title)
        self.device_list = QListWidget()
        self.device_list.currentRowChanged.connect(self._device_clicked)
        left.addWidget(self.device_list, 1)
        row = QHBoxLayout()
        self.refresh_btn = QPushButton()
        self.refresh_btn.clicked.connect(self.c.refresh_devices)
        self.add_ip_btn = QPushButton()
        self.add_ip_btn.clicked.connect(self.c.ask_add_host)
        self.pair_btn = QPushButton()
        self.pair_btn.clicked.connect(lambda: self.c.pair())
        self.forget_btn = QPushButton()
        self.forget_btn.clicked.connect(self.c.forget)
        for b in (self.refresh_btn, self.add_ip_btn, self.pair_btn, self.forget_btn):
            row.addWidget(b)
        left.addLayout(row)
        left_w = QWidget()
        left_w.setLayout(left)

        right = QVBoxLayout()
        self.source_box = QGroupBox()
        sf = QFormLayout(self.source_box)
        self.rb_display = QRadioButton()
        self.rb_window = QRadioButton()
        self.monitor_combo = QComboBox()
        self.window_combo = QComboBox()
        self.window_combo.setEditable(True)
        self.window_combo.setInsertPolicy(QComboBox.NoInsert)
        refresh_win = QPushButton()
        refresh_win.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        refresh_win.clicked.connect(self._fill_windows)
        wrow = QHBoxLayout()
        wrow.addWidget(self.window_combo, 1)
        wrow.addWidget(refresh_win)
        self.rb_extend = QRadioButton()
        self.rb_extend.setEnabled(vdisplay.available())
        self.extend_combo = QComboBox()
        for _name, mode in EXTEND_SIZES:
            self.extend_combo.addItem(size_label(mode), mode)
        sf.addRow(self.rb_display, self.monitor_combo)
        sf.addRow(self.rb_window, wrow)
        sf.addRow(self.rb_extend, self.extend_combo)
        self.audio_cb = QCheckBox()
        self.cursor_cb = QCheckBox()
        sf.addRow(self.audio_cb)
        sf.addRow(self.cursor_cb)
        right.addWidget(self.source_box)

        self.quality_box = QGroupBox()
        qf = QFormLayout(self.quality_box)
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
        self.encoder_combo = QComboBox()
        self._row_labels: dict[str, QLabel] = {}  # i18n key -> the label in front of its control
        for key, field in (("resolution", self.res_combo), ("framerate", self.fps_combo), ("bitrate", brow),
                           ("latency", self.latency_combo), ("encoder", self.encoder_combo)):
            self._row_labels[key] = QLabel()
            qf.addRow(self._row_labels[key], field)
        right.addWidget(self.quality_box)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        right.addWidget(self.status_label)
        self.cast_btn = QPushButton()
        self.cast_btn.setObjectName("cast")
        self.cast_btn.clicked.connect(self._cast_clicked)
        right.addWidget(self.cast_btn)
        self.mirror_btn = QPushButton()
        self.mirror_btn.clicked.connect(lambda: self.c.start_mirror())
        right.addWidget(self.mirror_btn)
        self.file_btn = QPushButton()
        self.file_btn.clicked.connect(self._play_file)
        right.addWidget(self.file_btn)
        right.addStretch(1)
        # The same choice as the tray menu's, named in every language so anyone finds it.
        lrow = QHBoxLayout()
        lrow.addWidget(QLabel(language_menu_title()))
        self.language_combo = QComboBox()
        for code, name in LANGUAGES.items():
            self.language_combo.addItem(name, code)
        self.language_combo.setCurrentIndex(max(0, self.language_combo.findData(language())))
        lrow.addWidget(self.language_combo)
        lrow.addStretch(1)
        right.addLayout(lrow)
        right_w = QWidget()
        right_w.setLayout(right)

        top = QSplitter()
        top.addWidget(left_w)
        top.addWidget(right_w)
        top.setSizes([420, 520])

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        outer = QSplitter(Qt.Vertical)
        outer.addWidget(top)
        outer.addWidget(self.log_view)
        outer.setSizes([480, 140])
        layout.addWidget(outer)
        self.setCentralWidget(root)
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
        self.res_combo.currentIndexChanged.connect(lambda _i: self._save(output_height=int(self.res_combo.currentData())))
        self.fps_combo.currentIndexChanged.connect(lambda _i: self._save(framerate=int(self.fps_combo.currentData())))
        self.bitrate.valueChanged.connect(self._bitrate_changed)
        self.bitrate.sliderReleased.connect(lambda: self._bitrate_changed(self.bitrate.value()))
        self.latency_combo.currentIndexChanged.connect(lambda _i: self._save(latency_mode=self.latency_combo.currentData()))
        self.encoder_combo.currentIndexChanged.connect(
            lambda _i: self.encoder_combo.currentData() and self._save(encoder=self.encoder_combo.currentData())
        )
        self.language_combo.currentIndexChanged.connect(self._language_chosen)

    def _retranslate(self) -> None:
        """Every fixed word in the window, in the app's language."""
        self.setWindowTitle(tr("window_title"))
        self.devices_title.setText(tr("devices"))
        for button, key in ((self.refresh_btn, "refresh"), (self.add_ip_btn, "add_ip"), (self.pair_btn, "pair"),
                            (self.forget_btn, "forget"), (self.rb_display, "entire_display"),
                            (self.rb_window, "single_window"), (self.rb_extend, "extend_screen"),
                            (self.audio_cb, "audio"), (self.cursor_cb, "cursor"), (self.file_btn, "play_file"),
                            (self.mirror_btn, "mirror_screen")):
            button.setText(tr(key))
        self.source_box.setTitle(tr("source_menu"))
        self.window_combo.setToolTip(tr("window_moved_hint"))
        self.rb_extend.setToolTip(tr("extend_hint"))
        self.extend_combo.setToolTip(tr("extend_size"))
        self.quality_box.setTitle(tr("quality_menu"))
        for key, label in self._row_labels.items():
            label.setText(tr(key))
        self.res_combo.setItemText(self.res_combo.findData(0), tr("native"))
        for i, (key, _value) in enumerate(LATENCIES):
            self.latency_combo.setItemText(i, tr(key))
        self.status_label.setText(self._status())
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
        self._update_buttons()  # Start casting or Stop casting

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
        virtual = self.c.virtual_device  # the virtual screen a cast added; it goes when the cast ends
        with self._filling():
            self.monitor_combo.clear()
            for mon in monitors:
                if not (virtual and mon.device == virtual):
                    self.monitor_combo.addItem(display_label(mon), mon.index)
            self.monitor_combo.setCurrentIndex(max(0, self.monitor_combo.findData(self.c.settings.monitor_index)))

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
            self.res_combo.setCurrentIndex(max(0, self.res_combo.findData(s.output_height)))
            self.fps_combo.setCurrentIndex(max(0, self.fps_combo.findData(s.framerate)))
            self.bitrate.setValue(max(2, s.video_bitrate_kbps // 1000))
            self.bitrate_label.setText(f"{self.bitrate.value()} Mbps")
            self.latency_combo.setCurrentIndex(max(0, self.latency_combo.findData(s.latency_mode)))
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
        if not self.c.is_active:
            n = len(self.c.devices)
            self._show_status(lambda: tr("found_n", n=n) if n else tr("found_none"))
        self._update_buttons()

    def _fill_devices(self) -> None:
        with self._filling():
            self.device_list.clear()
            selected = self.c.selected_device
            for dev in self.c.devices:
                item = QListWidgetItem(f"{dev.name}\n{dev.model} - {dev.address} - {tr('st_' + dev.status_key)}")
                icon = QStyle.SP_ComputerIcon if dev.can_cast_video else QStyle.SP_MediaVolume
                item.setIcon(self.style().standardIcon(icon))
                if not dev.can_cast_video:
                    item.setForeground(QColor(140, 140, 150))
                self.device_list.addItem(item)
                if dev is selected:
                    self.device_list.setCurrentItem(item)

    def _device_clicked(self, row: int) -> None:
        if not self._loading and 0 <= row < len(self.c.devices):
            self.c.select_device(self.c.devices[row].identifier)

    def _scanning(self, scanning: bool) -> None:
        self.refresh_btn.setEnabled(not scanning)
        if scanning:
            self._show_status(lambda: tr("searching"))

    def _show_status(self, text: Callable[[], str]) -> None:
        """Show a status line, from a function so that another language can say it again."""
        self._status = text
        self.status_label.setText(text())

    # -- casting -----------------------------------------------------------

    def _cast_clicked(self) -> None:
        self.c.toggle_cast()

    def _play_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, tr("choose_video"), os.path.expanduser("~"), "Video (*.mp4 *.m4v *.mov);;* (*)"
        )
        if path:
            self.c.play_file(path)

    def _cast_state(self, state: State, message) -> None:
        def line() -> str:
            text = state_text(state)
            if message and state in (State.STARTING, State.CASTING, State.FAILED):
                # encoder in use, stream address, or the error, in the user's language
                said = error_text(message) if isinstance(message, BaseException) else say(message)
                text = f"{text}: {said}"
            return text

        self._show_status(line)

    def _activity_changed(self) -> None:
        self._update_buttons()
        if not self.c.is_active:  # a cast that ended may have removed its virtual screen
            self.c.run_blocking(ffmpeg.list_monitors, on_ok=self._show_monitors)

    def _update_buttons(self) -> None:
        dev = self.c.selected_device
        active = self.c.is_active
        usable = bool(dev and dev.can_cast_video)
        self.pair_btn.setEnabled(usable and not active)
        self.forget_btn.setEnabled(bool(dev and dev.is_paired) and not active)
        self.device_list.setEnabled(not active)
        self.cast_btn.setEnabled(usable or active)
        self.mirror_btn.setEnabled(self.c.can_mirror(dev) and not active)
        self.mirror_btn.setToolTip(self.c.mirror_tip(dev))
        self.file_btn.setEnabled(usable and not active)
        self.cast_btn.setText(tr("stop_mirror") if self.c.mirroring else tr("stop") if active else tr("start"))
        self.cast_btn.setProperty("casting", "true" if active else "false")
        self.cast_btn.style().unpolish(self.cast_btn)
        self.cast_btn.style().polish(self.cast_btn)

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
