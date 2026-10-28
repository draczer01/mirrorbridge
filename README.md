# MirrorBridge: AirPlay casting for Windows

MirrorBridge casts your Windows screen (a whole display or one window) with system audio
to an Apple TV or another AirPlay TV or video receiver (LG, Samsung, TCL...). It finds
receivers on your network, pairs with the PIN shown on the TV, and streams with your GPU's
hardware H.264 encoder.

It is a single `MirrorBridge.exe` (FFmpeg included, nothing to install; only Extend screen
needs a driver, which MirrorBridge offers to install the first time) that lives in the
system tray. Every feature is in the tray icon's menu; the full window only opens when
you choose **Open MirrorBridge** in that menu. The interface follows the Windows display
language (Spanish or English). There is also a command line.

## Formerly AirCast

MirrorBridge was called AirCast up to version 0.2.1. To move over, quit AirCast (its tray
icon > **Quit**) and start `MirrorBridge.exe`. The first time it starts, MirrorBridge:

* copies AirCast's settings and TV pairings (`settings.json` and `pairings.json` in
  `%APPDATA%\AirCast`) to `%APPDATA%\MirrorBridge`, so the TV stays paired. It never changes
  the AirCast folder, and never replaces a file MirrorBridge already has;
* starts with Windows in AirCast's place, if AirCast did (but not if you had turned AirCast
  off in Task Manager's Startup tab).

Windows Firewall then asks again, this time for `MirrorBridge.exe`: allow it on private
networks. After that, delete `AirCast.exe` and the `AirCast` shortcut on your desktop, which
MirrorBridge leaves alone; once MirrorBridge casts to your TV, `%APPDATA%\AirCast` can go
too. AirCast and MirrorBridge do not know about each other, so if both run, there are two
icons in the tray.

## What it can and cannot do

| Feature | Status |
|---|---|
| Discover Apple TVs / AirPlay receivers (mDNS, or by IP when mDNS is blocked) | Works |
| PIN pairing (Apple TV shows a 4-digit code, you type it in), saved for next time | Works |
| AirPlay password-protected receivers | Not for video: MirrorBridge cannot send the AirPlay password with a video it plays from a URL, so turn the password off on the TV |
| Cast a display or a single window, with system audio | Works, with a 3 to 7 second delay on Apple TV, about 7 or more on other TVs (see below) |
| Extend screen: the TV as a second monitor | Experimental, not yet tested with the real driver; needs a free driver from Parsec, installed the first time |
| Hardware encoding: NVIDIA NVENC, Intel Quick Sync, AMD AMF, Media Foundation, x264 fallback | Auto-detected |
| Play a local video file on the TV | Works |
| Real-time screen mirroring like a Mac or iPhone (under 0.5 s delay) | Not possible yet, see below |

### Why there is a delay, and why this is not "true" mirroring

When a Mac or iPhone mirrors its screen, it uses a private AirPlay mirroring stream that
is protected with Apple's FairPlay encryption. Projects like UxPlay and RPiPlay only
implement the **receiving** side. Commercial Windows apps such as AirParrot have licensed
or reverse-engineered the sender. In 2026 a young open-source sender, doubletake (Go,
Linux only), reports mirroring working with Apple TV; porting that approach to Windows is
on the roadmap, but MirrorBridge does not do it today.

MirrorBridge uses the part of AirPlay that is open: **AirPlay video**, the same feature the
YouTube app uses to send a video to the TV. MirrorBridge turns your screen into a live HLS
video stream, serves it from your PC, and tells the Apple TV to play it. This gives
excellent picture quality and stable sound. The cost is delay, because the Apple TV
buffers a few segments before it starts playing:

* **Low**: about 3 seconds. Good for presentations, photos and documents.
* **Balanced**: about 4 seconds.
* **Smooth**: about 7 seconds. Best for watching video, and handles weak Wi-Fi.

