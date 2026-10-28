# MirrorBridge: guía rápida

MirrorBridge transmite la pantalla de Windows (completa o una sola ventana), con el sonido
del equipo, a un Apple TV o a una TV con AirPlay (LG, Samsung, TCL...). Es un solo archivo,
`MirrorBridge.exe`, que ya trae FFmpeg adentro: no hay que instalar nada (solo *Extender
pantalla* necesita un controlador, que MirrorBridge ofrece instalar la primera vez).

## Antes se llamaba AirCast

Hasta la versión 0.2.1, MirrorBridge se llamaba AirCast. Para pasarte, cierra AirCast (su
icono en la bandeja > **Salir**) y abre `MirrorBridge.exe`. La primera vez, MirrorBridge:

* copia los ajustes y los emparejamientos de AirCast (`settings.json` y `pairings.json` de
  `%APPDATA%\AirCast`) a `%APPDATA%\MirrorBridge`, así que la TV sigue emparejada. Nunca
  cambia la carpeta de AirCast ni reemplaza un archivo que MirrorBridge ya tenga;
* se inicia con Windows en lugar de AirCast, si AirCast lo hacía (pero no si lo habías
  desactivado en la pestaña Inicio del Administrador de tareas).

Después el Firewall de Windows vuelve a preguntar, ahora por `MirrorBridge.exe`: permítelo
en redes privadas. Luego borra `AirCast.exe` y el acceso directo de `AirCast` del
escritorio, que MirrorBridge no toca; cuando MirrorBridge ya transmita a tu TV, también
puedes borrar `%APPDATA%\AirCast`. AirCast y MirrorBridge no se conocen: si los dos están
abiertos, verás dos iconos en la bandeja.

AirCast seguía el idioma de Windows; MirrorBridge empieza en inglés. Para volver a tenerlo en
español, elige una vez **Language / Idioma** > **Español** en su menú.

## Uso

1. Haz doble clic en **MirrorBridge.exe**. Si Windows SmartScreen avisa que la app no es
   reconocida (no está firmada), elige *Más información* > *Ejecutar de todas formas*.
2. Aparece el icono de MirrorBridge junto al reloj (puede estar escondido bajo la flecha
   **^**; arrástralo afuera para tenerlo siempre visible). Haz clic en él, izquierdo o
   derecho, para abrir el menú.
3. El menú empieza en inglés. Para verlo en español, elige **Language / Idioma** >
   **Español**: el menú, la ventana y los mensajes cambian enseguida, y MirrorBridge lo
   recuerda para la próxima vez. **English** lo vuelve a poner en inglés.
4. Cuando el Firewall de Windows pregunte, **permite MirrorBridge en redes privadas**. La
   TV descarga el video desde tu PC.
5. En el menú: **Dispositivo** > elige tu TV, luego **Transmitir a ...**. La primera vez
   la TV muestra un PIN: escríbelo y queda emparejado para siempre.
6. Todo lo demás también está en el menú:
   * **Qué transmitir**: la pantalla, una sola ventana, o **Extender pantalla** (la TV
     como segunda pantalla), y si se ve el puntero.
   * **Calidad**: resolución, cuadros por segundo, retardo, calidad del video y codificador.
   * **Incluir el sonido del equipo**.
   * **Reproducir un archivo de video** en la TV.
   * **Más opciones**: iniciar con Windows, crear acceso directo en el escritorio, abrir
     la carpeta de registros y **Probar esta TV…** (ver *Avisar de una TV que no funciona*).
   * **Language / Idioma**: English o Español. La ventana completa también lo tiene, abajo.
7. **Abrir MirrorBridge** muestra la ventana completa. Al cerrarla, MirrorBridge sigue en
   la bandeja. **Salir** detiene la transmisión y cierra MirrorBridge.

## Extender pantalla (la TV como segunda pantalla)

Es experimental: todavía no se probó con el controlador real.

En el menú, **Qué transmitir** > **Extender pantalla (pantalla virtual)**. Mientras
transmites, Windows tiene un monitor más que solo se ve en la TV: arrastra ventanas hacia
la derecha de tu pantalla para llevarlas a la TV. Al detener la transmisión, esa pantalla
desaparece. En **Tamaño de la pantalla extendida** eliges 1080p (el normal), 720p (letra
más grande y menos datos) o 4K (se envía a 20 Mbps o más). La TV recibe exactamente ese
tamaño, así que la **Resolución** de *Calidad* no se aplica en este modo.

