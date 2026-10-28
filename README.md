# AirCast: AirPlay casting for Windows

AirCast casts your Windows screen (a whole display or one window) with system audio to an
Apple TV or other AirPlay video receiver. It finds receivers on your network, pairs with
the PIN shown on the TV, and streams with your GPU's hardware H.264 encoder.

It is a single `AirCast.exe` (FFmpeg included, nothing to install) that lives in the
system tray. Every feature is in the tray icon's menu; the full window only opens when
you choose **Open AirCast** in that menu. The interface follows the Windows display
language (Spanish or English). There is also a command line.

## What it can and cannot do

| Feature | Status |
|---|---|
| Discover Apple TVs / AirPlay receivers (mDNS, or by IP when mDNS is blocked) | Works |
| PIN pairing (Apple TV shows a 4-digit code, you type it in), saved for next time | Works |
| AirPlay password-protected receivers | Not for video: AirCast cannot send the AirPlay password with a video it plays from a URL, so turn the password off on the TV |
| Cast a display or a single window, with system audio | Works, with a 3 to 7 second delay on Apple TV, about 7 or more on other TVs (see below) |
| Hardware encoding: NVIDIA NVENC, Intel Quick Sync, AMD AMF, Media Foundation, x264 fallback | Auto-detected |
| Play a local video file on the TV | Works |
| Real-time screen mirroring like a Mac or iPhone (under 0.5 s delay) | Not possible yet, see below |

### Why there is a delay, and why this is not "true" mirroring

When a Mac or iPhone mirrors its screen, it uses a private AirPlay mirroring stream that
is protected with Apple's FairPlay encryption. Projects like UxPlay and RPiPlay only
implement the **receiving** side. Commercial Windows apps such as AirParrot have licensed
or reverse-engineered the sender. In 2026 a young open-source sender, doubletake (Go,
Linux only), reports mirroring working with Apple TV; porting that approach to Windows is
on the roadmap, but AirCast does not do it today.

AirCast uses the part of AirPlay that is open: **AirPlay video**, the same feature the
YouTube app uses to send a video to the TV. AirCast turns your screen into a live HLS
video stream, serves it from your PC, and tells the Apple TV to play it. This gives
excellent picture quality and stable sound. The cost is delay, because the Apple TV
buffers a few segments before it starts playing:

* **Low**: about 3 seconds. Good for presentations, photos and documents.
* **Balanced**: about 4 seconds.
* **Smooth**: about 7 seconds. Best for watching video, and handles weak Wi-Fi.

AirPlay TVs from other brands (LG, Samsung, TCL...) play the stream with their own
video players, so AirCast sends them, and any receiver that does not say what it is,
what Apple's rules for AirPlay 2 TVs ask for: 2-second segments and a 12-second playlist
whatever the Delay setting. Expect about 7 seconds of delay or more on those TVs. The cast
also takes about 6 seconds longer to start there, because AirCast waits until the
playlist holds its whole 12 seconds before it hands it to the TV.

It is not suited to gaming or anything where you interact with the mouse while you watch
the TV.

## Quick start (Windows 10/11)

1. Download **`AirCast.exe`** and double-click it. Windows SmartScreen may warn that the
   app is unrecognised (it is not code-signed): click *More info* > *Run anyway*.
2. An AirCast icon appears in the tray, next to the clock (it may be under the **^**
   arrow; drag it out to keep it visible). Left- or right-click it to open the menu.
3. When Windows Firewall asks, **allow AirCast on private networks**. The Apple TV has
   to download the stream from your PC.
4. In the menu, choose your TV under **Device**, then **Start casting**, and type the
   PIN the TV shows. You only pair once.
5. **More options** has *Start with Windows*, *Create desktop shortcut* and the log
   folder. **Open AirCast** shows the full window; closing it keeps AirCast in the tray.
   **Quit** stops any cast and exits.

### Casting a single window

AirCast captures the rectangle the window occupies on screen when the cast starts
(through Desktop Duplication, so browsers, video players and other GPU-drawn apps are
not black). Anything you drag over it is cast too, and if you move the window, stop and
start the cast again. Minimized windows cannot be cast.

### Running from source

```powershell
py -3 -m venv .venv
.venv\Scripts\activate
pip install -e .
aircast            # starts the tray app; aircast gui --open also shows the window
```