AirPlay TVs from other brands (LG, Samsung, TCL...) play the stream with their own
video players, so MirrorBridge sends them, and any receiver that does not say what it is,
what Apple's rules for AirPlay 2 TVs ask for: 2-second segments and a 12-second playlist
whatever the Delay setting. Expect about 7 seconds of delay or more on those TVs. The cast
also takes about 6 seconds longer to start there, because MirrorBridge waits until the
playlist holds its whole 12 seconds before it hands it to the TV.

It is not suited to gaming or anything where you interact with the mouse while you watch
the TV.

## Quick start (Windows 10/11)

1. Download **`MirrorBridge.exe`** and double-click it. Windows SmartScreen may warn that
   the app is unrecognised (it is not code-signed): click *More info* > *Run anyway*.
2. A MirrorBridge icon appears in the tray, next to the clock (it may be under the **^**
   arrow; drag it out to keep it visible). Left- or right-click it to open the menu.
3. When Windows Firewall asks, **allow MirrorBridge on private networks**. The Apple TV has
   to download the stream from your PC.
4. In the menu, choose your TV under **Device**, then **Start casting**, and type the
   PIN the TV shows. You only pair once.
5. **More options** has *Start with Windows*, *Create desktop shortcut* and the log
   folder. **Open MirrorBridge** shows the full window; closing it keeps MirrorBridge in the
   tray. **Quit** stops any cast and exits.

### Casting a single window

MirrorBridge captures the rectangle the window occupies on screen when the cast starts
(through Desktop Duplication, so browsers, video players and other GPU-drawn apps are
not black). Anything you drag over it is cast too, and if you move the window, stop and
start the cast again. Minimized windows cannot be cast.

### Extend screen (the TV as a second screen)

Experimental: it has not yet been tested with the real driver.

