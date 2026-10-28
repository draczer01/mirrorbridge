# AirCast: guía rápida

AirCast transmite la pantalla de Windows (completa o una sola ventana), con el sonido del
equipo, a un Apple TV o a una TV con AirPlay. Es un solo archivo, `AirCast.exe`, que ya
trae FFmpeg adentro: no hay que instalar nada (solo *Extender pantalla* necesita un
controlador, que AirCast ofrece instalar la primera vez).

## Uso

1. Haz doble clic en **AirCast.exe**. Si Windows SmartScreen avisa que la app no es
   reconocida (no está firmada), elige *Más información* > *Ejecutar de todas formas*.
2. Aparece el icono de AirCast junto al reloj (puede estar escondido bajo la flecha **^**;
   arrástralo afuera para tenerlo siempre visible). Haz clic en él, izquierdo o derecho,
   para abrir el menú.
3. Cuando el Firewall de Windows pregunte, **permite AirCast en redes privadas**. La TV
   descarga el video desde tu PC.
4. En el menú: **Dispositivo** > elige tu TV, luego **Transmitir a ...**. La primera vez
   la TV muestra un PIN: escríbelo y queda emparejado para siempre.
5. Todo lo demás también está en el menú:
   * **Qué transmitir**: la pantalla, una sola ventana, o **Extender pantalla** (la TV
     como segunda pantalla), y si se ve el puntero.
   * **Calidad**: resolución, cuadros por segundo, retardo, calidad del video y codificador.
   * **Incluir el sonido del equipo**.
   * **Reproducir un archivo de video** en la TV.
   * **Más opciones**: iniciar con Windows, crear acceso directo en el escritorio y abrir
     la carpeta de registros.
6. **Abrir AirCast** muestra la ventana completa. Al cerrarla, AirCast sigue en la bandeja.
   **Salir** detiene la transmisión y cierra AirCast.

## Extender pantalla (la TV como segunda pantalla)

Es experimental: todavía no se probó con el controlador real.

En el menú, **Qué transmitir** > **Extender pantalla (pantalla virtual)**. Mientras
transmites, Windows tiene un monitor más que solo se ve en la TV: arrastra ventanas hacia
la derecha de tu pantalla para llevarlas a la TV. Al detener la transmisión, esa pantalla
desaparece. En **Tamaño de la pantalla extendida** eliges 1080p (el normal), 720p (letra
más grande y menos datos) o 4K (se envía a 20 Mbps o más). La TV recibe exactamente ese
tamaño, así que la **Resolución** de *Calidad* no se aplica en este modo.

Windows no puede crear un monitor por sí solo, así que AirCast usa el controlador gratuito
**Parsec Virtual Display Driver** de Parsec (hoy parte de Unity), firmado por Microsoft.
AirCast no lo incluye. La primera vez te pregunta antes de hacer nada; si aceptas, descarga
el instalador oficial de parsec.app (unos 500 KB), comprueba que sea exactamente el archivo
esperado y lo ejecuta. Windows te pide permiso una sola vez. Al instalarlo aceptas los
[términos de Parsec](https://parsec.app/terms). Si la descarga está bloqueada, el mismo
cuadro ofrece instalarlo con winget (`winget install -e --id Parsec.ParsecVDD`) o
descargarlo a mano.

Para quitarlo: *Más opciones* > *Quitar el controlador de pantalla virtual* abre
Configuración > Aplicaciones; desinstala ahí **Parsec Virtual Display Driver**.

Si AirCast se cierra de golpe, el controlador quita la pantalla solo, en un segundo. Al
suspender el equipo también desaparece: vuelve a transmitir al despertarlo.

## Lo que hay que saber

* **Hay un retardo de 3 a 7 segundos** con un Apple TV (unos 7 segundos o más con TVs
  de otras marcas, que además tardan unos 6 segundos más en empezar). El espejo en
  tiempo real que hace un Mac o un iPhone usa un cifrado privado de Apple (FairPlay) que
  ningún programa libre puede enviar. AirCast usa "AirPlay video", lo mismo que usa
  YouTube para mandar un video a la TV. Sirve para presentaciones, fotos y videos, no
  para juegos.
* **Una sola ventana**: se captura el lugar que ocupa la ventana al empezar. Si la mueves,
  detén y vuelve a transmitir. No puede estar minimizada.
* **TVs que no son Apple** (LG, Samsung, Sony, Hisense, TCL...): muchas aceptan video por
  AirPlay 2, pero algunas solo aceptan audio o fotos de equipos que no son Apple. A estas
  TVs AirCast les manda segmentos de 2 segundos, como pide Apple para TVs con AirPlay 2,
  sin importar el ajuste de retardo. Si la TV rechaza el video, el mensaje dice qué
  contestó a cada pedido de AirCast.
* **TVs con contraseña de AirPlay**: AirCast no puede enviar la contraseña junto con el
  video. Desactiva la contraseña en los ajustes de AirPlay de la TV.
* **"The TV did not finish setting up the AirPlay session"** o **"The TV rejected
  AirCast's credentials"**: casi siempre la TV olvidó el emparejamiento con AirCast. Usa
  *Dispositivo > Olvidar emparejamiento* y vuelve a transmitir; escribe el PIN nuevo.
* Si algo falla, el registro está en `%APPDATA%\AirCast\aircast.log` (menú *Más opciones*
  > *Abrir carpeta de registros*).

## Diagnóstico

En una ventana de PowerShell o cmd, en la carpeta del exe:

```
.\AirCast.exe doctor
```

Se abre una consola que muestra FFmpeg, los codificadores de tu tarjeta de video, si se
puede capturar el sonido, las pantallas, si el controlador de pantalla virtual está listo
y las TVs encontradas. Una captura de esa ventana sirve mucho para encontrar problemas.
`.\AirCast.exe vdd status` muestra solo el estado del controlador de pantalla virtual.

### Avisar de una TV que no funciona

```
.\AirCast.exe -v scan
.\AirCast.exe -v cast -d "Nombre de la TV"
```

Manda lo que muestran estas dos consolas junto con `aircast.log` (menú *Más opciones* >
*Abrir carpeta de registros*). Ahí se ve qué anuncia la TV, qué contestó a cada pedido de
AirPlay, cuándo (y con qué reproductor) descargó el video, y si pidió algo que AirCast no
tiene.