From source, FFmpeg comes from the `imageio-ffmpeg` package, or from any FFmpeg on PATH
or installed with winget, Chocolatey or Scoop.

## Command line

```text
AirCast.exe doctor                        # check FFmpeg, GPU encoders, audio capture, and scan
AirCast.exe scan                          # list receivers
AirCast.exe scan --host 192.168.1.50      # scan one IP directly (when mDNS is blocked)
AirCast.exe scan -v                       # also show what each receiver advertises
AirCast.exe pair -d "Living Room"         # pair using the on-screen PIN
AirCast.exe cast -d "Living Room"         # cast the main display with audio
AirCast.exe cast --monitor 2 --fps 60 --bitrate 15000 --latency smooth
AirCast.exe cast --window "PowerPoint"    # cast a single window
AirCast.exe cast --file movie.mp4         # play a video file on the TV
AirCast.exe forget -d "Living Room"       # remove the saved pairing
```

`AirCast.exe` is a windowed program, so a command opens its own console window, which
waits for Enter before closing. With output redirected (`AirCast.exe doctor > out.txt`)
it writes there instead, and asks for a PIN in a small dialog. From source, the same
commands are `aircast doctor` and so on.

## How it works

```
Desktop Duplication (ddagrab) or GDI capture ─┐
WASAPI loopback (system audio) ──TCP─────────┤ FFmpeg: GPU H.264 + AAC → live HLS (0.5–2 s segments)
                                             └──► local HTTP server on port 7878
                                                        │
pyatv: mDNS discovery → PIN pair-setup → pair-verify → AirPlay /play (URL of the stream) → Apple TV
```

| Module | Role |
|---|---|
| `aircast/devices.py` | Discovery, PIN pairing and connection through pyatv, with one shared credential store |
| `aircast/ffmpeg.py` | Finds FFmpeg, checks which encoders really work, builds the capture and encode command |
| `aircast/audio.py` | Captures system audio with WASAPI loopback and sends it to FFmpeg |
| `aircast/hls_server.py` | Serves only the playlist and segments, with no caching |
| `aircast/session.py` | Runs a cast from start to stop, watches every part, and cleans up |
| `aircast/airplay_patch.py` | Keeps pyatv's playback session open for an endless live stream, and plays on TVs that skip Apple's optional requests |
| `aircast/winmonitors.py` | Displays at their real resolution, window rectangles, and a job object that ends FFmpeg with AirCast |
| `aircast/gui/tray.py` | The tray icon and its menu, with every feature |
| `aircast/gui/window.py` | The full window, opened from the menu's *Open* |
| `aircast/gui/controller.py` | State shared by the menu and the window; runs pyatv on a background asyncio loop |
| `aircast/gui/app.py` | Single instance, startup and quit |
| `aircast/cli.py` | Command line |

Settings are saved in `%APPDATA%\AirCast\settings.json`, pairings in
`%APPDATA%\AirCast\pairings.json`, and the log in `%APPDATA%\AirCast\aircast.log`
(*More options* > *Open log folder*).

## Troubleshooting

* **No devices found**: make sure the PC and the TV are on the same network. Guest Wi-Fi
  and "AP isolation" block AirPlay. VPNs and Hyper-V/WSL network adapters can hijack mDNS,
  so use **Add by IP** (Apple TV: Settings > Network shows the IP).
* **No PIN on the TV**: on the Apple TV, open Settings > AirPlay and HomeKit > Allow
  Access, and choose "Everyone on the same network" or "Anyone on the same network".
* **TV shows a spinner, then gives up**: the TV cannot reach your PC. Allow AirCast (or
  inbound TCP 7878) in Windows Firewall, and set the network profile to *Private*. Only
  requests from the TV's own address count: opening the stream in a player on the PC or
  a phone does not keep a cast going.
* **Third-party AirPlay TVs** (LG, Samsung, Sony, Roku, Hisense, TCL...): many support
  AirPlay 2 video, but some only accept audio or photos from non-Apple senders. AirCast
  marks those as "audio only" or "restricted" in the device list. A TV that pairs but
  refuses video sent from a URL gets an error listing what it answered to each request.
