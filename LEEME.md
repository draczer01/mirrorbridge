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
   * **Duplicar pantalla (baja latencia, experimental)**, justo debajo de **Transmitir a ...**:
     ver *Duplicar la pantalla*.
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

* **Al transmitir hay un retardo de 3 a 7 segundos** con un Apple TV (unos 7 segundos o más
  con TVs de otras marcas, que además tardan unos 6 segundos más en empezar). Para transmitir,
  MirrorBridge usa "AirPlay video", lo mismo que usa YouTube para mandar un video a la TV.
  Sirve para presentaciones, fotos y videos, no para juegos. A las TVs de otras marcas también
  puede duplicarles la pantalla con una fracción de segundo de retardo (experimental, ver
  *Duplicar la pantalla*). Con un Apple TV, el espejo en tiempo real que hace un Mac o un
  iPhone usa un cifrado privado de Apple (FairPlay) que MirrorBridge todavía no tiene.
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

## Duplicar la pantalla (experimental)

A las TVs de otras marcas con AirPlay 2 (TCL, Roku, Samsung, LG...) MirrorBridge también
puede mandarles la pantalla como la duplica un iPhone: cada cuadro va a la TV apenas FFmpeg lo
codifica, por una conexión propia, así que la imagen llega con una fracción de segundo de
retardo (la idea es de 100 a 200 ms) en lugar de los segundos de una transmisión. Es nuevo y
todavía no se probó con una TV real.

### Desde el menú de la bandeja

1. Elige la TV en **Dispositivo** y emparéjala, como para transmitir (**Transmitir a ...** una
   vez, o *Dispositivo* > *Emparejar con PIN…*).
2. Una vez, usa **Más opciones** > **Probar esta TV…**. Su última prueba duplica una imagen de
   prueba en movimiento durante unos 10 segundos: cuando un aviso diga *Mira la TV ahora*, mira
   si aparece en la TV. Lo que funcionó queda guardado para esa TV, así que *Duplicar pantalla*
   lo pide de entrada (ver *Avisar de una TV que no funciona*).
3. Elige **Duplicar pantalla (baja latencia, experimental)**, justo debajo de **Transmitir a
   ...**. Igual que al transmitir, el texto del icono dice *Conectando con la TV…* y luego
   *Duplicando la pantalla en (tu TV)*, y un aviso dice cuándo empieza, cuándo termina y, si
   falla, por qué. **Detener la duplicación** (donde está *Detener transmisión*) o **Salir** la
   terminan.

*Duplicar pantalla* aparece en gris hasta que MirrorBridge está emparejado con la TV (al pasar
el mouse lo dice), con los receptores que no ofrecen duplicar la pantalla, y con los Apple TV y
los Mac, que necesitan FairPlay para duplicar la pantalla: llegarán en una versión futura, y
mientras tanto se les transmite como siempre. Se hace una sola cosa a la vez: mientras hay una
transmisión, una duplicación o una prueba de la TV, lo demás queda en gris hasta que termine.
La ventana completa tiene el mismo botón, debajo de **Transmitir**.

**Qué mandar** después de probarlo con tu TV:

* el `probe-<fecha>-<hora>.log` que escribió *Probar esta TV*, y si en su última prueba
  apareció en la TV la imagen de prueba en movimiento, y si se movía con fluidez;
* `mirrorbridge.log` después de unos minutos de *Duplicar pantalla* (*Más opciones* > *Abrir
  carpeta de registros*): cada 5 segundos anota en qué se fue el tiempo de los cuadros;
* cuánto se atrasa la TV: pon un cronómetro en la pantalla del equipo (uno en una pestaña del
  navegador sirve), duplícala y saca una sola foto con el teléfono donde se vean la pantalla
  del equipo y la TV. La diferencia entre los dos tiempos es el retardo;
* `%APPDATA%\MirrorBridge\mirroring.json`, donde queda lo que funcionó con la TV.

### Desde la consola

