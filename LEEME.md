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
5. En el menú: **Dispositivo** > elige tu TV, luego **Mostrar mi pantalla en ...**. La
   primera vez la TV muestra un PIN: escríbelo y queda emparejado para siempre. A una TV de
   otra marca le duplica la pantalla con una fracción de segundo de retardo (ver *Duplicar la
   pantalla*); a un Apple TV le transmite, con unos segundos de retardo.
6. Todo lo demás también está en el menú:
   * **Qué transmitir**: la pantalla, una sola ventana, o **Extender pantalla** (la TV
     como segunda pantalla), y si se ve el puntero.
   * **Ajustes de Duplicar pantalla**: **Latencia** y **Cuadros por segundo** al duplicar
     (ver *Duplicar la pantalla*).
   * **Calidad**: resolución, calidad del video y codificador; sus cuadros por segundo y su
     retraso son solo para transmitir.
   * **Incluir el sonido del equipo (solo al transmitir)**: la duplicación todavía no lleva
     sonido.
   * **Reproducir un archivo de video** en la TV.
   * **Más opciones**: **Transmitir (modo anterior, 3-7 s de retraso)**, para una TV que no
     muestra la pantalla de la otra forma; iniciar con Windows, crear acceso directo en el
     escritorio, abrir la carpeta de registros y **Probar esta TV…** (ver *Avisar de una TV
     que no funciona*).
   * **Language / Idioma**: English o Español. La ventana completa también lo tiene, abajo.
7. **Abrir MirrorBridge** muestra la ventana completa. Al cerrarla, MirrorBridge sigue en
   la bandeja. **Salir** detiene la transmisión y cierra MirrorBridge.

## Extender pantalla (la TV como segunda pantalla)

Es experimental: todavía no se probó con el controlador real.

En el menú, **Qué transmitir** > **Extender pantalla (pantalla virtual)**. Mientras
transmites, Windows tiene un monitor más que solo se ve en la TV: arrastra ventanas hacia
la derecha de tu pantalla para llevarlas a la TV. Al detener la transmisión, esa pantalla
desaparece. *Duplicar pantalla* también la envía, con una fracción de segundo de retardo en
lugar de unos segundos (ver *Duplicar la pantalla*): por ejemplo, para la Vista del moderador
de PowerPoint, con las notas en la laptop y las diapositivas en la TV. En **Tamaño de la
pantalla extendida** eliges 1080p (el normal), 720p (letra más grande y menos datos) o 4K.
Al transmitir, la TV recibe exactamente ese tamaño (4K se envía a 20 Mbps o más).
*Duplicar pantalla* la crea a 1080p como máximo (a 1080p si eliges 4K: achicar 4K en cada
cuadro hacía que llegaran después de lo que espera la TV) y la ajusta a la pantalla de la TV,
así que la **Resolución** de *Calidad* no se aplica en este modo.

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
Al suspender el equipo también desaparece: vuelve a transmitir o a duplicar la pantalla al
despertarlo.

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

1. Elige la TV en **Dispositivo** y emparéjala (*Dispositivo* > *Emparejar con PIN…*:
   escribe el PIN que muestra la TV).
2. Una vez, usa **Más opciones** > **Probar esta TV…**. Su última prueba duplica una imagen de
   prueba en movimiento durante unos 10 segundos: cuando un aviso diga *Mira la TV ahora*, mira
   si aparece en la TV. Lo que funcionó queda guardado para esa TV, así que *Duplicar pantalla*
   lo pide de entrada (ver *Avisar de una TV que no funciona*).
3. Elige **Mostrar mi pantalla en (tu TV)**, justo debajo de la línea de estado (si te
   saltaste el paso 1, primero la empareja). Igual que al transmitir, el texto del icono dice
   *Conectando con la TV…* y luego *Duplicando la pantalla en (tu TV)*, y un aviso dice cuándo
   empieza, cuándo termina y, si falla, por qué. **Detener la duplicación**, en el mismo
   lugar, o **Salir** la terminan.

Al pasar el mouse, la opción dice qué va a hacer. A los receptores que no ofrecen duplicar la
pantalla se les transmite, y también a los Apple TV y los Mac, que necesitan FairPlay para
duplicar la pantalla (llegará en una versión futura). Si la duplicación falla antes de que la
imagen llegue a la TV, se prueba la siguiente forma sin preguntar. La transmisión, con sonido,
también está en *Más opciones* > *Transmitir (modo anterior, 3-7 s de retraso)*. Se hace una
sola cosa a la vez: mientras hay una transmisión, una duplicación o una prueba de la TV, lo
demás queda en gris hasta que termine. El botón grande de la ventana completa hace lo mismo.

