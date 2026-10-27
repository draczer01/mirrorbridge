# AirCast: guía rápida

AirCast transmite la pantalla de Windows (completa o una sola ventana), con el sonido del
equipo, a un Apple TV o a una TV con AirPlay. Es un solo archivo, `AirCast.exe`, que ya
trae FFmpeg adentro: no hay que instalar nada.

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
   * **Qué transmitir**: la pantalla, o una sola ventana, y si se ve el puntero.
   * **Calidad**: resolución, cuadros por segundo, retardo, calidad del video y codificador.
   * **Incluir el sonido del equipo**.
   * **Reproducir un archivo de video** en la TV.
   * **Más opciones**: iniciar con Windows, crear acceso directo en el escritorio y abrir
     la carpeta de registros.
6. **Abrir AirCast** muestra la ventana completa. Al cerrarla, AirCast sigue en la bandeja.
   **Salir** detiene la transmisión y cierra AirCast.

## Lo que hay que saber

* **Hay un retardo de 3 a 7 segundos** con un Apple TV (unos 7 segundos o más con TVs
  de otras marcas). El espejo en tiempo real que hace un Mac o un iPhone usa un cifrado
  privado de Apple (FairPlay) que ningún programa libre puede enviar. AirCast usa
  "AirPlay video", lo mismo que usa YouTube para mandar un video a la TV. Sirve para
  presentaciones, fotos y videos, no para juegos.
* **Una sola ventana**: se captura el lugar que ocupa la ventana al empezar. Si la mueves,
  detén y vuelve a transmitir. No puede estar minimizada.
* **TVs que no son Apple** (LG, Samsung, Sony, Hisense, TCL...): muchas aceptan video por
  AirPlay 2, pero algunas solo aceptan audio o fotos de equipos que no son Apple. A estas
  TVs AirCast les manda segmentos de 2 segundos, como pide Apple para TVs con AirPlay 2,
  sin importar el ajuste de retardo. Si la TV rechaza el video, el mensaje dice qué
  contestó a cada pedido de AirCast.
* Si algo falla, el registro está en `%APPDATA%\AirCast\aircast.log` (menú *Más opciones*
  > *Abrir carpeta de registros*).

## Diagnóstico

En una ventana de PowerShell o cmd, en la carpeta del exe:

```
.\AirCast.exe doctor
```

Se abre una consola que muestra FFmpeg, los codificadores de tu tarjeta de video, si se
puede capturar el sonido, las pantallas y las TVs encontradas. Una captura de esa
ventana sirve mucho para encontrar problemas.
