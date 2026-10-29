"""More options > Debug > Latency meter: a big clock on every screen, to photograph the PC's screen
and the TV together. The difference between the two clocks in the photo is the delay from screen
to TV, all of it: capture, encoding, the network and the TV's own decoding and display, which the
"Mirroring:" lines of the log (mirror/stats.py) cannot see.

One window per screen, so that the clock is also on the virtual screen of "Extend screen" (shown
only on the TV) and on a screen added while it is open. The tenths of a second are also shown as
a row of cells, readable when the digits blur in a photo. The last "Mirroring:" line of the log
is shown below, for the part the PC can measure.

The meter runs in the process that mirrors, and its painting holds the GIL the mirroring session
also needs: drawing the whole window with huge text on every frame starved the session (17 fps of
24, frames 400 ms old when sent), and the meter then measured its own delay. So each window draws
its characters once, as pixmaps, at its own size; on each frame it repaints only the clock and the
cells, from those pixmaps, at its own screen's refresh rate. What its painting costs is shown on
each window and logged when it closes.
"""

from __future__ import annotations

import logging
import re
import time

from PySide6.QtCore import QObject, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QGuiApplication, QKeyEvent, QPainter, QPixmap, QRegion,
                           QScreen)
from PySide6.QtWidgets import QWidget

from .i18n import tr

_LOGGER = logging.getLogger(__name__)

STATS_LOGGER = "mirrorbridge.mirror.stats"
CELLS = 10  # one per tenth of a second
GLYPHS = "0123456789."
MIN_TICK_MS = 2
LIT, DARK, TEXT, DIM = QColor("#30d158"), QColor("#2a2a2e"), QColor("white"), QColor("#a0a0aa")


class _StatsHandler(logging.Handler):
    """Hands the mirroring session's "Mirroring:" lines (logged on the asyncio thread) to Qt."""

    def __init__(self, emit_line) -> None:
        super().__init__(logging.INFO)
        self._emit_line = emit_line

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno == logging.INFO:
            self._emit_line(record.getMessage())


class LatencyMeter(QObject):
    """The clock windows, opened together and closed together (Esc, or a click on any of them)."""

    stats_line = Signal(str)
    closed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.started = time.perf_counter()
        self.stats = ""
        self.windows: dict[QScreen, _ClockWindow] = {}
        self.open = False
        self._handler = _StatsHandler(self.stats_line.emit)
        self.stats_line.connect(self._set_stats)

    def show(self) -> None:
        app = QGuiApplication.instance()
        self.open = True
        for screen in QGuiApplication.screens():
            self._add(screen)
        app.screenAdded.connect(self._add)  # the virtual screen of "Extend screen", added by a cast
        app.screenRemoved.connect(self._remove)
        logging.getLogger(STATS_LOGGER).addHandler(self._handler)
        primary = self.windows.get(QGuiApplication.primaryScreen())
        if primary is not None:
            primary.activateWindow()  # so that Esc reaches it

    def close(self) -> None:
        if not self.open:
            return
        self.open = False
        logging.getLogger(STATS_LOGGER).removeHandler(self._handler)
        app = QGuiApplication.instance()
        app.screenAdded.disconnect(self._add)
        app.screenRemoved.disconnect(self._remove)
        for window in self.windows.values():
            window.log_cost()
            window.stop()
            window.close()
            window.deleteLater()
        self.windows.clear()
        self.closed.emit()

    def elapsed(self) -> float:
        return time.perf_counter() - self.started

    def _add(self, screen: QScreen) -> None:
        if screen in self.windows:
            return
        window = _ClockWindow(self, screen.name(), screen.refreshRate())
        if hasattr(window, "setScreen"):
            window.setScreen(screen)  # before the geometry: the screens have different scales
        window.setGeometry(screen.geometry())
        window.showFullScreen()
        window.start()
        self.windows[screen] = window

    def _remove(self, screen: QScreen) -> None:
        window = self.windows.pop(screen, None)
        if window is not None:
            window.stop()
            window.close()
            window.deleteLater()

    def _set_stats(self, line: str) -> None:
        self.stats = line
        for window in self.windows.values():
            window.update(window.text_rect)