**Ajustes de Duplicar pantalla**, en el menú, cambian la siguiente duplicación:

* *Latencia*: cuánto después de capturar un cuadro lo muestra la TV. *Baja* son 60 ms, para
  cable o buena señal de Wi-Fi; *Normal* (la predeterminada) es lo guardado para la TV,
  100 ms; *Suave* son 200 ms, un poco más de retardo y menos saltos cuando falla el Wi-Fi. Una
  espera más corta solo baja el retardo si la TV respeta la hora de los cuadros.
* *Cuadros por segundo*: *Automático* (lo predeterminado) son 60 cuando la tarjeta de video
  codifica y la captura es Desktop Duplication, y 30 con x264 (el procesador) o con la captura
  GDI; *30 fps* y *60 fps* se quedan en ese valor. Nunca más de lo que acepta la TV. Los
  cuadros por segundo y el retraso de **Calidad** son solo para transmitir.

**Qué mandar** después de probarlo con tu TV:

* el `probe-<fecha>-<hora>.log` que escribió *Probar esta TV*, y si en su última prueba
  apareció en la TV la imagen de prueba en movimiento, y si se movía con fluidez;
* `mirrorbridge.log` después de unos minutos de *Duplicar pantalla* (*Más opciones* > *Abrir
  carpeta de registros*): cada 5 segundos anota en qué se fue el tiempo de los cuadros;
* cuánto se atrasa la TV: mientras duplica, elige **Más opciones** > **Depuración** >
  **Medidor de latencia**. Muestra un reloj grande (segundos y milisegundos, con las décimas
  como una fila de casillas) en cada pantalla, también en la virtual de *Extender pantalla*.
  Saca unas fotos con el teléfono donde se vean la pantalla del equipo y la TV: la diferencia
  entre los dos relojes es el retardo. En la pantalla más rápida el reloj es más preciso (una
  de 200 Hz muestra una hora nueva cada 5 ms). Esc o un clic lo cierra;
* `%APPDATA%\MirrorBridge\mirroring.json`, donde queda lo que funcionó con la TV.

Junto al *Medidor de latencia*, **Más opciones** > **Depuración (para pruebas)** tiene cuatro
opciones para hacer pruebas. Empiezan con los valores de siempre y cambian la siguiente
duplicación (y `mirror`), no *Probar esta TV* ni `mirror --probe`: *Espera de la TV (prueba)*
le pide a la TV que espere de 60 a 300 ms después de capturar cada cuadro antes de mostrarlo,
en lugar de lo que diga *Latencia* (*Automática*; elegir una Latencia la deja ahí, y un valor
de prueba nunca se guarda); *Cuadros clave* manda un cuadro clave (una imagen completa) solo
cuando hace falta (lo normal), cada 10 segundos o cada 2 segundos, como antes; *Escalado*
cambia el tamaño de la imagen en la tarjeta de video cuando se puede (*Automático*, lo normal)
o en el procesador (*Procesador (como antes)*); *Mostrar cuadros al llegar (prueba)* le pide a
la TV que muestre cada cuadro en cuanto llega, en lugar de a su hora. Al empezar cada sesión,
el registro dice cuáles usó, y cada vez que FFmpeg vuelve a arrancar, por qué.

### Desde la consola

```
.\MirrorBridge.exe mirror --probe -d "Nombre de la TV"      # averigua cómo lo acepta la TV
.\MirrorBridge.exe mirror -d "Nombre de la TV"              # duplica la pantalla hasta Ctrl+C
.\MirrorBridge.exe mirror --test-pattern --seconds 30       # una imagen de prueba en lugar de la pantalla
.\MirrorBridge.exe mirror --extend                          # la TV como segunda pantalla (un monitor virtual nuevo)
.\MirrorBridge.exe mirror --extend --extend-mode 720p       # ... a 720p (1080p, 720p o 4k)
.\MirrorBridge.exe mirror --monitor 2                       # la pantalla 2 (--window "PowerPoint": una ventana)
```

Igual que `cast`, `mirror` parte de los ajustes de la app: sin `--monitor`, `--window` ni
`--extend` duplica lo que elegiste en **Qué transmitir**, incluida la pantalla virtual.

### Qué necesita y qué hace

* Primero empareja la TV (`pair`), igual que para transmitir. Los Apple TV y los Mac necesitan
  FairPlay para duplicar la pantalla, y MirrorBridge no lo tiene.
