"""PyInstaller entry point for AirCast.exe (a windowed app that also has a CLI)."""
import sys

SUBCOMMANDS = {"scan", "pair", "forget", "cast", "doctor"}
INFO_FLAGS = {"-h", "--help", "--version"}


def _wants_console(argv: list[str]) -> bool:
    """CLI use (`AirCast.exe doctor`, `AirCast.exe --help`), not the tray app."""
    return any(arg in SUBCOMMANDS or arg in INFO_FLAGS for arg in argv)


def _open_own_console() -> bool:
    """Give a CLI command a console window of its own.

    AirCast.exe is a windowed program, so cmd/PowerShell do not wait for it. Sharing
    their console would make the shell and AirCast read the keyboard at the same time,
    and PyInstaller's launcher process has no console to share anyway. When output is
    already redirected (`AirCast.exe doctor > out.txt`), that is used as it is.
    """
    if sys.stdout is not None:
        return False
    import ctypes

    if not ctypes.windll.kernel32.AllocConsole():
        return False
    ctypes.windll.kernel32.SetConsoleTitleW("AirCast")
    sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)  # noqa: SIM115
    sys.stderr = sys.stdout
    sys.stdin = open("CONIN$", "r", encoding="utf-8")  # noqa: SIM115
    return True


if __name__ == "__main__":
    own_console = sys.platform == "win32" and _wants_console(sys.argv[1:]) and _open_own_console()
    from aircast.cli import main

    try:
        code = main()
    except SystemExit as ex:
        code = ex.code
        if isinstance(code, str):  # SystemExit("message"): print it before the window closes
            print(code, file=sys.stderr)
            code = 1
    if own_console:
        try:
            input("\nPress Enter to close this window...")
        except (EOFError, KeyboardInterrupt):
            pass
    sys.exit(code)
