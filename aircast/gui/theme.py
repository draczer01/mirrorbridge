"""Icon drawing and the dark Fusion theme."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication

ACCENT = "#0a84ff"
STATE_COLORS = {"idle": ACCENT, "active": "#30d158", "busy": "#ff9f0a"}


def app_icon(state: str = "idle") -> QIcon:
    """A screen with the AirPlay triangle; colour shows the cast state in the tray."""
    icon = QIcon()
    color = QColor(STATE_COLORS.get(state, ACCENT))
    for size in (16, 20, 24, 32, 48, 64, 256):
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        s = size / 64.0
        pen = QPen(color, max(1.5, 6 * s))
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRect(int(5 * s), int(7 * s), int(54 * s), int(36 * s)), 6 * s, 6 * s)
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawPolygon([QPoint(int(32 * s), int(32 * s)), QPoint(int(14 * s), int(60 * s)), QPoint(int(50 * s), int(60 * s))])
        if state == "active":  # live dot
            p.setBrush(QColor("#ff453a"))
            r = max(3, int(9 * s))
            p.drawEllipse(QPoint(int(52 * s), int(12 * s)), r, r)
        p.end()
        icon.addPixmap(pix)
    return icon


def apply_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    base, alt, text = QColor(30, 30, 32), QColor(44, 44, 48), QColor(230, 230, 235)
    for role, color in {
        QPalette.Window: base, QPalette.WindowText: text, QPalette.Base: QColor(22, 22, 24),
        QPalette.AlternateBase: alt, QPalette.Text: text, QPalette.Button: alt,
        QPalette.ButtonText: text, QPalette.Highlight: QColor(ACCENT),
        QPalette.HighlightedText: QColor("white"), QPalette.ToolTipBase: alt,
        QPalette.ToolTipText: text, QPalette.PlaceholderText: QColor(140, 140, 150),
    }.items():
        pal.setColor(role, color)
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(110, 110, 115))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(110, 110, 115))
    pal.setColor(QPalette.Disabled, QPalette.WindowText, QColor(110, 110, 115))
    app.setPalette(pal)
    app.setStyleSheet(
        f"""
        QGroupBox {{ border: 1px solid #3a3a3f; border-radius: 8px; margin-top: 14px; padding: 10px; }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #a0a0aa; }}
        QListWidget {{ border: 1px solid #3a3a3f; border-radius: 8px; padding: 4px; }}
        QListWidget::item {{ padding: 8px; border-radius: 6px; }}
        QPushButton {{ padding: 6px 12px; border-radius: 6px; background: #2c2c30; border: 1px solid #3a3a3f; }}
        QPushButton:hover {{ background: #36363b; }}
        QPushButton:disabled {{ color: #6a6a70; }}
        QPushButton#cast {{ background: {ACCENT}; color: white; font-size: 15px; font-weight: 600;
                            padding: 10px; border: none; }}
        QPushButton#cast[casting="true"] {{ background: #ff453a; }}
        QPushButton#cast:disabled {{ background: #3a3a3f; color: #888; }}
        QPlainTextEdit {{ font-family: Consolas, monospace; font-size: 11px; border-radius: 6px; }}
        QMenu {{ background: #2a2a2e; border: 1px solid #3a3a3f; padding: 4px; }}
        QMenu::item {{ padding: 6px 24px 6px 24px; border-radius: 4px; }}
        QMenu::item:selected {{ background: {ACCENT}; color: white; }}
        QMenu::item:disabled {{ color: #8a8a90; }}
        QMenu::separator {{ height: 1px; background: #3a3a3f; margin: 4px 8px; }}
        """
    )