* Duplica lo que elegiste en **Qué transmitir**: una pantalla o una ventana al tamaño de la
  pantalla de la TV, hasta 1080p o lo que diga **Calidad** > **Resolución**, o con **Extender
  pantalla** la pantalla virtual. Esta se crea igual que al transmitir (el icono dice *Creando
  la pantalla virtual…*, y si falta el controlador se ofrece instalarlo), con su **Tamaño de la
  pantalla extendida** pero a 1080p como máximo (4K se crea a 1080p), ajustada a la pantalla de
  la TV (la **Resolución** no se aplica, igual que al transmitir). Dura toda la sesión y
  desaparece al terminarla; si desaparece antes (al suspender el equipo), la sesión termina y
  lo dice. *Probar esta TV* y `mirror --probe` nunca la crean. Todo se envía con el
  codificador y la tasa de bits de **Calidad**, a los cuadros por segundo de **Ajustes de
  Duplicar pantalla**. Todavía no hay sonido. Si FFmpeg no puede capturar o codificar al
  empezar (el controlador de video rechaza ddagrab, el codificador de la GPU está ocupado),
  sigue con la captura GDI y luego con x264 (CPU), igual que al transmitir.
* Con NVENC y Desktop Duplication la imagen no sale de la tarjeta de video: al tamaño de la TV
  va directo de la captura al codificador, y si no, Windows Graphics Capture (el `gfxcapture`
  de FFmpeg, que tiene el FFmpeg 8.1.1 que viene dentro de `MirrorBridge.exe`) toma la
  pantalla ya al tamaño de la TV, escalada en la tarjeta de video; otro FFmpeg sin él la escala
  en el procesador. Esa captura da un cuadro solo cuando la pantalla cambia, como mucho a los
  cuadros por segundo elegidos, así que una pantalla que no cambia no manda cuadros nuevos y no
  termina la sesión. (El `scale_d3d11` de FFmpeg no se usa: en una RTX 4080 SUPER con el
  controlador 621.01 no pudo hacer su imagen.) Si la captura termina antes de su primera
  imagen, o la imagen sale de otro tamaño que el pedido, esa pantalla vuelve al procesador
  hasta que se cierra MirrorBridge. Quick Sync, AMF y Media Foundation escalan en el
  procesador.
* Un cuadro clave (una imagen completa) se manda solo cuando hace falta: al empezar, y después
  de descartar cuadros, cuando FFmpeg vuelve a arrancar para dar uno enseguida (como mucho
  cada 2 segundos). Un cuadro clave cada 2 segundos hacía que el texto se viera borroso cada
  2 segundos. Quick Sync, AMF y Media Foundation, que todavía no se probaron así, siguen
  mandando uno cada 10 segundos.
* Si Windows corta la captura de pantalla (al instalar o quitar un controlador de video, al
  cerrar sesión, al cambiar la resolución, con un aviso de UAC), FFmpeg vuelve a arrancar en
  la misma pantalla, buscada de nuevo (también la virtual), hasta cinco veces en 10 segundos;
  solo entonces la sesión termina y dice que Windows detuvo la captura de pantalla. El
  registro guarda las últimas 30 líneas de FFmpeg cada vez que se detiene.
* La TV le pide la hora al equipo por UDP: permite MirrorBridge, o los puertos UDP de entrada
  7880-7889, en el Firewall de Windows para redes privadas, o la TV no mostrará nada.
* `mirror --probe` prueba las formas en que las TVs difieren, muestra qué contestó la TV y
  guarda lo que funcionó en `%APPDATA%\MirrorBridge\mirroring.json`, que *Duplicar pantalla* y
  cada `mirror` piden primero. La última prueba de *Probar esta TV* hace lo mismo en 75
  segundos: la imagen de prueba sale en cuanto la TV acepta un SETUP, y las demás formas se
  prueban con el tiempo que queda (el informe dice cuáles no alcanzó a probar). Si no funciona,
  manda lo que muestra junto con `mirrorbridge.log`. Antes de un `--probe`, si la TV corta el
  video apenas empieza, se intenta una vez más con el otro final del encabezado H.264.
* Si la red detiene el video un momento, como el Wi-Fi lo hace de vez en cuando, los cuadros
  que esperaban se mandan igual, un poco tarde, y la TV se pone al día. Solo si los cuadros
  llevan más de un segundo, más o menos, llegando tarde (la red o la TV no dan abasto), se
  descartan hasta un cuadro clave que llegue a tiempo, en lugar de acumularlos: la imagen
  salta, pero no se atrasa. Si la TV no recibe video durante 5 segundos, la sesión termina con
  un error; si terminas la duplicación desde la TV, la sesión simplemente se detiene.

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