* **"The TV downloaded the playlist but none of the video"**: the TV does not accept
  the video format. On an Apple TV choose *Quality > Delay > Smooth*; on other TVs try a
  lower resolution or 30 frames per second. If the message says that FFmpeg could not
  tell the TV which codecs the encoder produces, choose *Quality > Encoder > x264 (CPU)*
  (`--encoder libx264` on the command line).
* **"The TV did not finish setting up the AirPlay session"** or **"The TV rejected
  AirCast's credentials"**: most often the TV has dropped AirCast's pairing (a firmware
  update, or AirCast removed from the TV's list of AirPlay devices). Use *Device > Forget
  pairing*, then cast again and type the new PIN.
* **Reporting a TV that does not work**: run `AirCast.exe -v scan` and
  `AirCast.exe -v cast -d "TV name"`, and send their output with `aircast.log`. They show
  what the TV advertises, what it answered to each AirPlay request, when (and with
  which video player) it fetched the stream, and anything it asked for that AirCast
  does not serve.
* **Choppy video**: choose Smooth latency, lower the bitrate or resolution, or use 5 GHz
  Wi-Fi or Ethernet.
* **No audio**: `aircast doctor` shows whether system audio capture works. Audio is taken
  from the default playback device.
* **Black picture when casting a window**: pick *Entire display*, or make sure the window
  is not minimized. Protected video (Netflix in Edge, for example) is always black in any
  screen capture.
* The full log is in `%APPDATA%\AirCast\aircast.log`; `AirCast.exe -v cast ...` adds
  debug detail.

## Tests

```bash
pip install -e ".[dev]"
# End-to-end tests run against pyatv's own fake Apple TV (shipped only in its source package):
pip download --no-deps --no-binary :all: pyatv==0.18.0 -d /tmp/p && tar xzf /tmp/p/*.tar.gz -C /tmp/p
PYATV_SRC=/tmp/p/pyatv-0.18.0 pytest -q
```

The tests cover command building for every encoder and capture mode (including window
cropping and even-size rounding), a real FFmpeg run that produces H.264 and AAC HLS from
a test source with audio over TCP, the HTTP server (including path-traversal attempts,
the playlist fix for sub-second segments, and the log of what each client fetched or
asked for in vain), the stream for TVs of other brands (master playlist with CODECS,
FRAME-RATE and AVERAGE-BANDWIDTH, PROGRAM-DATE-TIME), the live-stream keepalive and its
shutdown however a cast ends, AirPlay 2 playback against mock receivers (PUT refused on
every protocol, error replies without CSeq, /play answering 500, SETUP refused, hung up
or never answered, everything refused, and a byte-for-byte check that Apple TVs still
get exactly pyatv's requests), when a cast gives up on a TV that never plays (and that
it waits for one that is only slow, counting only the TV's own requests), legacy and
HomeKit-style (AirPlay 2) PIN pairing, wrong-PIN rejection, a full pair-and-cast run
against the fake Apple TV, falling back from Desktop Duplication to GDI capture, and
stopping while a cast is still starting.

## Building AirCast.exe

On Windows: `.\packaging\build_exe.ps1`. It installs the pinned build requirements
(`packaging/requirements-build.txt`), takes FFmpeg 7.1 from `imageio-ffmpeg`, and runs
PyInstaller with `packaging/AirCast.spec` to produce a one-file `dist\AirCast.exe`.

On Linux the same exe can be built under Wine with a Windows Python:
`WINPY=/path/to/windows-python ./packaging/build_exe_wine.sh`.

CI (`.github/workflows/ci.yml`) runs the tests on Linux and builds the exe on Windows.

## Roadmap

1. **Lower delay**: Low-Latency HLS (partial segments) to get close to 1 second.
2. **Audio-only casting to HomePods and AirPlay speakers** (RAOP), which pyatv supports.
3. **Multi-room**: send the same stream to several Apple TVs.
4. **True mirroring** (about 100 ms delay): the AirPlay 2 mirroring sender with the
   FairPlay setup step, ported from doubletake, with capture and encoding kept on the GPU.
   See the research report in the project files (`research/airplay-mirroring-windows.html`).
5. **Lower CPU use now**: keep ddagrab frames on the GPU (no `hwdownload`) when the
   encoder accepts D3D11 frames.
