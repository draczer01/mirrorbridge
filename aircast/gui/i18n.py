"""User-facing strings in English and Spanish. The system language picks one."""

from __future__ import annotations

_STRINGS: dict[str, dict[str, str]] = {
    # Tray menu
    "open": {"en": "Open AirCast", "es": "Abrir AirCast"},
    "status_idle": {"en": "Not casting", "es": "Sin transmitir"},
    "status_casting": {"en": "Casting to {device}", "es": "Transmitiendo a {device}"},
    "status_busy": {"en": "{state}…", "es": "{state}…"},
    "start_to": {"en": "Start casting to {device}", "es": "Transmitir a {device}"},
    "start_no_device": {"en": "Start casting (choose a device)", "es": "Transmitir (elige un dispositivo)"},
    "stop": {"en": "Stop casting", "es": "Detener transmisión"},
    "device_menu": {"en": "Device", "es": "Dispositivo"},
    "searching": {"en": "Searching…", "es": "Buscando…"},
    "no_devices": {"en": "No devices found", "es": "No se encontraron dispositivos"},
    "refresh": {"en": "Search again", "es": "Buscar de nuevo"},
    "add_ip": {"en": "Add by IP address…", "es": "Agregar por dirección IP…"},
    "pair": {"en": "Pair with PIN…", "es": "Emparejar con PIN…"},
    "forget": {"en": "Forget pairing", "es": "Olvidar emparejamiento"},
    "source_menu": {"en": "What to cast", "es": "Qué transmitir"},
    "display_n": {"en": "Display {n}: {w}x{h}", "es": "Pantalla {n}: {w}x{h}"},
    "primary": {"en": " (main)", "es": " (principal)"},
    "window_menu": {"en": "A single window", "es": "Una sola ventana"},
    "no_windows": {"en": "No windows found", "es": "No hay ventanas"},
    "extend_screen": {"en": "Extend screen (virtual display)", "es": "Extender pantalla (pantalla virtual)"},
    "extend_size": {"en": "Extended screen size", "es": "Tamaño de la pantalla extendida"},
    "extend_hint": {
        "en": "The TV becomes a second screen: drag windows onto it. Needs a free driver from Parsec, "
              "installed the first time.",
        "es": "La TV pasa a ser una segunda pantalla: arrastra ventanas hacia ella. Usa un controlador "
              "gratuito de Parsec que se instala la primera vez.",
    },
    "vdd_remove": {"en": "Remove the virtual display driver", "es": "Quitar el controlador de pantalla virtual"},
    "audio": {"en": "Include computer sound", "es": "Incluir el sonido del equipo"},
    "cursor": {"en": "Show mouse pointer", "es": "Mostrar el puntero del mouse"},
    "quality_menu": {"en": "Quality", "es": "Calidad"},
    "resolution": {"en": "Resolution", "es": "Resolución"},
    "native": {"en": "Native", "es": "Nativa"},
    "framerate": {"en": "Frame rate", "es": "Cuadros por segundo"},
    "latency": {"en": "Delay", "es": "Retraso"},
    "lat_low": {"en": "Low (~3 s delay)", "es": "Bajo (~3 s de retraso)"},
    "lat_balanced": {"en": "Balanced (~4 s delay)", "es": "Equilibrado (~4 s de retraso)"},
    "lat_smooth": {"en": "Smooth (~7 s delay)", "es": "Fluido (~7 s de retraso)"},
    "bitrate": {"en": "Bitrate", "es": "Tasa de bits"},
    "encoder": {"en": "Encoder", "es": "Codificador"},
    "auto_encoder": {"en": "Automatic (best available)", "es": "Automático (el mejor disponible)"},
    "play_file": {"en": "Play a video file…", "es": "Reproducir un archivo de video…"},
    "autostart": {"en": "Start with Windows", "es": "Iniciar con Windows"},
    "desktop_shortcut": {"en": "Create desktop shortcut", "es": "Crear acceso directo en el escritorio"},
    "shortcut_done": {"en": "Shortcut created on your desktop.", "es": "Acceso directo creado en el escritorio."},
    "shortcut_description": {"en": "AirCast - cast your screen to Apple TV", "es": "AirCast - transmite tu pantalla al Apple TV"},
    "more": {"en": "More options", "es": "Más opciones"},
    "log_folder": {"en": "Open log folder", "es": "Abrir carpeta de registros"},
    "quit": {"en": "Quit", "es": "Salir"},
    # Notifications
    "tray_hint": {
        "en": "AirCast is running here. Click the icon for the menu.",
        "es": "AirCast está aquí. Haz clic en el icono para ver el menú.",
    },
    "already_running": {
        "en": "AirCast is already running. Use its icon next to the clock.",
        "es": "AirCast ya se está ejecutando. Usa su icono junto al reloj.",
    },
    "hidden_hint": {
        "en": "AirCast keeps running in the tray. Use Quit in its menu to close it.",
        "es": "AirCast sigue en la bandeja. Usa Salir en su menú para cerrarlo.",
    },
    "cast_started": {"en": "Casting to {device}", "es": "Transmitiendo a {device}"},
    "cast_stopped": {"en": "Casting stopped", "es": "Transmisión detenida"},
    "cast_failed": {"en": "Casting failed", "es": "La transmisión falló"},
    "pair_title": {"en": "Pair with {device}", "es": "Emparejar con {device}"},
    "pair_prompt": {
        "en": "Enter the 4-digit PIN shown on your TV:",
        "es": "Escribe el PIN de 4 dígitos que aparece en tu TV:",
    },
    "pair_show_pin": {
        "en": "Enter this PIN on the receiver:\n\n{pin}",
        "es": "Escribe este PIN en el receptor:\n\n{pin}",
    },
    "pairing": {"en": "Pairing with {device}… look at the TV for a PIN.", "es": "Emparejando con {device}… mira el PIN en la TV."},
    "paired": {"en": "Paired with {device}", "es": "Emparejado con {device}"},
    "pair_failed": {"en": "Pairing failed", "es": "El emparejamiento falló"},
    "pair_failed_hint": {
        "en": "{error}\n\nOn an Apple TV, check Settings > AirPlay and HomeKit > Allow Access.",
        "es": "{error}\n\nEn un Apple TV, revisa Ajustes > AirPlay y HomeKit > Permitir acceso.",
    },
    "password_title": {"en": "AirPlay password", "es": "Contraseña de AirPlay"},
    "password_prompt": {"en": "Password for {device}:", "es": "Contraseña de {device}:"},
    "add_ip_prompt": {"en": "Receiver IP address (e.g. 192.168.1.33):", "es": "Dirección IP del receptor (ej. 192.168.1.33):"},
    "choose_video": {"en": "Choose a video", "es": "Elige un video"},
    "playing_file": {"en": "Playing {file} on {device}", "es": "Reproduciendo {file} en {device}"},
    "playback_done": {"en": "Playback finished", "es": "Reproducción terminada"},
    "playback_failed": {"en": "Playback failed", "es": "La reproducción falló"},
    "found_n": {"en": "Found {n} device(s)", "es": "Se encontraron {n} dispositivo(s)"},
    "found_none": {
        "en": "No devices found. Check you're on the same network, or add one by IP.",
        "es": "No se encontraron dispositivos. Revisa que estén en la misma red o agrega uno por IP.",
    },
    "scan_failed": {"en": "Search failed: {error}", "es": "La búsqueda falló: {error}"},
    "ffmpeg_missing": {"en": "FFmpeg problem", "es": "Problema con FFmpeg"},
    # Extend screen: virtual display driver
    "vdd_install_title": {"en": "Install the virtual screen", "es": "Instalar la pantalla virtual"},
    "vdd_install_text": {
        "en": "To extend your desktop to the TV, AirCast uses the free \"Parsec Virtual Display Driver\" "
              "from Parsec (Unity). AirCast will download it from parsec.app (about 500 KB) and Windows "
              "will ask for your permission once. The virtual screen only exists while you cast. By "
              "installing it you accept Parsec's terms.",
        "es": "Para extender tu escritorio a la TV, AirCast usa el controlador gratuito \"Parsec Virtual "
              "Display Driver\" de Parsec (Unity). AirCast lo descargará de parsec.app (unos 500 KB) y "
              "Windows te pedirá permiso una vez. La pantalla virtual solo existe mientras transmites. "
              "Al instalarlo aceptas los términos de Parsec.",
    },
    "vdd_terms": {"en": "Parsec's terms", "es": "Términos de Parsec"},
    "vdd_install": {"en": "Install", "es": "Instalar"},
    "vdd_not_now": {"en": "Not now", "es": "Ahora no"},
    "vdd_reinstall": {"en": "Reinstall", "es": "Reinstalar"},
    "vdd_open_devmgmt": {"en": "Open Device Manager", "es": "Abrir el Administrador de dispositivos"},
    "vdd_downloading": {"en": "Downloading the driver… {pct}%", "es": "Descargando el controlador… {pct}%"},
    "vdd_asking": {
        "en": "Windows will ask for permission to install the driver…",
        "es": "Windows te pedirá permiso para instalar el controlador…",
    },
    "vdd_installing": {"en": "Installing the driver…", "es": "Instalando el controlador…"},
    "vdd_finishing": {"en": "Waiting for the driver to start…", "es": "Esperando a que arranque el controlador…"},
    "vdd_ready": {"en": "Virtual screen ready", "es": "Pantalla virtual lista"},
    "vdd_ready_hint": {
        "en": "Start casting and the TV becomes a second screen.",
        "es": "Empieza a transmitir y la TV será una segunda pantalla.",
    },
    "vdd_uac_cancelled": {
        "en": "Not installed: you cancelled the Windows permission request.",
        "es": "No se instaló: cancelaste la solicitud de permiso de Windows.",
    },
    "vdd_restart_to_finish": {
        "en": "Restart Windows to finish the installation.",
        "es": "Reinicia Windows para terminar la instalación.",
    },
    "vdd_enable_hint": {
        "en": "To turn it on, open Device Manager, go to Display adapters, right-click Parsec Virtual "
              "Display Adapter and choose Enable device.",
        "es": "Para activarla, abre el Administrador de dispositivos, ve a Adaptadores de pantalla, haz "
              "clic derecho en Parsec Virtual Display Adapter y elige Habilitar dispositivo.",
    },
    "vdd_no_answer": {
        "en": "The driver did not respond. Restart Windows or reinstall it.",
        "es": "El controlador no respondió. Reinicia Windows o reinstálalo.",
    },
    "vdd_download_failed": {
        "en": "Could not download the driver. Check the internet connection, or install it another way.",
        "es": "No se pudo descargar el controlador. Revisa la conexión a internet o instálalo de otra forma.",
    },
    "vdd_mismatch": {
        "en": "The downloaded file is not the expected driver installer, so AirCast did not run it.",
        "es": "El archivo descargado no es el instalador esperado, así que AirCast no lo ejecutó.",
    },
    "vdd_install_failed": {"en": "Could not install the driver", "es": "No se pudo instalar el controlador"},
    "vdd_installer_error": {
        "en": "Windows could not run the installer: {error}",
        "es": "Windows no pudo ejecutar el instalador: {error}",
    },
    "vdd_check_failed": {
        "en": "Could not check the virtual screen driver.",
        "es": "No se pudo comprobar el controlador de pantalla virtual.",
    },
    "vdd_unsupported": {
        "en": "The virtual screen needs Windows 10 or 11.",
        "es": "La pantalla virtual necesita Windows 10 u 11.",
    },
    "vdd_extend_off": {
        "en": "Extend screen is off for now, so Start casting sends your display. Choose it again once "
              "the driver works.",
        "es": "Extender pantalla quedó desactivado, así que Transmitir enviará tu pantalla. Vuelve a "
              "elegirlo cuando el controlador funcione.",
    },
    "vdd_winget": {"en": "Install with winget", "es": "Instalar con winget"},
    "vdd_manual": {"en": "Download manually", "es": "Descargar manualmente"},
    "vdd_choose_again": {
        "en": "When the installation finishes, choose Extend screen again.",
        "es": "Cuando termine la instalación, vuelve a elegir Extender pantalla.",
    },
    "vdd_winget_missing": {
        "en": "winget is not available on this PC. Use Download manually instead.",
        "es": "winget no está disponible en este equipo. Usa Descargar manualmente.",
    },
    "vdd_winget_missing_only": {
        "en": "winget is not available on this PC, so the driver cannot be installed from here.",
        "es": "winget no está disponible en este equipo, así que no se puede instalar el controlador desde aquí.",
    },
    "vdd_remove_hint": {
        "en": "Look for \"Parsec Virtual Display Driver\" in the list and choose Uninstall.",
        "es": "Busca \"Parsec Virtual Display Driver\" en la lista y elige Desinstalar.",
    },
    "vdd_st_missing": {
        "en": "The virtual screen driver is not installed.",
        "es": "El controlador de pantalla virtual no está instalado.",
    },
    "vdd_st_disabled": {
        "en": "The virtual screen is turned off in Windows.",
        "es": "La pantalla virtual está desactivada en Windows.",
    },
    "vdd_st_restart": {
        "en": "Restart Windows to use the virtual screen.",
        "es": "Reinicia Windows para usar la pantalla virtual.",
    },
    "vdd_st_broken": {
        "en": "The virtual screen driver is not responding.",
        "es": "El controlador de pantalla virtual no responde.",
    },
    "vdd_add_failed": {
        "en": "Windows did not allow another virtual screen.",
        "es": "Windows no permitió crear otra pantalla virtual.",
    },
    "vdd_not_attached": {
        "en": "Windows did not turn on the virtual screen. Press Win+P, choose \"Extend\" and try again.",
        "es": "Windows no activó la pantalla virtual. Pulsa Win+P, elige \"Extender\" y vuelve a intentarlo.",
    },
    "vdd_lost": {
        "en": "The virtual screen closed (for example, when the PC went to sleep). Start casting again.",
        "es": "La pantalla virtual se cerró (por ejemplo, al suspender el equipo). Vuelve a iniciar la "
              "transmisión.",
    },
    "extend_tip": {
        "en": "Drag windows past the right edge of your screen to see them on the TV.",
        "es": "Arrastra ventanas hacia la derecha de tu pantalla para verlas en la TV.",
    },
    "close": {"en": "Close", "es": "Cerrar"},
    # Device status
    "st_paired": {"en": "Paired", "es": "Emparejado"},
    "st_ready": {"en": "Ready", "es": "Listo"},
    "st_needs_pin": {"en": "Needs PIN", "es": "Necesita PIN"},
    "st_audio_only": {"en": "Audio only", "es": "Solo audio"},
    "st_restricted": {"en": "AirPlay restricted", "es": "AirPlay restringido"},
    # Main window
    "window_title": {"en": "AirCast - AirPlay for Windows", "es": "AirCast - AirPlay para Windows"},
    "devices": {"en": "AirPlay devices", "es": "Dispositivos AirPlay"},
    "entire_display": {"en": "Entire display", "es": "Pantalla completa"},
    "single_window": {"en": "Single window", "es": "Una ventana"},
    "start": {"en": "Start casting", "es": "Transmitir"},
    "select_device": {"en": "Select a device to start.", "es": "Elige un dispositivo para empezar."},
    "log": {"en": "Activity", "es": "Actividad"},
    "status_playing_file": {"en": "Playing a video on {device}", "es": "Reproduciendo un video en {device}"},
    "window_moved_hint": {
        "en": "The window's current spot on screen is captured. If you move it, stop and start again.",
        "es": "Se captura el lugar que ocupa la ventana ahora. Si la mueves, detén y vuelve a transmitir.",
    },
    # Cast states
    "state_idle": {"en": "Idle", "es": "Inactivo"},
    "state_starting": {"en": "Starting the encoder", "es": "Iniciando el codificador"},
    "state_extending": {"en": "Creating the virtual screen", "es": "Creando la pantalla virtual"},
    "state_buffering": {"en": "Preparing video", "es": "Preparando el video"},
    "state_connecting": {"en": "Connecting to the TV", "es": "Conectando con la TV"},
    "state_casting": {"en": "Casting", "es": "Transmitiendo"},
    "state_stopping": {"en": "Stopping", "es": "Deteniendo"},
    "state_stopped": {"en": "Stopped", "es": "Detenido"},
    "state_failed": {"en": "Failed", "es": "Falló"},
}