def clock_text(seconds: float) -> str:
    """The clock: seconds and milliseconds, the minutes left out (a measurement takes seconds)."""
    ms = int(seconds * 1000)
    return f"{ms // 1000 % 100:02d}.{ms % 1000:03d}"


def short_stats(line: str) -> str:
    """A "Mirroring:" line cut to what fits under the clock: the frame rate, the age of the frames
    when sent (p50/p95) and the TV's lead. Anything else is shown whole."""
    found = _STATS.search(line)
    if not found:
        return line
    fps, asked, age, lead = found.groups()
    return f"{fps} fps (of {asked}) · age p50/p95 {age} ms · lead {lead} ms"


_STATS = re.compile(r"Mirroring: ([\d.]+) fps \(of (\d+)\).*?\bage ([\d.]+/[\d.]+) .*?lead (\d+) ms")


def tick_ms(refresh_rate: float) -> int:
    """How often a window repaints its clock: once a frame of its screen, never more often than
    every MIN_TICK_MS."""
    return max(MIN_TICK_MS, round(1000 / refresh_rate)) if refresh_rate > 0 else 16


class _ClockWindow(QWidget):
    def __init__(self, meter: LatencyMeter, screen_name: str, refresh_rate: float = 60.0) -> None:
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.meter = meter
        self.screen_name = screen_name
        self.setWindowTitle(tr("latency_meter"))
        self.setCursor(Qt.BlankCursor)
        self.setAttribute(Qt.WA_OpaquePaintEvent)  # every paint fills what it covers
        self.clock_rect = self.cells_rect = self.text_rect = QRect()
        self.tick_rect = QRect()
        self._covered = QRegion()
        self._text = clock_text(0)  # what the clock shows: set by the ticks, so that a paint shows one time
        self._tenth = 0
        self._glyphs: dict[str, QPixmap] = {}
        self._glyph_w = self._glyph_h = 0
        self._small = QFont()
        self.last_parts: set[str] = set()  # what the last paint drew, for the tests
        # What painting costs: totals, and the last whole second (shown on the window).
        self.paints = 0
        self.paint_seconds = 0.0
        self.rate = 0.0  # paints a second
        self.cost_ms = 0.0  # ms a second spent painting
        self._second_paints = 0
        self._second_seconds = 0.0
        self._second_start = time.perf_counter()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(tick_ms(refresh_rate))
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self._text = clock_text(self.meter.elapsed())
        self._tenth = int(self._text[3])
        self._second_start = time.perf_counter()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def log_cost(self) -> None:
        if self.paints:
            _LOGGER.info("Latency meter on %s: %d paints, every %d ms, %.2f ms each", self.screen_name, self.paints,
                         self._timer.interval(), self.paint_seconds * 1000 / self.paints)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt's name)
        if event.key() in (Qt.Key_Escape, Qt.Key_Space, Qt.Key_Q):
            self.meter.close()

    def mousePressEvent(self, _event) -> None:  # noqa: N802 (Qt's name)
        self.meter.close()

    def resizeEvent(self, _event) -> None:  # noqa: N802 (Qt's name)
        self._layout()

    def _layout(self) -> None:
        """The three parts and the clock's characters, drawn once at this size."""
        w, h = self.width(), self.height()
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        font.setBold(True)
        font.setPixelSize(max(12, min(int(h * 0.42), int(w / 4.2))))
        metrics = QFontMetrics(font)
        self._glyph_w = max(metrics.horizontalAdvance(c) for c in GLYPHS)
        self._glyph_h = metrics.height()
        ratio = self.devicePixelRatioF()
        self._glyphs = {}
        for c in GLYPHS:
            pix = QPixmap(round(self._glyph_w * ratio), round(self._glyph_h * ratio))
            pix.setDevicePixelRatio(ratio)
            pix.fill(Qt.black)
            p = QPainter(pix)
            p.setFont(font)
            p.setPen(TEXT)
            p.drawText(QRect(0, 0, self._glyph_w, self._glyph_h), Qt.AlignCenter, c)
            p.end()
            self._glyphs[c] = pix
        clock_w = self._glyph_w * len(clock_text(0))
        self.clock_rect = QRect((w - clock_w) // 2, int(h * 0.06 + (h * 0.5 - self._glyph_h) / 2), clock_w,
                                self._glyph_h)
        self.cells_rect = QRect(int(w * 0.05), int(h * 0.6), int(w * 0.9), int(h * 0.14))
        self.text_rect = QRect(int(w * 0.03), int(h * 0.76), int(w * 0.94), int(h * 0.045 * 4))
        self.tick_rect = self.clock_rect.united(self.cells_rect)  # all a tick can repaint
        self._covered = QRegion(self.clock_rect)
        for i in range(CELLS):
            self._covered += self._cell_rect(i)
        self._small = QFont(font)
        self._small.setBold(False)
        self._small.setPixelSize(max(10, int(h * 0.028)))

    def _glyph_rect(self, i: int) -> QRect:
        return QRect(self.clock_rect.x() + i * self._glyph_w, self.clock_rect.y(), self._glyph_w, self._glyph_h)

    def _cell_rect(self, i: int) -> QRect:
        """Whole pixels, so that a cell and the black around it never overlap."""
        r = self.cells_rect
        gap = r.width() / 90  # 1 % of the window (the cells take 90 %)
        cell = (r.width() - gap * (CELLS - 1)) / CELLS
        left, right = round(r.x() + i * (cell + gap)), round(r.x() + i * (cell + gap) + cell)
        return QRect(left, r.y(), right - left, r.height())

    def _tick(self) -> None:
        """Repaints what changed since the last frame: a few of the clock's characters, and two
        cells when the tenth changes. The rest stays on screen as it was."""
        text = clock_text(self.meter.elapsed())
        tenth = int(text[3])
        changed = QRegion()
        for i, (old, new) in enumerate(zip(self._text, text)):
            if old != new:
                changed += self._glyph_rect(i)
        if tenth != self._tenth:
            for i in (self._tenth, tenth):
                changed += self._cell_rect(i)
        self._text, self._tenth = text, tenth
        if not changed.isEmpty():
            self.update(changed)
        now = time.perf_counter()
        if now - self._second_start >= 1.0:
            seconds = now - self._second_start
            self.rate = self._second_paints / seconds
            self.cost_ms = self._second_seconds * 1000 / seconds
            self._second_paints, self._second_seconds, self._second_start = 0, 0.0, now
            self.update(self.text_rect)  # the cost line, once a second

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        start = time.perf_counter()
        area = event.region()
        p = QPainter(self)
        # Black where nothing else paints: the characters' pixmaps and the cells cover their own.
        for rect in area - self._covered:
            p.fillRect(rect, Qt.black)
        parts = set()
        if area.intersects(self.clock_rect):
            parts.add("clock")
            for i, c in enumerate(self._text):
                rect = self._glyph_rect(i)
                if area.intersects(rect):
                    p.drawPixmap(rect.topLeft(), self._glyphs[c])
        if area.intersects(self.cells_rect):
            parts.add("cells")
            for i in range(CELLS):
                rect = self._cell_rect(i)
                if area.intersects(rect):
                    p.fillRect(rect, LIT if i == self._tenth else DARK)
        if area.intersects(self.text_rect):
            parts.add("text")
            self._paint_text(p)
        p.end()
        self.last_parts = parts
        spent = time.perf_counter() - start
        self.paints += 1
        self.paint_seconds += spent
        self._second_paints += 1
        self._second_seconds += spent

    def _paint_text(self, p: QPainter) -> None:
        p.setFont(self._small)
        p.setPen(DIM)
        r = self.text_rect
        line = r.height() / 4
        p.drawText(QRectF(r.x(), r.y(), r.width(), 2 * line), Qt.AlignCenter | Qt.TextWordWrap, tr("latency_meter_hint"))
        cost = (f"{self.screen_name} · {self.rate:.0f} paints/s · {self.cost_ms:.1f} ms/s painting "
                f"(every {self._timer.interval()} ms)")
        texts = [cost, short_stats(self.meter.stats) or tr("latency_meter_no_stats")]
        for i, text in enumerate(texts):
            rect = QRectF(r.x(), r.y() + (2 + i) * line, r.width(), line)
            p.drawText(rect, Qt.AlignCenter, p.fontMetrics().elidedText(text, Qt.ElideRight, int(rect.width())))
