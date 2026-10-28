"""User-facing strings in every language the app speaks. English unless the user picks another.

Each string keeps its languages side by side, so a new one is written in all of them at once and
the tests can check that each takes the same {values} as its English. Another language needs its
entry in LANGUAGES and its words under each key; a key it lacks is shown in English.
"""

from __future__ import annotations

from ..text import Text

# The languages the app speaks, in the order the Language menu lists them: code -> its own name.
LANGUAGES: dict[str, str] = {"en": "English", "es": "Español"}
# Every string is written in English first: the default, and the words any other language lacks.
DEFAULT = "en"

_STRINGS: dict[str, dict[str, str]] = {
    # Tray menu
    "open": {"en": "Open MirrorBridge", "es": "Abrir MirrorBridge"},
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
    "shortcut_description": {
        "en": "MirrorBridge - cast your screen to the TV with AirPlay",
        "es": "MirrorBridge - transmite tu pantalla a la TV con AirPlay",
    },
    "more": {"en": "More options", "es": "Más opciones"},
    "log_folder": {"en": "Open log folder", "es": "Abrir carpeta de registros"},
    "probe": {"en": "Test this TV…", "es": "Probar esta TV…"},
    "language_menu": {"en": "Language", "es": "Idioma"},
    "quit": {"en": "Quit", "es": "Salir"},
    # Notifications
    "tray_hint": {
        "en": "MirrorBridge is running here. Click the icon for the menu.",
        "es": "MirrorBridge está aquí. Haz clic en el icono para ver el menú.",
    },
    "already_running": {
        "en": "MirrorBridge is already running. Use its icon next to the clock.",
        "es": "MirrorBridge ya se está ejecutando. Usa su icono junto al reloj.",
    },
    "hidden_hint": {
        "en": "MirrorBridge keeps running in the tray. Use Quit in its menu to close it.",
        "es": "MirrorBridge sigue en la bandeja. Usa Salir en su menú para cerrarlo.",
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
    # More options > Test this TV
    "probe_title": {"en": "Test {device}", "es": "Probar {device}"},
    "probe_intro": {
        "en": "MirrorBridge will try three ways of sending {device} a video, one after the other, for about "
              "two minutes in all. Watch the TV: it may show Apple's test video, then MirrorBridge's test "
              "pattern. If the TV asks whether to allow MirrorBridge, choose Allow. Nothing from this PC's "
              "screen is sent.\n\nAt the end MirrorBridge says what worked, and where the report is: please "
              "send that report with your bug report.",
        "es": "MirrorBridge probará tres formas de enviar un video a {device}, una tras otra, durante unos dos "
              "minutos en total. Mira la TV: puede mostrar el video de prueba de Apple y luego la imagen de "
              "prueba de MirrorBridge. Si la TV pregunta si permite MirrorBridge, elige Permitir. No se envía "
              "nada de la pantalla de este equipo.\n\nAl final, MirrorBridge dice qué funcionó y dónde está "
              "el informe: mándalo con tu reporte del problema.",
    },
    "probe_start": {"en": "Start the test", "es": "Empezar la prueba"},
    "status_probing": {"en": "Testing {device}: {step}…", "es": "Probando {device}: {step}…"},
    "probe_step": {"en": "test {number} of {total}", "es": "prueba {number} de {total}"},
    "probe_done": {"en": "Test of {device} finished", "es": "Terminó la prueba de {device}"},
    "probe_failed_title": {"en": "The test failed", "es": "La prueba falló"},
    # Extend screen: virtual display driver
    "vdd_install_title": {"en": "Install the virtual screen", "es": "Instalar la pantalla virtual"},
    "vdd_install_text": {
        "en": "To extend your desktop to the TV, MirrorBridge uses the free \"Parsec Virtual Display Driver\" "
              "from Parsec (Unity). MirrorBridge will download it from parsec.app (about 500 KB) and Windows "
              "will ask for your permission once. The virtual screen only exists while you cast. By "
              "installing it you accept Parsec's terms.",
        "es": "Para extender tu escritorio a la TV, MirrorBridge usa el controlador gratuito \"Parsec Virtual "
              "Display Driver\" de Parsec (Unity). MirrorBridge lo descargará de parsec.app (unos 500 KB) y "
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
        "en": "The downloaded file is not the expected driver installer, so MirrorBridge did not run it.",
        "es": "El archivo descargado no es el instalador esperado, así que MirrorBridge no lo ejecutó.",
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
    "ok": {"en": "OK", "es": "Aceptar"},
    "cancel": {"en": "Cancel", "es": "Cancelar"},
    # Device status
    "st_paired": {"en": "Paired", "es": "Emparejado"},
    "st_ready": {"en": "Ready", "es": "Listo"},
    "st_needs_pin": {"en": "Needs PIN", "es": "Necesita PIN"},
    "st_audio_only": {"en": "Audio only", "es": "Solo audio"},
    "st_restricted": {"en": "AirPlay restricted", "es": "AirPlay restringido"},
    # Main window
    "window_title": {"en": "MirrorBridge - AirPlay for Windows", "es": "MirrorBridge - AirPlay para Windows"},
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
    # What the core modules say (mirrorbridge.text.Text), with the same {values}. The English is
    # the core module's own; {menu_...} is a menu path, as the menu names it (_menu_paths).
    "checking_ffmpeg": {"en": "Checking FFmpeg and encoders", "es": "Comprobando FFmpeg y los codificadores"},
    "encoder_via": {"en": "{encoder} via {backend}", "es": "{encoder} con {backend}"},
    "ffmpeg_not_found": {
        "en": "FFmpeg was not found. Use the MirrorBridge.exe build (FFmpeg included), run "
              "'pip install imageio-ffmpeg', or put ffmpeg.exe next to MirrorBridge.",
        "es": "No se encontró FFmpeg. Usa la versión MirrorBridge.exe (trae FFmpeg), ejecuta "
              "'pip install imageio-ffmpeg' o pon ffmpeg.exe junto a MirrorBridge.",
    },
    "no_encoder": {
        "en": "No working H.264 encoder found in this FFmpeg build",
        "es": "Esta versión de FFmpeg no tiene ningún codificador H.264 que funcione",
    },
    "ffmpeg_exited": {
        "en": "FFmpeg exited (code {code}):\n{errors}",
        "es": "FFmpeg se cerró (código {code}):\n{errors}",
    },
    "ffmpeg_no_video": {
        "en": "FFmpeg produced no video within {seconds}s:\n{errors}",
        "es": "FFmpeg no produjo video en {seconds} s:\n{errors}",
    },
    "encoder_stopped": {
        "en": "Encoder stopped unexpectedly:\n{errors}",
        "es": "El codificador se detuvo de forma inesperada:\n{errors}",
    },
    "window_hidden": {
        "en": "The window \"{title}\" is not on screen. Restore it if it is minimized, or pick another one.",
        "es": "La ventana \"{title}\" no está en la pantalla. Restáurala si está minimizada, o elige otra.",
    },
    "window_off_screen": {
        "en": "The window \"{title}\" is off screen.",
        "es": "La ventana \"{title}\" está fuera de la pantalla.",
    },
    "window_too_small": {
        "en": "The window \"{title}\" is too small to cast.",
        "es": "La ventana \"{title}\" es demasiado pequeña para transmitirla.",
    },
    "connect_failed": {
        "en": "Could not connect to {device}: {error}",
        "es": "No se pudo conectar con {device}: {error}",
    },
    "pair_cancelled": {"en": "Pairing cancelled", "es": "Emparejamiento cancelado"},
    "pin_refused": {"en": "The device did not accept the PIN", "es": "El dispositivo no aceptó el PIN"},
    "powershell_failed": {"en": "PowerShell failed", "es": "PowerShell falló"},
    # Screen mirroring (mirrorbridge.mirror)
    "mirror_connect_failed": {
        "en": "Could not connect to {device} ({address}:{port}): {error}. Check that the TV is on and on the same "
              "network as this PC.",
        "es": "No se pudo conectar con {device} ({address}:{port}): {error}. Revisa que la TV esté encendida y en "
              "la misma red que este equipo.",
    },
    "mirror_verify_refused": {
        "en": "{device} did not accept MirrorBridge's pairing (pair-verify: {reason}). Use {menu_forget}, then cast "
              "again and type the new PIN the TV shows.",
        "es": "{device} no aceptó el emparejamiento de MirrorBridge (pair-verify: {reason}). Usa {menu_forget}, "
              "vuelve a transmitir y escribe el PIN nuevo que muestra la TV.",
    },
    "mirror_not_paired": {
        "en": "MirrorBridge is not paired with {device}. Use {menu_pair}, then try again.",
        "es": "MirrorBridge no está emparejado con {device}. Usa {menu_pair} y vuelve a intentarlo.",
    },
    "mirror_no_info": {
        "en": "{device} did not say what it can show (GET /info: {answer}).",
        "es": "{device} no dijo qué puede mostrar (GET /info: {answer}).",
    },
    "mirror_fairplay": {
        "en": "{device} needs FairPlay for screen mirroring (an Apple TV or a Mac), which MirrorBridge does not "
              "have yet. Cast to it as usual instead.",
        "es": "{device} necesita FairPlay para duplicar la pantalla (es un Apple TV o un Mac), y MirrorBridge "
              "todavía no lo tiene. Transmite como siempre en su lugar.",
    },
    "mirror_no_timing": {
        "en": "The TV never asked MirrorBridge for the time (UDP port {port}), so it cannot show the picture. "
              "Allow MirrorBridge, or inbound UDP ports {ports}, in Windows Firewall on private networks, then "
              "try again.",
        "es": "La TV nunca le pidió la hora a MirrorBridge (puerto UDP {port}), así que no puede mostrar la "
              "imagen. Permite MirrorBridge, o los puertos UDP de entrada {ports}, en el Firewall de Windows "
              "para redes privadas y vuelve a intentarlo.",
    },
    "mirror_password": {
        "en": "The TV asks for an AirPlay password (HTTP 401), which MirrorBridge does not send for screen "
              "mirroring yet. Turn off the AirPlay password in the TV's AirPlay settings, then try again.",
        "es": "La TV pide una contraseña de AirPlay (HTTP 401), y MirrorBridge todavía no la envía al duplicar "
              "la pantalla. Desactiva la contraseña en los ajustes de AirPlay de la TV y vuelve a intentarlo.",
    },
    "mirror_no_answer": {
        "en": "The TV stopped answering while screen mirroring was set up ({steps}). Check that the PC and the TV "
              "are on the same network, then try again.",
        "es": "La TV dejó de responder mientras se preparaba la duplicación de pantalla ({steps}). Revisa que el "
              "equipo y la TV estén en la misma red y vuelve a intentarlo.",
    },
    "mirror_refused": {
        "en": "The TV turned down screen mirroring ({steps}). Please report this TV, with mirrorbridge.log.",
        "es": "La TV rechazó la duplicación de pantalla ({steps}). Avisa de esta TV y manda mirrorbridge.log.",
    },
    "mirror_no_data_port": {
        "en": "The TV accepted screen mirroring but named no port for the video ({steps}). Please report this "
              "TV, with mirrorbridge.log.",
        "es": "La TV aceptó la duplicación de pantalla, pero no indicó ningún puerto para el video ({steps}). "
              "Avisa de esta TV y manda mirrorbridge.log.",
    },
    "mirror_no_ptp": {
        "en": "The TV wants PTP timing, which MirrorBridge does not have yet.",
        "es": "La TV quiere sincronizar la hora por PTP, y MirrorBridge todavía no lo tiene.",
    },
    "mirror_data_refused": {
        "en": "The TV did not take the video connection (TCP port {port}: {answer}).",
        "es": "La TV no aceptó la conexión del video (puerto TCP {port}: {answer}).",
    },
    "mirror_data_closed": {
        "en": "The TV closed the video connection after {frames} frames. It may not take this video: please "
              "report this TV, with mirrorbridge.log.",
        "es": "La TV cerró la conexión del video después de {frames} fotogramas. Puede que no acepte este "
              "video: avisa de esta TV y manda mirrorbridge.log.",
    },
    "mirror_video_stalled": {
        "en": "The TV took none of the video for {seconds} s. Check the network between this PC and the TV (the "
              "Wi-Fi signal, or use a cable), then try again.",
        "es": "La TV no tomó nada del video durante {seconds} s. Revisa la red entre este equipo y la TV (la señal "
              "Wi-Fi, o usa un cable) y vuelve a intentarlo.",
    },
    "mirror_no_picture": {
        "en": "No picture came from the screen capture or the encoder ({encoder}) for {seconds} s.{errors}",
        "es": "No llegó ninguna imagen de la captura de pantalla ni del codificador ({encoder}) durante {seconds} s."
              "{errors}",
    },
    "mirror_no_frames": {
        "en": "No video went to {device}: FFmpeg made no frame in {seconds} s.",
        "es": "No se mandó video a {device}: FFmpeg no hizo ningún fotograma en {seconds} s.",
    },
    "mirror_retry_trailer": {
        "en": "The TV dropped the video at once: trying again with the other ending of the H.264 header",
        "es": "La TV cortó el video enseguida: se vuelve a intentar con el otro final de la cabecera H.264",
    },
    "mirror_tv_ended": {"en": "The TV ended screen mirroring", "es": "La TV terminó la duplicación de pantalla"},
    "mirror_time_up": {
        "en": "Mirrored for {seconds} s, as asked",
        "es": "Pantalla duplicada durante {seconds} s, como se pidió",
    },
    "mirror_what_test": {"en": "the test pattern", "es": "la imagen de prueba"},
    "mirror_what_screen": {"en": "this PC's screen", "es": "la pantalla de este equipo"},
    "mirror_casting": {
        "en": "Mirroring {what} to {device} ({width}x{height}, {fps} fps)",
        "es": "Duplicando {what} en {device} ({width}x{height}, {fps} fps)",
    },
    "mirror_probe_works": {
        "en": "Screen mirroring works with {device}: {how}.",
        "es": "La duplicación de pantalla funciona con {device}: {how}.",
    },
    "mirror_probe_failed": {
        "en": "Screen mirroring did not work with {device}. Please report this TV, with mirrorbridge.log.",
        "es": "La duplicación de pantalla no funcionó con {device}. Avisa de esta TV y manda mirrorbridge.log.",
    },
    # The TV did not play the video
    "tv_password": {
        "en": "The TV asks for an AirPlay password (HTTP 401), which MirrorBridge cannot send with video it "
              "plays from a URL. Turn off the AirPlay password in the TV's AirPlay settings, then try again.",
        "es": "La TV pide una contraseña de AirPlay (HTTP 401), y MirrorBridge no puede enviarla con un video "
              "que reproduce desde una URL. Desactiva la contraseña en los ajustes de AirPlay de la TV y "
              "vuelve a intentarlo.",
    },
    "tv_pairing_refused": {
        "en": "The TV no longer accepts MirrorBridge's pairing (HTTP 403). Use {menu_forget}, then cast again "
              "and type the new PIN the TV shows.",
        "es": "La TV ya no acepta el emparejamiento de MirrorBridge (HTTP 403). Usa {menu_forget}, vuelve a "
              "transmitir y escribe el PIN nuevo que muestra la TV.",
    },
    "tv_busy": {
        "en": "The TV is busy with another AirPlay sender (HTTP 453). Stop AirPlay on the other phone, tablet "
              "or computer, then try again.",
        "es": "La TV está ocupada con otro equipo que le envía AirPlay (HTTP 453). Detén AirPlay en el otro "
              "teléfono, tableta o computadora y vuelve a intentarlo.",
    },
    "tv_restricted": {
        "en": "AirPlay access is restricted on the TV (HTTP 470). In the TV's AirPlay settings, allow everyone "
              "on the same network, then pair again.",
        "es": "El acceso a AirPlay está restringido en la TV (HTTP 470). En los ajustes de AirPlay de la TV, "
              "permite el acceso a todos en la misma red y vuelve a emparejar.",
    },
    "tv_no_answer": {
        "en": "The TV did not answer the request to play the video ({steps}). Check that the PC and the TV "
              "are on the same network, then try again.",
        "es": "La TV no contestó al pedido de reproducir el video ({steps}). Revisa que el equipo y la TV "
              "estén en la misma red y vuelve a intentarlo.",
    },
    "apple_tv_failed": {
        "en": "The Apple TV could not start the video ({steps}). Try again, or restart the Apple TV.",
        "es": "El Apple TV no pudo iniciar el video ({steps}). Vuelve a intentarlo o reinicia el Apple TV.",
    },
    "tv_no_url_video": {
        "en": "This TV accepts AirPlay pairing but refused video sent from a URL ({steps}). It may only take "
              "screen mirroring and audio from Apple devices.",
        "es": "Esta TV acepta el emparejamiento de AirPlay, pero rechazó el video enviado desde una URL "
              "({steps}). Puede que solo acepte la duplicación de pantalla y el audio de equipos Apple.",
    },
    "tv_credentials": {
        "en": "The TV rejected MirrorBridge's credentials ({steps}). Use {menu_forget}, then cast again and "
              "type the new PIN the TV shows.{password}",
        "es": "La TV rechazó las credenciales de MirrorBridge ({steps}). Usa {menu_forget}, vuelve a "
              "transmitir y escribe el PIN nuevo que muestra la TV.{password}",
    },
    "tv_also_password": {
        "en": " The TV also asks for an AirPlay password, which MirrorBridge cannot send with video it plays "
              "from a URL: turn it off in the TV's AirPlay settings.",
        "es": " La TV además pide una contraseña de AirPlay, que MirrorBridge no puede enviar con un video "
              "que reproduce desde una URL: desactívala en los ajustes de AirPlay de la TV.",
    },
    "tv_setup_failed": {
        "en": "The TV did not finish setting up the AirPlay session ({steps}). {advice}",
        "es": "La TV no terminó de preparar la sesión de AirPlay ({steps}). {advice}",
    },
    "tv_pair_first": {
        "en": "The TV wants MirrorBridge to pair first: use {menu_pair}, then cast again.",
        "es": "La TV pide que MirrorBridge se empareje primero: usa {menu_pair} y vuelve a transmitir.",
    },
    "tv_pairing_dropped": {
        "en": "Most often the TV has dropped MirrorBridge's pairing: use {menu_forget}, then cast again and "
              "type the new PIN the TV shows.",
        "es": "Casi siempre es que la TV olvidó el emparejamiento con MirrorBridge: usa {menu_forget}, "
              "vuelve a transmitir y escribe el PIN nuevo que muestra la TV.",
    },
    "tv_error": {"en": "Receiver error {code} ({domain})", "es": "Error del receptor {code} ({domain})"},
    "tv_never_played": {
        "en": "The receiver never started playing. Check that Windows Firewall allows MirrorBridge and that "
              "both devices are on the same network.",
        "es": "El receptor nunca empezó a reproducir. Revisa que el Firewall de Windows permita MirrorBridge "
              "y que los dos equipos estén en la misma red.",
    },
    "tv_wrong_path": {
        "en": "The TV reached MirrorBridge but asked for {path}, which MirrorBridge does not serve, instead of "
              "the stream at {url}. Please report this TV, with mirrorbridge.log.",
        "es": "La TV llegó a MirrorBridge, pero pidió {path}, que MirrorBridge no ofrece, en lugar del video "
              "en {url}. Avisa de esta TV y manda mirrorbridge.log.",
    },
    "tv_not_fetched": {
        "en": "The TV accepted the video but never downloaded it from {url}. Allow MirrorBridge (or inbound "
              "TCP port {port}) in Windows Firewall on private networks, and check that the PC and the TV "
              "are on the same network.{others}",
        "es": "La TV aceptó el video, pero nunca lo descargó de {url}. Permite MirrorBridge (o el puerto TCP "
              "de entrada {port}) en el Firewall de Windows para redes privadas, y revisa que el equipo y la "
              "TV estén en la misma red.{others}",
    },
    "tv_no_request": {
        "en": "The TV did not start the video within {seconds} s and never asked for it at {url}. Allow "
              "MirrorBridge in Windows Firewall on private networks, and check that the PC and the TV are "
              "on the same network.{others}",
        "es": "La TV no inició el video en {seconds} s y nunca lo pidió en {url}. Permite MirrorBridge en el "
              "Firewall de Windows para redes privadas, y revisa que el equipo y la TV estén en la misma "
              "red.{others}",
    },
    "tv_other_clients": {
        "en": " ({clients} did download it, but that is not the TV's address, {address}.)",
        "es": " ({clients} sí lo descargó, pero esa no es la dirección de la TV, {address}.)",
    },
    "tv_rejected": {
        "en": "The TV downloaded the playlist but none of the video{asked}, so it probably does not accept "
              "this video format. {hint}",
        "es": "La TV descargó la lista de reproducción, pero nada del video{asked}, así que probablemente no "
              "acepta este formato de video. {hint}",
    },
    "tv_also_asked": {
        "en": " (it also asked for {path}, which MirrorBridge does not have)",
        "es": " (también pidió {path}, que MirrorBridge no tiene)",
    },
    "hint_x264": {
        "en": "FFmpeg could not tell the TV which codecs {encoder} produces: choose {menu_x264}.",
        "es": "FFmpeg no pudo decirle a la TV qué códecs produce {encoder}: elige {menu_x264}.",
    },
    "hint_smooth": {"en": "Try {menu_smooth}.", "es": "Prueba {menu_smooth}."},
    "hint_30fps": {"en": "Try 30 frames per second.", "es": "Prueba con 30 cuadros por segundo."},
    "hint_720p": {
        "en": "Try {menu_720p}, or 30 frames per second.",
        "es": "Prueba {menu_720p} o 30 cuadros por segundo.",
    },
    "hint_lower": {
        "en": "Try a lower resolution or 30 frames per second.",
        "es": "Prueba una resolución más baja o 30 cuadros por segundo.",
    },
    # The play queue (airplay_queue.py, airplay_patch.py)
    "tv_queue_refused": {
        "en": "The TV turned down the video sent from a URL ({steps}). Use {menu_probe} to see which way of "
              "sending video it takes, and send the test's report with your bug report.",
        "es": "La TV rechazó el video enviado desde una URL ({steps}). Usa {menu_probe} para ver de qué forma "
              "acepta el video, y manda el informe de la prueba con tu reporte del problema.",
    },
    "tv_media_error": {
        "en": "The TV's player could not play the video (error {code}, {domain}). {hint}",
        "es": "La TV no pudo reproducir el video: su reproductor dio el error {code} ({domain}). {hint}",
    },
    "tv_media_network": {
        "en": "Its player could not download the video: allow MirrorBridge in Windows Firewall on private "
              "networks, and check that the PC and the TV are on the same network.",
        "es": "Su reproductor no pudo descargar el video: permite MirrorBridge en el Firewall de Windows para "
              "redes privadas, y revisa que el equipo y la TV estén en la misma red.",
    },
    "tv_media_format": {
        "en": "Its player does not take this video format. Please report this TV, with mirrorbridge.log.",
        "es": "Su reproductor no acepta este formato de video. Avisa de esta TV y manda mirrorbridge.log.",
    },
    "tv_media_decoder": {
        "en": "Its player could not decode the video. Try a lower resolution or 30 frames per second.",
        "es": "Su reproductor no pudo decodificar el video. Prueba una resolución más baja o 30 cuadros por "
              "segundo.",
    },
    "tv_media_report": {
        "en": "Please report this TV, with mirrorbridge.log.",
        "es": "Avisa de esta TV y manda mirrorbridge.log.",
    },
    "tv_media_unreachable": {
        "en": "Its player could not download the video from {host}.",
        "es": "Su reproductor no pudo descargar el video de {host}.",
    },
    "tv_probe_advice": {
        "en": "{problem} Use {menu_probe}, and send the test's report with your bug report.",
        "es": "{problem} Usa {menu_probe} y manda el informe de la prueba con tu reporte del problema.",
    },
    "tv_live_played_to_end": {
        "en": "The TV ended the live stream after {seconds} s, as if it were a video file that had reached its "
              "end.",
        "es": "La TV terminó la transmisión en vivo a los {seconds} s, como si fuera un archivo de video que "
              "llegó a su final.",
    },
    "tv_queue_silent": {
        "en": "The TV took the video but did not say within {seconds} s that it was playing it.",
        "es": "La TV aceptó el video, pero en {seconds} s no dijo que lo estuviera reproduciendo.",
    },
    "tv_queue_ended": {
        "en": "The TV ended the AirPlay session before it played the video: {reason}.",
        "es": "La TV terminó la sesión de AirPlay antes de reproducir el video: {reason}.",
    },
    "tv_preflight_only": {
        "en": "The TV reached MirrorBridge and asked whether its player may fetch the stream from a web page, but "
              "then never fetched it from {url}. Please report this TV, with mirrorbridge.log.",
        "es": "La TV llegó a MirrorBridge y preguntó si su reproductor puede descargar el video desde una página "
              "web, pero después nunca lo descargó de {url}. Avisa de esta TV y manda mirrorbridge.log.",
    },
    # Why the TV ended a cast on the play queue
    "queue_end_stopped": {"en": "the TV stopped playing", "es": "la TV dejó de reproducir"},
    "queue_end_played": {"en": "the video ended on the TV", "es": "el video terminó en la TV"},
    "queue_end_failed": {"en": "the TV's player failed", "es": "el reproductor de la TV falló"},
    "queue_end_back": {
        "en": "BACK was pressed on the TV's remote",
        "es": "se presionó ATRÁS en el control remoto de la TV",
    },
    "queue_end_events_closed": {
        "en": "the TV closed the event channel",
        "es": "la TV cerró el canal de eventos",
    },
    "queue_end_hung_up": {"en": "the TV closed the connection", "es": "la TV cerró la conexión"},
    "queue_end_silent": {
        "en": "the TV stopped answering ({misses} keep-alives in a row)",
        "es": "la TV dejó de responder ({misses} mensajes de mantenimiento seguidos)",
    },
    # What Test this TV found out (probe.py)
    "probe_test_play": {
        "en": "POST /play (AirPlay video v1) with Apple's test video",
        "es": "POST /play (video AirPlay v1) con el video de prueba de Apple",
    },
    "probe_test_queue": {
        "en": "The play queue (AirPlay video v2) with Apple's test video",
        "es": "La cola de reproducción (video AirPlay v2) con el video de prueba de Apple",
    },
    "probe_test_live": {
        "en": "The play queue with MirrorBridge's test pattern, from this PC",
        "es": "La cola de reproducción con la imagen de prueba de MirrorBridge, desde este equipo",
    },
    "probe_line": {"en": "{number}. {test}: {result}", "es": "{number}. {test}: {result}"},
    "probe_no_answer": {"en": "no answer ({steps})", "es": "sin respuesta ({steps})"},
    "probe_refused": {"en": "refused ({steps})", "es": "rechazado ({steps})"},
    "probe_failed": {"en": "failed: {error}", "es": "falló: {error}"},
    "probe_agent": {"en": "User-Agent: {agent}", "es": "User-Agent: {agent}"},
    "probe_no_agent": {"en": "no User-Agent", "es": "sin User-Agent"},
    "probe_fetched": {
        "en": "the TV took it and fetched the video ({agent})",
        "es": "la TV lo aceptó y descargó el video ({agent})",
    },
    "probe_playlist_only": {
        "en": "the TV took it and fetched the playlist, but none of the video ({agent})",
        "es": "la TV lo aceptó y descargó la lista de reproducción, pero nada del video ({agent})",
    },
    "probe_not_fetched": {
        "en": "the TV took it but never fetched it from this PC ({steps})",
        "es": "la TV lo aceptó, pero nunca lo descargó de este equipo ({steps})",
    },
    "probe_playing": {
        "en": "the TV took it and said it was playing ({steps})",
        "es": "la TV lo aceptó y dijo que lo estaba reproduciendo ({steps})",
    },
    "probe_wrong_path": {
        "en": "the TV took it and reached this PC, but asked for {path}, which MirrorBridge does not serve "
              "({agent})",
        "es": "la TV lo aceptó y llegó a este equipo, pero pidió {path}, que MirrorBridge no ofrece ({agent})",
    },
    "probe_preflight_only": {
        "en": "the TV took it and asked from a web page whether it may fetch it (OPTIONS), but never fetched it "
              "({agent})",
        "es": "la TV lo aceptó y preguntó desde una página web si podía descargarlo (OPTIONS), pero nunca lo "
              "descargó ({agent})",
    },
    "probe_took": {
        "en": "the TV took it but did not say that it was playing ({steps})",
        "es": "la TV lo aceptó, pero no dijo que lo estuviera reproduciendo ({steps})",
    },
    "probe_then": {"en": "{verdict}, then {ended}", "es": "{verdict}; después, {ended}"},
    "probe_works": {
        "en": "{device} should play your casts: it fetched MirrorBridge's video.",
        "es": "{device} debería reproducir tus transmisiones: descargó el video de MirrorBridge.",
    },
    "probe_live_not_played": {
        "en": "{device} reached this PC for MirrorBridge's stream, but did not play it: {result}. Please send the "
              "report with your bug report.",
        "es": "{device} llegó a este equipo para el video de MirrorBridge, pero no lo reprodujo: {result}. Manda "
              "el informe con tu reporte del problema.",
    },
    "probe_internet_only": {
        "en": "{device} plays video from the internet, but never fetched MirrorBridge's own stream from this PC. "
              "Check that Windows Firewall allows MirrorBridge on private networks; if it does, this TV may "
              "only play video from HTTPS addresses.",
        "es": "{device} reproduce video de internet, pero nunca descargó el video de MirrorBridge desde este "
              "equipo. Revisa que el Firewall de Windows permita MirrorBridge en redes privadas; si lo permite, "
              "puede que esta TV solo reproduzca video de direcciones HTTPS.",
    },
    "probe_player_silent": {
        "en": "{device} takes the video it is sent, but its player did not start: it did not say that it played "
              "Apple's video, and never fetched MirrorBridge's stream from this PC. Please send the report with "
              "your bug report.",
        "es": "{device} acepta el video que se le envía, pero su reproductor no arrancó: no dijo que reprodujera "
              "el video de Apple y nunca descargó el video de MirrorBridge desde este equipo. Manda el informe "
              "con tu reporte del problema.",
    },
    "probe_not_fetched_here": {
        "en": "{device} took MirrorBridge's stream but never fetched it from this PC. Check that Windows Firewall "
              "allows MirrorBridge on private networks, and send the report with your bug report.",
        "es": "{device} aceptó el video de MirrorBridge, pero nunca lo descargó de este equipo. Revisa que el "
              "Firewall de Windows permita MirrorBridge en redes privadas, y manda el informe con tu reporte del "
              "problema.",
    },
    "probe_live_refused": {
        "en": "{device} took Apple's video, but not MirrorBridge's stream: {result}. Please send the report with "
              "your bug report.",
        "es": "{device} aceptó el video de Apple, pero no el de MirrorBridge: {result}. Manda el informe con tu "
              "reporte del problema.",
    },
    "probe_none": {
        "en": "None of the tests got {device} to take the video.",
        "es": "Ninguna de las pruebas logró que {device} aceptara el video.",
    },
    "probe_report_file": {
        "en": "The full report is in {file}: please send it with your bug report.",
        "es": "El informe completo está en {file}: mándalo con tu reporte del problema.",
    },
}

_lang = DEFAULT


def set_language(code: str) -> str:
    """Speak `code` from now on, or English for a code not in LANGUAGES (such as the "" that
    used to mean "as Windows"). Returns the language used."""
    global _lang  # pylint: disable=global-statement
    _lang = code if code in LANGUAGES else DEFAULT
    return _lang


def language() -> str:
    return _lang


def _words(key: str, code: str) -> str:
    entry = _STRINGS.get(key)
    return (entry.get(code) or entry[DEFAULT]) if entry else key


def tr(key: str, **kwargs) -> str:
    """The string `key` in the app's language, or in English if that language lacks it."""
    text = _words(key, _lang)
    return text.format(**kwargs) if kwargs else text


def language_menu_title() -> str:
    """The Language menu's name in every language ("Language / Idioma"), so it is found
    whichever language the menu is in."""
    return " / ".join(dict.fromkeys(_words("language_menu", code) for code in LANGUAGES))


def state_text(state) -> str:
    """Localized name of a session.State."""
    return tr("state_" + state.name.lower())


def _item(key: str) -> str:
    """A menu item's name without its "…" or "(~7 s delay)"."""
    return tr(key).split(" (")[0].rstrip("…")


def _menu_paths() -> dict[str, str]:
    """The menu items that texts name ({menu_forget}...), as the menu names them."""
    paths = {
        "menu_forget": (_item("device_menu"), _item("forget")),
        "menu_pair": (_item("device_menu"), _item("pair")),
        "menu_smooth": (_item("quality_menu"), _item("latency"), _item("lat_smooth")),
        "menu_x264": (_item("quality_menu"), _item("encoder"), "x264 (CPU)"),
        "menu_720p": (_item("source_menu"), _item("extend_size"), "720p"),
        "menu_probe": (_item("more"), _item("probe")),
    }
    return {name: " > ".join(words) for name, words in paths.items()}


def say(text: str) -> str:
    """A core module's Text in the user's language, its values too; any other str, or a Text
    whose key is not here, as it is (in English)."""
    if not isinstance(text, Text) or text.key not in _STRINGS:
        return text
    values = {name: say(value) if isinstance(value, str) else value for name, value in text.values.items()}
    return tr(text.key, **{**_menu_paths(), **values})
