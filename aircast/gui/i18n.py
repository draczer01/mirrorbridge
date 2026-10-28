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
    }
    for english, words in paths.items():
        text = text.replace(english, " > ".join(words))
    return text