Choose **What to cast** > **Extend screen (virtual display)**. While you cast, Windows has
one more monitor that only the TV shows, so the TV works as a second screen: drag windows
past the right edge of your screen to move them onto it. The screen disappears when the
cast stops. **Extended screen size** picks 1080p (the default), 720p (bigger text and a
lighter stream, like AirParrot's "force 720p") or 4K (sent at 20 Mbps or more). The TV gets
exactly that size, so the Resolution setting does not apply in this mode.

Windows cannot add a monitor by itself, so MirrorBridge uses the free **Parsec Virtual
Display Driver**, a Microsoft-signed driver from Parsec (now part of Unity). MirrorBridge
does not include it. The first time, MirrorBridge asks before doing anything; if you agree,
it downloads Parsec's official installer from parsec.app (about 500 KB), checks that it is
exactly the expected file (SHA-256), and runs it. Windows asks for permission once;
MirrorBridge itself never runs as administrator. By installing the driver you accept
[Parsec's terms](https://parsec.app/terms). If the download is blocked, the same dialog
offers `winget install -e --id Parsec.ParsecVDD` or a manual download.

To remove the driver, choose *More options* > *Remove the virtual display driver*. It opens
Settings > Apps, where you uninstall **Parsec Virtual Display Driver**.

If MirrorBridge crashes or is ended from Task Manager, the driver removes the screen by
itself about a second later. Sleep also removes it; start the cast again after waking the
PC.

### Running from source

```powershell
py -3 -m venv .venv
.venv\Scripts\activate
pip install -e .
mirrorbridge       # starts the tray app; mirrorbridge gui --open also shows the window
```

From source, FFmpeg comes from the `imageio-ffmpeg` package, or from any FFmpeg on PATH
or installed with winget, Chocolatey or Scoop.

## Command line

```text
MirrorBridge.exe doctor                        # check FFmpeg, GPU encoders, audio capture, and scan
MirrorBridge.exe scan                          # list receivers
MirrorBridge.exe scan --host 192.168.1.50      # scan one IP directly (when mDNS is blocked)
MirrorBridge.exe scan -v                       # also show what each receiver advertises
MirrorBridge.exe pair -d "Living Room"         # pair using the on-screen PIN
MirrorBridge.exe cast -d "Living Room"         # cast the app's choice (at first, the main display) with audio
MirrorBridge.exe cast --monitor 2 --fps 60 --bitrate 15000 --latency smooth
MirrorBridge.exe cast --window "PowerPoint"    # cast a single window
MirrorBridge.exe cast --file movie.mp4         # play a video file on the TV
MirrorBridge.exe cast --extend                 # the TV as a second screen (a new virtual monitor)
MirrorBridge.exe cast --extend --extend-mode 4k   # ... at 4K (1080p, 720p or 4k)
MirrorBridge.exe vdd status                    # is the virtual display driver installed and working?
MirrorBridge.exe vdd install                   # download and install it (asks first; one UAC prompt)
MirrorBridge.exe forget -d "Living Room"       # remove the saved pairing
```

`cast` starts from the app's settings, so without `--monitor`, `--window` or `--extend` it
casts what the app has selected under *What to cast*, the virtual screen included.

`MirrorBridge.exe` is a windowed program, so a command opens its own console window, which
waits for Enter before closing. With output redirected (`MirrorBridge.exe doctor > out.txt`)
it writes there instead, and asks for a PIN in a small dialog. From source, the same
commands are `mirrorbridge doctor` and so on.

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
| `mirrorbridge/devices.py` | Discovery, PIN pairing and connection through pyatv, with one shared credential store |
| `mirrorbridge/ffmpeg.py` | Finds FFmpeg, checks which encoders really work, builds the capture and encode command |
| `mirrorbridge/audio.py` | Captures system audio with WASAPI loopback and sends it to FFmpeg |
| `mirrorbridge/hls_server.py` | Serves only the playlist and segments, with no caching |
| `mirrorbridge/session.py` | Runs a cast from start to stop, watches every part, and cleans up |
| `mirrorbridge/airplay_patch.py` | Keeps pyatv's playback session open for an endless live stream, and plays on TVs that skip Apple's optional requests |
| `mirrorbridge/winmonitors.py` | Displays at their real resolution and GPU, window rectangles, and a job object that ends FFmpeg with MirrorBridge |
| `mirrorbridge/vdisplay.py` | Extend screen: adds and removes the virtual monitor through the Parsec driver, keeps it alive, installs the driver |
| `mirrorbridge/gui/tray.py` | The tray icon and its menu, with every feature |
| `mirrorbridge/gui/window.py` | The full window, opened from the menu's *Open* |
| `mirrorbridge/gui/controller.py` | State shared by the menu and the window; runs pyatv on a background asyncio loop |
| `mirrorbridge/gui/extend.py` | The Extend screen choice: driver check, consent and install dialogs |
| `mirrorbridge/gui/app.py` | Single instance, startup and quit |
| `mirrorbridge/migration.py` | Moves settings, pairings and Start with Windows over from the app's old name (see above) |
| `mirrorbridge/cli.py` | Command line |

Settings are saved in `%APPDATA%\MirrorBridge\settings.json`, pairings in
`%APPDATA%\MirrorBridge\pairings.json`, and the log in
`%APPDATA%\MirrorBridge\mirrorbridge.log` (*More options* > *Open log folder*). While a
virtual screen exists it is also noted in `%APPDATA%\MirrorBridge\vdd_state.json`, so the
next start can remove one a crash left behind (possible only when another program, such as
the Parsec app, keeps the driver awake). The downloaded driver installer stays in
`%LOCALAPPDATA%\MirrorBridge\downloads`.

## Troubleshooting

* **No devices found**: make sure the PC and the TV are on the same network. Guest Wi-Fi
  and "AP isolation" block AirPlay. VPNs and Hyper-V/WSL network adapters can hijack mDNS,
  so use **Add by IP** (Apple TV: Settings > Network shows the IP).
* **No PIN on the TV**: on the Apple TV, open Settings > AirPlay and HomeKit > Allow
  Access, and choose "Everyone on the same network" or "Anyone on the same network".
* **TV shows a spinner, then gives up**: the TV cannot reach your PC. Allow MirrorBridge
  (or inbound TCP 7878) in Windows Firewall, and set the network profile to *Private*. Only
  requests from the TV's own address count: opening the stream in a player on the PC or
  a phone does not keep a cast going.
* **Third-party AirPlay TVs** (LG, Samsung, Sony, Roku, Hisense, TCL...): many support
  AirPlay 2 video, but some only accept audio or photos from non-Apple senders.
  MirrorBridge marks those as "audio only" or "restricted" in the device list. A TV that
  pairs but refuses video sent from a URL gets an error listing what it answered to each
  request.
* **"The TV downloaded the playlist but none of the video"**: the TV does not accept
  the video format. On an Apple TV choose *Quality > Delay > Smooth*; on other TVs try a
  lower resolution (with Extend screen, *What to cast > Extended screen size > 720p*) or
  30 frames per second. If the message says that FFmpeg could not tell the TV which
  codecs the encoder produces, choose *Quality > Encoder > x264 (CPU)*
  (`--encoder libx264` on the command line).
* **"The TV did not finish setting up the AirPlay session"** or **"The TV rejected
  MirrorBridge's credentials"**: most often the TV has dropped MirrorBridge's pairing (a
  firmware update, or MirrorBridge removed from the TV's list of AirPlay devices). Use
  *Device > Forget pairing*, then cast again and type the new PIN.
* **Reporting a TV that does not work**: run `MirrorBridge.exe -v scan` and
  `MirrorBridge.exe -v cast -d "TV name"`, and send their output with `mirrorbridge.log`.
  They show what the TV advertises, what it answered to each AirPlay request, when (and
  with which video player) it fetched the stream, and anything it asked for that
  MirrorBridge does not serve.
* **Choppy video**: choose Smooth latency, lower the bitrate or resolution, or use 5 GHz
  Wi-Fi or Ethernet.
* **No audio**: `mirrorbridge doctor` shows whether system audio capture works. Audio is
  taken from the default playback device.
* **Black picture when casting a window**: pick *Entire display*, or make sure the window
  is not minimized. Protected video (Netflix in Edge, for example) is always black in any
  screen capture.
* **Extend screen**: if Windows does not show the new screen, press Win+P and choose
  *Extend*. If MirrorBridge says the virtual screen is turned off, enable it in Device
  Manager > Display adapters > Parsec Virtual Display Adapter. `MirrorBridge.exe vdd status`
  shows the driver's state. If the virtual screen is drawn by a GPU other than the main one
  (DXGI adapter 0), MirrorBridge captures it with GDI, which uses more CPU.
* The full log is in `%APPDATA%\MirrorBridge\mirrorbridge.log`; `MirrorBridge.exe -v cast ...`
  adds debug detail.

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
stopping while a cast is still starting. For Extend screen they cover the driver's IOCTL
codes and payloads, IOCTL time-outs and cancels, reading the driver's state, finding its
monitor and switching Duplicate or Second screen only to Extend, pings that never wait for
the presence check, the mode fallback, the installer check (a cached file included), asking
before any download, the clean-up after a crash, the virtual screen being removed only
after FFmpeg has stopped (on stop, failure, cancel, a lost screen and a TV that stops
fetching), and another brand's TV getting its own stream of the virtual screen, all with a
fake driver, so no real monitor is ever created. The move from the old name is covered
too: settings and pairings copied byte for byte with the old folder left exactly as it was,
a file MirrorBridge already has never replaced, a missing or unreadable old folder, the TV
still paired afterwards, and the Start with Windows entry (on, off in Task Manager, another
program's, or none) against a fake registry. One more test fails if the old name shows up
anywhere it is not needed.

## Building MirrorBridge.exe

On Windows: `.\packaging\build_exe.ps1`. It installs the pinned build requirements
(`packaging/requirements-build.txt`), takes FFmpeg 7.1 from `imageio-ffmpeg`, and runs
PyInstaller with `packaging/MirrorBridge.spec` to produce a one-file
`dist\MirrorBridge.exe`. To bundle another FFmpeg build instead, set `MIRRORBRIDGE_FFMPEG`
to its `ffmpeg.exe` (`AIRCAST_FFMPEG`, the name from before the rename, still works).

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