_lang = "en"


def set_language(code: str) -> None:
    global _lang  # pylint: disable=global-statement
    _lang = "es" if code.lower().startswith("es") else "en"


def language() -> str:
    return _lang


def tr(key: str, **kwargs) -> str:
    entry = _STRINGS.get(key)
    text = (entry.get(_lang) or entry["en"]) if entry else key
    return text.format(**kwargs) if kwargs else text


def state_text(state) -> str:
    """Localized name of a session.State."""
    return tr("state_" + state.name.lower())


def _item(key: str) -> str:
    """A menu item's name without its "…" or "(~7 s delay)"."""
    return tr(key).split(" (")[0].rstrip("…")


def menu_paths(text: str) -> str:
    """Name the menu items that a cast's (English) error mentions as the menu does."""
    paths = {
        "Device > Forget pairing": (_item("device_menu"), _item("forget")),
        "Device > Pair with PIN": (_item("device_menu"), _item("pair")),
        "Quality > Delay > Smooth": (_item("quality_menu"), _item("latency"), _item("lat_smooth")),
        "Quality > Encoder > x264 (CPU)": (_item("quality_menu"), _item("encoder"), "x264 (CPU)"),
        "What to cast > Extended screen size > 720p": (_item("source_menu"), _item("extend_size"), "720p"),
    }
    for english, words in paths.items():
        text = text.replace(english, " > ".join(words))
    return text
