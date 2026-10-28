"""More options > Debug > Latency meter: a big clock on every screen, to photograph the PC's screen
and the TV together. The difference between the two clocks in the photo is the delay from screen
to TV, all of it: capture, encoding, the network and the TV's own decoding and display, which the
"Mirroring:" lines of the log (mirror/stats.py) cannot see.

One window per screen, so that the clock is also on the virtual screen of "Extend screen" (shown
only on the TV) and on a screen added while it is open. The tenths of a second are also shown as
a row of cells, readable when the digits blur in a photo. The last "Mirroring:" line of the log
is shown below, for the part the PC can measure.
"""

from __future__ import annotations

import logging
import re
import time

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QKeyEvent, QPainter, QScreen
from PySide6.QtWidgets import QWidget

from .i18n import tr

STATS_LOGGER = "mirrorbridge.mirror.stats"
CELLS = 10  # one per tenth of a second
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
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(1)  # each window repaints as often as its screen shows a new frame
        self._timer.timeout.connect(self._tick)
        self._handler = _StatsHandler(self.stats_line.emit)
        self.stats_line.connect(self._set_stats)

    def show(self) -> None:
        app = QGuiApplication.instance()
        for screen in QGuiApplication.screens():
            self._add(screen)
        app.screenAdded.connect(self._add)  # the virtual screen of "Extend screen", added by a cast
        app.screenRemoved.connect(self._remove)
        logging.getLogger(STATS_LOGGER).addHandler(self._handler)
        self._timer.start()

    def close(self) -> None:
        if not self._timer.isActive():
            return
        self._timer.stop()
        logging.getLogger(STATS_LOGGER).removeHandler(self._handler)
        app = QGuiApplication.instance()
        app.screenAdded.disconnect(self._add)
        app.screenRemoved.disconnect(self._remove)
        for window in self.windows.values():
            window.close()
            window.deleteLater()
        self.windows.clear()
        self.closed.emit()

    def elapsed(self) -> float:
        return time.perf_counter() - self.started

    def _add(self, screen: QScreen) -> None:
        if screen in self.windows:
            return
        window = _ClockWindow(self, screen.name())
        window.setGeometry(screen.geometry())
        window.showFullScreen()
        self.windows[screen] = window

    def _remove(self, screen: QScreen) -> None:
        window = self.windows.pop(screen, None)
        if window is not None:
            window.close()
            window.deleteLater()

    def _set_stats(self, line: str) -> None:
        self.stats = line

    def _tick(self) -> None:
        for window in self.windows.values():
            window.update()


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


class _ClockWindow(QWidget):
    def __init__(self, meter: LatencyMeter, screen_name: str) -> None:
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.meter = meter
        self.screen_name = screen_name
        self.frames = 0
        self.setWindowTitle(tr("latency_meter"))
        self.setCursor(Qt.BlankCursor)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt's name)
        if event.key() in (Qt.Key_Escape, Qt.Key_Space, Qt.Key_Q):
            self.meter.close()

    def mousePressEvent(self, _event) -> None:  # noqa: N802 (Qt's name)
        self.meter.close()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt's name)
        self.frames += 1
        seconds = self.meter.elapsed()
        w, h = self.width(), self.height()
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.black)

        # The clock, as wide as the screen allows.
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        font.setBold(True)
        font.setPixelSize(max(12, min(int(h * 0.42), int(w / 4.2))))
        p.setFont(font)
        p.setPen(TEXT)
        p.drawText(QRectF(0, h * 0.06, w, h * 0.5), Qt.AlignCenter, clock_text(seconds))

        # The tenths, as cells.
        tenth = int(seconds * 10) % CELLS
        gap = w * 0.01
        cell = (w * 0.9 - gap * (CELLS - 1)) / CELLS
        top, height = h * 0.6, h * 0.14
        for i in range(CELLS):
            p.fillRect(QRectF(w * 0.05 + i * (cell + gap), top, cell, height), LIT if i == tenth else DARK)

        small = QFont(font)
        small.setBold(False)
        small.setPixelSize(max(10, int(h * 0.028)))
        p.setFont(small)
        p.setPen(DIM)
        line = h * 0.045
        p.drawText(QRectF(w * 0.03, h * 0.76, w * 0.94, 2 * line), Qt.AlignCenter | Qt.TextWordWrap,
                   tr("latency_meter_hint"))
        texts = [f"{self.screen_name} · {self.frames}", short_stats(self.meter.stats) or tr("latency_meter_no_stats")]
        for i, text in enumerate(texts):
            rect = QRectF(w * 0.03, h * 0.76 + (2 + i) * line, w * 0.94, line)
            p.drawText(rect, Qt.AlignCenter, p.fontMetrics().elidedText(text, Qt.ElideRight, int(rect.width())))
        p.end()