```
.\MirrorBridge.exe mirror --probe -d "Nombre de la TV"      # averigua cómo lo acepta la TV
.\MirrorBridge.exe mirror -d "Nombre de la TV"              # duplica la pantalla hasta Ctrl+C
.\MirrorBridge.exe mirror --test-pattern --seconds 30       # una imagen de prueba en lugar de la pantalla
```

### Qué necesita y qué hace

* Primero empareja la TV (`pair`), igual que para transmitir. Los Apple TV y los Mac necesitan
  FairPlay para duplicar la pantalla, y MirrorBridge no lo tiene.
* Duplica lo que elegiste en **Qué transmitir** (una pantalla o una ventana) al tamaño de la
  pantalla de la TV, hasta 1080p o lo que diga **Calidad** > **Resolución**, con el
  codificador, los cuadros por segundo y la tasa de bits de **Calidad**. Todavía no hay sonido.
  Si FFmpeg no puede capturar o codificar al empezar (el controlador de video rechaza ddagrab,
  el codificador de la GPU está ocupado), sigue con la captura GDI y luego con x264 (CPU), igual
  que al transmitir.
* La TV le pide la hora al equipo por UDP: permite MirrorBridge, o los puertos UDP de entrada
  7880-7889, en el Firewall de Windows para redes privadas, o la TV no mostrará nada.
* `mirror --probe` prueba las formas en que las TVs difieren, muestra qué contestó la TV y
  guarda lo que funcionó en `%APPDATA%\MirrorBridge\mirroring.json`, que *Duplicar pantalla* y
  cada `mirror` piden primero. La última prueba de *Probar esta TV* hace lo mismo en 75
  segundos: la imagen de prueba sale en cuanto la TV acepta un SETUP, y las demás formas se
  prueban con el tiempo que queda (el informe dice cuáles no alcanzó a probar). Si no funciona,
  manda lo que muestra junto con `mirrorbridge.log`. Antes de un `--probe`, si la TV corta el
  video apenas empieza, se intenta una vez más con el otro final del encabezado H.264.
* Si la red o la TV no dan abasto, se descartan cuadros hasta el siguiente cuadro clave en
  lugar de acumularlos: la imagen salta, pero no se atrasa. Si la TV no recibe video durante
  5 segundos, la sesión termina con un error; si terminas la duplicación desde la TV, la
  sesión simplemente se detiene.

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
tres minutos: mira la TV y, si pregunta si permite MirrorBridge, elige **Permitir**.
MirrorBridge prueba cuatro formas de enviarle un video, una tras otra: `POST /play` con el
video de prueba público de Apple, la cola de reproducción con el mismo video, la cola de
reproducción con una transmisión corta de la imagen de prueba de MirrorBridge desde este
equipo, y por último la duplicación de pantalla con una imagen de prueba en movimiento durante
unos 10 segundos (en ninguna se manda nada de tu pantalla). Cuando un aviso diga *Mira la TV
ahora*, mira si la imagen en movimiento aparece en la TV; una TV que necesita FairPlay (un Apple
TV), o con la que MirrorBridge no está emparejado, se salta esa prueba. Al final dice qué hizo
la TV con cada una (en la duplicación, si aceptó la sesión y mantuvo abierto el video, y qué
orden de SETUP, final del encabezado H.264 y forma de dar la hora funcionaron, que guarda para
*Duplicar pantalla*), si *Duplicar pantalla* debería funcionar, y dónde está el informe:
`%APPDATA%\MirrorBridge\probe-<fecha>-<hora>.log` (el botón **Abrir carpeta de registros** del
resultado lleva ahí).

Manda ese archivo junto con `mirrorbridge.log`, y di si apareció la imagen de prueba en
movimiento. Ahí se ve qué anuncia la TV, qué contestó a cada pedido de AirPlay, qué dijo
mientras reproducía, si descargó el video de este equipo (y con qué reproductor), si pidió
algo que MirrorBridge no tiene, y cada paso de la duplicación de pantalla.

Lo mismo desde una consola, en la carpeta del exe:

```
.\MirrorBridge.exe probe "Nombre de la TV"
.\MirrorBridge.exe -v scan
```

`scan` agrega lo que anuncia cada receptor de la red.
