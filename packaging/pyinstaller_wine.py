"""Run PyInstaller under Wine.

Wine cannot pass inherited pipe handles to child processes the way PyInstaller's
isolated hook runner expects, so the children die. PyInstaller skips the subprocess
when it believes it already runs inside one, so the hooks run in this process instead.
"""
import sys

sys._pyi_isolated_subprocess = True  # pylint: disable=protected-access


def _reuse_qt_application() -> None:
    """Qt hooks each create a QCoreApplication; in one process only the first may exist."""
    try:
        from PySide6 import QtCore
    except ImportError:
        return
    original = QtCore.QCoreApplication

    def application(*args, **kwargs):
        return original.instance() or original(*args, **kwargs)

    QtCore.QCoreApplication = application


_reuse_qt_application()

from PyInstaller.__main__ import run  # noqa: E402

if __name__ == "__main__":
    sys.exit(run())