Windows no puede crear un monitor por sí solo, así que MirrorBridge usa el controlador
gratuito **Parsec Virtual Display Driver** de Parsec (hoy parte de Unity), firmado por
Microsoft. MirrorBridge no lo incluye. La primera vez te pregunta antes de hacer nada; si
aceptas, descarga el instalador oficial de parsec.app (unos 500 KB), comprueba que sea
exactamente el archivo esperado y lo ejecuta. Windows te pide permiso una sola vez. Al
instalarlo aceptas los
[términos de Parsec](https://parsec.app/terms). Si la descarga está bloqueada, el mismo
cuadro ofrece instalarlo con winget (`winget install -e --id Parsec.ParsecVDD`) o
descargarlo a mano.

Para quitarlo: *Más opciones* > *Quitar el controlador de pantalla virtual* abre
Configuración > Aplicaciones; desinstala ahí **Parsec Virtual Display Driver**.

Si MirrorBridge se cierra de golpe, el controlador quita la pantalla solo, en un segundo.
Al suspender el equipo también desaparece: vuelve a transmitir al despertarlo.

## Lo que hay que saber

* **Hay un retardo de 3 a 7 segundos** con un Apple TV (unos 7 segundos o más con TVs
  de otras marcas, que además tardan unos 6 segundos más en empezar). El espejo en
  tiempo real que hace un Mac o un iPhone usa un cifrado privado de Apple (FairPlay) que
  ningún programa libre puede enviar. MirrorBridge usa "AirPlay video", lo mismo que usa
  YouTube para mandar un video a la TV. Sirve para presentaciones, fotos y videos, no
  para juegos.
* **Una sola ventana**: se captura el lugar que ocupa la ventana al empezar. Si la mueves,
  detén y vuelve a transmitir. No puede estar minimizada.
* **TVs que no son Apple** (LG, Samsung, Sony, Hisense, TCL...): muchas aceptan video por
  AirPlay 2, pero algunas solo aceptan audio o fotos de equipos que no son Apple. A estas
  TVs MirrorBridge les manda segmentos de 2 segundos, como pide Apple para TVs con
  AirPlay 2, sin importar el ajuste de retardo. Muchas (TCL y LG, entre otras) no tienen
  `POST /play`, el primer pedido de video de AirPlay, y le contestan 404: a esas
  MirrorBridge les entrega el video por la *cola de reproducción* (video AirPlay v2), como
  lo hace un iPhone. Lo elige según lo que anuncia la TV, también lo intenta una vez con
  cualquier TV que conteste 404 a `/play`, y recuerda qué forma funcionó hasta que se
  cierra. Si la TV rechaza el video, el mensaje dice qué contestó a cada pedido de
  MirrorBridge.
* **TVs con contraseña de AirPlay**: MirrorBridge no puede enviar la contraseña junto con
  el video. Desactiva la contraseña en los ajustes de AirPlay de la TV.
* **"La TV no terminó de preparar la sesión de AirPlay"** o **"La TV rechazó las
  credenciales de MirrorBridge"**: casi siempre la TV olvidó el emparejamiento con
  MirrorBridge. Usa *Dispositivo > Olvidar emparejamiento* y vuelve a transmitir; escribe el
  PIN nuevo.
* Si algo falla, el registro está en `%APPDATA%\MirrorBridge\mirrorbridge.log` (menú *Más
  opciones* > *Abrir carpeta de registros*).

## Diagnóstico

En una ventana de PowerShell o cmd, en la carpeta del exe:

```
.\MirrorBridge.exe doctor
```

Se abre una consola que muestra FFmpeg, los codificadores de tu tarjeta de video, si se
puede capturar el sonido, las pantallas, si el controlador de pantalla virtual está listo
y las TVs encontradas. Una captura de esa ventana sirve mucho para encontrar problemas.
`.\MirrorBridge.exe vdd status` muestra solo el estado del controlador de pantalla virtual.
La consola escribe en inglés; el menú y la ventana, en el idioma que elijas en **Language /
Idioma**.

### Avisar de una TV que no funciona

Elige la TV en **Dispositivo** y luego **Más opciones** > **Probar esta TV…**. Tarda unos
dos minutos: mira la TV y, si pregunta si permite MirrorBridge, elige **Permitir**.
MirrorBridge prueba tres formas de enviarle un video, una tras otra: `POST /play` con el
video de prueba público de Apple, la cola de reproducción con el mismo video, y la cola de
reproducción con una transmisión corta de la imagen de prueba de MirrorBridge desde este
equipo (nada de tu pantalla). Al final dice qué hizo la TV con cada una y dónde está el
informe: `%APPDATA%\MirrorBridge\probe-<fecha>-<hora>.log` (el botón **Abrir carpeta de
registros** del resultado lleva ahí).

Manda ese archivo junto con `mirrorbridge.log`. Ahí se ve qué anuncia la TV, qué contestó
a cada pedido de AirPlay, qué dijo mientras reproducía, si descargó el video de este equipo
(y con qué reproductor), y si pidió algo que MirrorBridge no tiene.

Lo mismo desde una consola, en la carpeta del exe:

```
.\MirrorBridge.exe probe "Nombre de la TV"
.\MirrorBridge.exe -v scan
```

`scan` agrega lo que anuncia cada receptor de la red.
