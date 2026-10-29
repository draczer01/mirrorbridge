# MirrorBridge: AirPlay casting for Windows

MirrorBridge casts your Windows screen (a whole display or one window) with system audio
to an Apple TV or another AirPlay TV or video receiver (LG, Samsung, TCL...). It finds
receivers on your network, pairs with the PIN shown on the TV, and streams with your GPU's
hardware H.264 encoder.

It is a single `MirrorBridge.exe` (FFmpeg included, nothing to install; only Extend screen
needs a driver, which MirrorBridge offers to install the first time) that lives in the
system tray. Every feature is in the tray icon's menu; the full window only opens when
you choose **Open MirrorBridge** in that menu. The interface is in English, or in Spanish
once you choose it under **Language / Idioma**. There is also a command line.

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

AirCast followed the Windows display language; MirrorBridge starts in English. To have it in
Spanish again, choose **Language / Idioma** > **Español** in its menu once.

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
| Real-time screen mirroring like a Mac or iPhone (under 0.5 s delay) | Experimental, for AirPlay 2 TVs of other brands: *Show my screen on (your TV)* in the tray menu, or `mirror` (see [Screen mirroring](#screen-mirroring-experimental)); not possible with Apple TV yet |

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
4. In the menu, choose your TV under **Device**, then **Show my screen on (your TV)**, and
   type the PIN the TV shows. You only pair once. A TV of another brand gets screen mirroring,
   a fraction of a second behind (see [Screen mirroring](#screen-mirroring-experimental)); an
   Apple TV gets the cast, a few seconds behind.
5. **More options** has *Transmit (old way, 3-7 s delay)*, the cast, for a TV that does not
   show the screen the first way; *Start with Windows*, *Create desktop shortcut* and the log
   folder. **Open MirrorBridge** shows the full window; closing it keeps MirrorBridge in the
   tray. **Quit** stops any cast and exits.

### Language

MirrorBridge is in English until you choose another language: in the tray menu, choose
**Language / Idioma** > **Español** (and **English** to go back). The menu, the window, and
every dialog and notification from then on switch at once, and MirrorBridge keeps the choice
for its next start. The full window has the same choice at the bottom. It does not follow the
Windows display language, so it starts in English on a Spanish Windows too. English and
Spanish are the only languages for now; the command line is always in English.

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
cast stops. *Mirror screen* sends it too, a fraction of a second behind instead of a few
seconds (see *Screen mirroring* below): for PowerPoint's Presenter View, for example, with
the notes on the laptop and the slides on the TV. **Extended screen size** picks 1080p (the
default), 720p (bigger text and a lighter stream, like AirParrot's "force 720p") or 4K. A
cast sends exactly that size (4K at 20 Mbps or more). Mirror screen adds it at 1080p at most
(at 1080p when 4K is chosen: shrinking 4K on every frame made frames later than the TV waits
for) and fits it into the TV's screen, so the Resolution setting does not apply in this mode.

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
itself about a second later. Sleep also removes it; start the cast or Mirror screen again
after waking the PC.

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
MirrorBridge.exe probe "Living Room"           # test which ways of sending video the TV takes, for a bug report
```

`cast` starts from the app's settings, so without `--monitor`, `--window` or `--extend` it
casts what the app has selected under *What to cast*, the virtual screen included.

`MirrorBridge.exe` is a windowed program, so a command opens its own console window, which
waits for Enter before closing. With output redirected (`MirrorBridge.exe doctor > out.txt`)
it writes there instead, and asks for a PIN in a small dialog. From source, the same
commands are `mirrorbridge doctor` and so on.

## Screen mirroring (experimental)

AirPlay 2 TVs of other brands (TCL, Roku, Samsung, LG...) can also be sent the screen the way
an iPhone mirrors it: each frame goes to the TV as soon as FFmpeg has encoded it, over a
connection of its own, with the time to show it, so the picture is a fraction of a second
behind (100 to 200 ms is the aim) instead of the seconds a cast takes. It is new and not yet
tried on a real TV.

### Mirror screen, from the tray menu

1. Choose the TV under **Device** and pair it (*Device > Pair with PIN…*: type in the PIN the
   TV shows).
2. Once, run **More options > Test this TV…**. Its last test mirrors a moving test pattern
   for about 10 seconds: when a notification says *Watch the TV now*, look for it. What worked
   is saved for the TV, so Mirror screen asks for it straight away (see *Reporting a TV that
   does not work* under Troubleshooting).
3. Choose **Show my screen on (your TV)**, right under the status line (it pairs first if
   step 1 was skipped). As for a cast, the tray icon's tooltip says *Connecting to the TV…*,
   then *Mirroring to (your TV)*, and a notification says when it starts, when it stops, and
   why if it fails. **Stop mirroring**, in the same place, or **Quit** ends it.

The item's tooltip says what it will do. A receiver that does not offer screen mirroring gets
the cast, and so do Apple TVs and Macs, whose screen mirroring needs FairPlay (coming in a later
version). If screen mirroring fails before the picture reaches the TV, the next way is tried
without asking. The cast is also under *More options > Transmit (old way, 3-7 s delay)*, with
sound. One thing runs at a time: while a cast, Mirror screen or *Test this TV* runs, the others
are greyed out until it stops. The full window's big button does the same.

**Mirror screen settings**, in the tray menu, change the next Mirror screen:

* *Latency*: how long after a frame was captured the TV shows it. *Low* is 60 ms, for a cable
  or a strong Wi-Fi signal; *Normal* (the default) is what is saved for the TV, 100 ms;
  *Smooth* is 200 ms, a little more delay and fewer skips when the Wi-Fi falters. A shorter
  wait only lowers the delay if the TV honours the frames' times.
* *Frame rate*: *Automatic* (the default) is 60 fps when the graphics card encodes and Desktop
  Duplication captures, and 30 fps with x264 (the processor) or GDI capture; *30 fps* and
  *60 fps* keep to that rate. It is never more than the TV takes. *Quality*'s frame rate and
  delay are the cast's. On a Google Cast TV the main item mirrors by Google Cast, which these
  two do not change yet: it has the PC's sound (*Include computer sound*) and *Quality*'s frame
  rate, so while one is chosen they are named without "when transmitting".

**What to send back** after trying it on your TV:

* the `probe-<date>-<time>.log` that *Test this TV* wrote, and whether the moving test pattern
  appeared on the TV during its last test, and moved smoothly;
* `mirrorbridge.log` after a few minutes of Mirror screen (*More options > Open log folder*):
  every 5 seconds it notes where the frames' time went;
* how far behind the TV is: while it mirrors, choose **More options > Debug > Latency meter**.
  It shows a big clock (seconds and milliseconds, with the tenths as a row of cells) on every
  screen, the virtual one of *Extend screen* too. Take a few phone photos with the PC's screen
  and the TV in them: the difference between the two clocks is the delay. On the fastest
  screen the clock is the most precise (a 200 Hz screen shows a new time every 5 ms). Esc or a
  click closes it;
* `%APPDATA%\MirrorBridge\mirroring.json`, which holds what worked for the TV.

Next to *Latency meter*, **More options > Debug (for testing)** has four switches for tests.
They start from the defaults, and change the next Mirror screen (and `mirror`), not *Test this
TV* or `mirror --probe`:
*TV wait (test)* asks the TV to wait 60 to 300 ms after a frame was captured before it shows
it, instead of what *Latency* says (*Automatic*; choosing a Latency puts it back there, and a
test value is never saved); *Keyframes* sends a complete picture only when needed (the
default), every 10 seconds or every 2 seconds, as before; *Scaling* resizes the picture on the
graphics card when it can (*Automatic*, the default) or on the processor (*Processor (as
before)*); *Show frames on arrival (test)* asks the TV to show each frame as soon as it comes
instead of at its time. The log says, as each session starts, which ones it used, and each
time FFmpeg starts again, why.

### From the command line

```text
MirrorBridge.exe mirror --probe -d "Living Room"      # find out how the TV takes it (10 s of a test pattern)
MirrorBridge.exe mirror -d "Living Room"              # mirror the screen until Ctrl+C
MirrorBridge.exe mirror --test-pattern --seconds 30   # a moving test pattern instead of the screen
MirrorBridge.exe mirror --extend                      # the TV as a second screen (a new virtual monitor)
MirrorBridge.exe mirror --extend --extend-mode 720p   # ... at 720p (1080p, 720p or 4k)
MirrorBridge.exe mirror --monitor 2                   # display 2 (--window "PowerPoint": one window)
```

As `cast` does, `mirror` starts from the app's settings: without `--monitor`, `--window` or
`--extend` it mirrors what the app has selected under *What to cast*, the virtual screen
included.

### What it needs, and what it does

* Pair the TV first (`pair`), as for a cast. Apple TVs and Macs need FairPlay for screen
  mirroring, which MirrorBridge does not have, so they are turned down at once.
* It mirrors what *What to cast* names: a display or a window at the TV's screen size, up to
  1080p or the *Quality > Resolution* setting, or with *Extend screen* the virtual screen. That
  is added as for a cast (the tray says *Creating the virtual screen…*, and a missing driver
  gets the same offer to install it), at its *Extended screen size* but 1080p at most (4K is
  added at 1080p), fitted into the TV's screen (Resolution does not apply, as for a cast). It
  stays for the whole session and goes when the session ends; if it goes away first (sleep),
  the session ends and says so. *Test this TV* and `mirror --probe` never add it. Each is sent
  with the encoder and bitrate of the *Quality* menu, at the frame rate of *Mirror screen
  settings*. There is no sound yet. If FFmpeg cannot capture or encode at the start (the
  graphics driver refuses ddagrab, the GPU encoder is busy), it goes on with GDI capture, then
  with x264 (CPU), as a cast does.
* With NVENC and Desktop Duplication the picture stays on the graphics card: at the TV's size
  it goes from the capture to the encoder untouched, and otherwise Windows Graphics Capture
  (FFmpeg's `gfxcapture`, which the FFmpeg 8.1.1 inside `MirrorBridge.exe` has) takes the
  screen at the TV's size, scaled on the graphics card; another FFmpeg without it scales on the
  processor. That capture gives a frame only when the screen changes, at most at the frame
  rate, so a screen that stays as it is sends no new frames; MirrorBridge does not end the
  session for that, but how long a TV keeps a still picture is still to be tried. MirrorBridge's
  own command for this capture has not yet run on real hardware. (FFmpeg's `scale_d3d11` is not
  used: on an RTX 4080 SUPER with driver 621.01 it could not make its picture.) If the capture
  ends before its first picture, or the picture comes out at another size than asked for, that
  display goes back to the processor until MirrorBridge quits; when Windows takes the capture
  away instead, it is started again as below. Quick Sync, AMF and Media Foundation scale on the
  processor.
* A keyframe (a complete picture) goes only when it is needed: at the start, and after frames
  were dropped, when FFmpeg is started again for a fresh one at once (at most every 2 seconds),
  whether or not the screen changes meanwhile. A keyframe every 2 seconds made text blur every 2
  seconds, because in a one-frame buffer it gets far fewer bits than the frames around it. Quick
  Sync, AMF and Media Foundation, not yet tried that way, still make one every 10 seconds.
* When Windows takes the screen capture away (a display driver installed or removed, a logoff,
  a change of resolution, a UAC prompt), FFmpeg is started again on the same display, looked
  up again (the virtual screen too), up to five times in 10 seconds; only then does the session
  end, saying that Windows stopped the screen capture. The log has FFmpeg's last 30 lines
  whenever it stops.
* The TV asks the PC for the time over UDP: allow MirrorBridge, or inbound UDP ports
  7880-7889, in Windows Firewall on private networks, or the TV shows nothing.
  The experimental PTP timing (`mirror --timing ptp`, or `MIRRORBRIDGE_TIMING=ptp`), where
  MirrorBridge is the TV's PTP clock, also needs inbound UDP 319 and 320; if the TV does not
  follow that clock within a few seconds (`MIRRORBRIDGE_PTP_LOCK_S`), the session goes on with
  NTP, and says why in the log. Nothing about PTP is saved for the next session.
* TVs differ in the order they want the session set up in, whether they need an (empty) audio
  stream, whether they offer PTP timing, and how they want the H.264 header to end.
  `mirror --probe` tries each, prints what the TV answered, and saves what worked in
  `%APPDATA%\MirrorBridge\mirroring.json`, which Mirror screen and every `mirror` after it ask
  for first. The last test of *Test this TV* does the same in 75 seconds: the test pattern goes
  as soon as the TV takes a SETUP, and the other shapes are tried in the time left (the report
  names any it had no time for). If screen mirroring fails, please send that output with
  `mirrorbridge.log`. Before a probe, a TV that drops the video at once gets one more try with
  the other H.264 header ending.
* When the network holds the video up for a moment, as Wi-Fi does now and then, the frames that
  waited still go, a little late, and the TV catches up. Only when frames have been late for
  more than about a second (the network or the TV cannot keep up) are they dropped, up to a
  keyframe that is on time, instead of piling up, so the picture skips but stays current. A TV
  that takes no video for 5 seconds ends the session with an error; ending mirroring on the TV
  just stops it.
* Every 5 seconds the log says where the frames' time went, from FFmpeg's pipe to the network,
  as median and 95th percentile per stage, with the frame rate FFmpeg delivers (and a warning
  when it falls short, except with Windows Graphics Capture, which gives frames only as the
  screen changes), what was waiting to go to the TV, how many frames were dropped or sent
  late, and how often the TV asked for the time.

## How it works

```
Desktop Duplication (ddagrab) or GDI capture ─┐
WASAPI loopback (system audio) ──TCP─────────┤ FFmpeg: GPU H.264 + AAC → live HLS (0.5–2 s segments)
                                             └──► local HTTP server on port 7878
                                                        │
pyatv: mDNS discovery → PIN pair-setup → pair-verify → AirPlay /play (URL of the stream) → Apple TV
                                                      └► or the play queue (/command) → LG, Samsung, TCL...
```

| Module | Role |
|---|---|
| `mirrorbridge/devices.py` | Discovery, PIN pairing and connection through pyatv, with one shared credential store |
| `mirrorbridge/ffmpeg.py` | Finds FFmpeg, checks which encoders really work, builds the capture and encode command |
| `mirrorbridge/audio.py` | Captures system audio with WASAPI loopback and sends it to FFmpeg |
| `mirrorbridge/hls_server.py` | Serves only the playlist and segments, with no caching |
| `mirrorbridge/session.py` | Runs a cast from start to stop, watches every part, and cleans up |
| `mirrorbridge/airplay_patch.py` | Keeps pyatv's playback session open for an endless live stream, plays on TVs that skip Apple's optional requests, and picks /play or the play queue for each receiver |
| `mirrorbridge/airplay_queue.py` | AirPlay video through the play queue, for TVs that have no POST /play: one request at a time, each answer matched to its request, and the TV's event channel |
| `mirrorbridge/probe.py` | *Test this TV*: tries each way of sending the TV video, screen mirroring included, and writes a report for a bug report |
| `mirrorbridge/mirror/` | Screen mirroring (experimental): pair-verify, the session's SETUP in the shapes TVs take, the clock the TV asks, FFmpeg's frames encrypted to the TV as they come, the probe, and what worked for each TV |
| `mirrorbridge/winmonitors.py` | Displays at their real resolution and GPU, window rectangles, and a job object that ends FFmpeg with MirrorBridge |
| `mirrorbridge/vdisplay.py` | Extend screen: adds and removes the virtual monitor through the Parsec driver, keeps it alive, installs the driver |
| `mirrorbridge/extended.py` | The virtual monitor of one cast or Mirror screen session: added, found among the displays FFmpeg captures, watched, and removed when the session ends |
| `mirrorbridge/gui/tray.py` | The tray icon and its menu, with every feature |
| `mirrorbridge/gui/window.py` | The full window, opened from the menu's *Open* |
| `mirrorbridge/gui/controller.py` | State shared by the menu and the window; runs pyatv on a background asyncio loop |
| `mirrorbridge/gui/extend.py` | The Extend screen choice: driver check, consent and install dialogs |
| `mirrorbridge/gui/app.py` | Single instance, startup and quit |
| `mirrorbridge/gui/i18n.py` | The languages the *Language / Idioma* menu offers, and every word the menu and the window show in each (English when a language lacks one) |
| `mirrorbridge/text.py` | Messages that say which of those words they are, so a failed cast is explained in Spanish too; the command line stays in English |
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
  MirrorBridge marks those as "audio only" or "restricted" in the device list. Many of
  these TVs (TCL and LG among them) have no `POST /play`, the first AirPlay video request,
  and answer it with 404: MirrorBridge hands them the stream through the *play queue*
  instead (AirPlay video v2), the way an iPhone does. It picks that from what the TV
  advertises, also tries it once on any TV that answers `/play` with 404, and remembers
  which way worked until it quits. A TV that pairs but refuses video sent from a URL gets
  an error listing what it answered to each request.
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
* **Reporting a TV that does not work**: choose the TV under *Device*, then *More
  options* > *Test this TV…* (or run `MirrorBridge.exe probe "TV name"`). It takes about
  three minutes: watch the TV, and if it asks whether to allow MirrorBridge, choose *Allow*.
  MirrorBridge tries four ways of sending it video, one after the other: `POST /play` with
  Apple's public test video, the play queue with the same video, the play queue with a short
  cast of MirrorBridge's own test pattern from this PC, and last screen mirroring with a
  moving test pattern for about 10 seconds (nothing from your screen in any of them). Watch
  the TV for that moving pattern when a notification says *Watch the TV now*; a TV that needs
  FairPlay (an Apple TV), or that MirrorBridge is not paired with, skips that test. Then
  MirrorBridge says what each one did (for screen mirroring, whether the TV accepted the
  session and kept the video open, and which SETUP order, H.264 header ending and timing
  worked, which it saves for Mirror screen), whether Mirror screen should work, and
  where its report is: `%APPDATA%\MirrorBridge\probe-<date>-<time>.log` (*Open log folder* in
  the result). Send that file with `mirrorbridge.log`, and say whether the moving test pattern
  appeared. It shows what the TV advertises, what it answered to each AirPlay request, what it
  said while it played, whether (and with which video player) it fetched the stream from this
  PC, anything it asked for that MirrorBridge does not serve, and each step of setting up
  screen mirroring. `MirrorBridge.exe -v scan` adds what every receiver on the network
  advertises.
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
or never answered, everything refused, and a byte-for-byte check that Apple TVs, and
receivers that do not say what they have, still get exactly pyatv's requests), the play
queue against a fake TV that has no /play, on real sockets with HAP pair-verify and
encryption (the route each receiver's feature bits pick, the exact requests and headers,
the 404 fallback on a new connection and the route remembered, refusals and silence
explained, the keep-alive never overlapping a command, a late answer waited for before the
next request and never taken for its answer, a TV that goes quiet or ends a video file before
playing it, every message on the event channel answered, the TV's player errors, BACK, the
end of the video (and of a live stream, which has none) and a closed channel ending a cast,
and the video stopped and the session torn down however it ends), *Test this TV* against the
same fake TV with a real cast of the test pattern (each test's verdict, a conclusion that
only names the firewall or HTTPS when the TV played Apple's video but never reached the PC,
the report file, the command line's output, Stop and Quit waiting for the test to tear down,
and the dialogs in Spanish), CORS for players in a web page (a preflight counting as the TV
reaching the PC), when a cast gives up on a TV that never plays (and that it waits for one
that is only slow, or asking whether to allow MirrorBridge, counting only the TV's own
requests), legacy and HomeKit-style (AirPlay 2) PIN pairing, wrong-PIN rejection, a full
pair-and-cast run against the fake Apple TV, falling back from Desktop Duplication to GDI
capture, and stopping while a cast is still starting. For Extend screen they cover the driver's IOCTL
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
anywhere it is not needed. Every message a cast, pairing or FFmpeg can end with is checked
to be in `gui/i18n.py` in English and Spanish, with the same English as the command line
and the same values, and the hints name the Spanish menu items. The language tests check that
a first start, and a settings file from before there was a Language menu, are in English, and
that a language lacking a string shows it in English. With the real tray menu and window built
offscreen, they check that choosing Español changes both at once (the status line and the
dialogs' buttons too), that the choice is kept for the next start, and that a language added
only to `gui/i18n.py` appears in both.

Screen mirroring is tested piece by piece against fixed vectors and FFmpeg's own output (the
avcC record with both endings, the 128-byte header, the frame counter nonce, the HKDF video key
checked against an independent HKDF and ChaCha20-Poly1305, NTP answers, the FLV reader, the
frame clock), and as a whole against a TV in the same process built on pyatv's server-side
pairing (`tests/fake_mirror_tv.py`), which behaves like a TCL 55QM7L: it refuses the
control-first SETUP, needs the audio stream, answers PTP without a ClockID, asks for the time,
opens an event channel and answers `/feedback` with nothing. It decrypts the video, and FFmpeg
decodes every frame of it. The probe is run against it too, with TVs that drop the pairing,
drop the video or refuse it, never ask for the time, or take only the other avcC ending.
*Mirror screen* is tested with the real tray menu and window built offscreen: the main item
mirrors a TV that does not need FairPlay, pairing first, and casts to an Apple TV (the tooltip
saying which, in English and Spanish), the cast is under *More options*, *Mirror screen
settings* and the Debug switches save what they say, it runs against the same fake TV on the
controller's own loop through Connecting, Casting and Stopping, Stop and Quit tear the session
down with no task left running, a failure is explained in Spanish, ending mirroring on the TV
is said, and only one cast, mirroring session or test runs at a time. *Test this TV*'s last
test runs against it as well: the TV that keeps the video, drops it, refuses the session or has
dropped the pairing, the time limit, Stop, the skip for a TV that needs FairPlay, and the
command line's output.

## Building MirrorBridge.exe

On Windows: `.\packaging\build_exe.ps1`. It installs the pinned build requirements
(`packaging/requirements-build.txt`), gets FFmpeg 8.1.1 (gyan.dev's essentials build, which
has `gfxcapture` to capture the screen scaled on the graphics card), and runs PyInstaller with
`packaging/MirrorBridge.spec` to produce a one-file `dist\MirrorBridge.exe`.

`packaging/fetch_ffmpeg.py` gets FFmpeg: it downloads the zip that `packaging/ffmpeg_pin.json`
names (109 MB, once), checks its size and SHA-256 and those of the `ffmpeg.exe` inside, and
takes out only `ffmpeg.exe`, to `build\ffmpeg_811\`. Where downloads are blocked, give it a
copy of the zip: `.\packaging\build_exe.ps1 -FFmpegZip C:\path\ffmpeg-8.1.1-essentials_build.zip`.
Before PyInstaller runs, the script runs the `ffmpeg.exe` that will go inside and stops unless
it is 8.1.1 and has `gfxcapture`. The spec bundles that file only: `MIRRORBRIDGE_FFMPEG`
(or `AIRCAST_FFMPEG`, the name from before the rename) set to another `ffmpeg.exe`, or else
the FFmpeg 7.1 of `imageio-ffmpeg`, goes inside only with
`.\packaging\build_exe.ps1 -AllowOtherFFmpeg` (which sets `MIRRORBRIDGE_OTHER_FFMPEG=1` for the
build), and then the checks only warn.

On Linux the same exe can be built under Wine with a Windows Python:
`WINPY=/path/to/windows-python ./packaging/build_exe_wine.sh` (it fetches FFmpeg with the
Linux Python first; `FFMPEG_ZIP=/path/to/the.zip` takes a copy already there).

A release (`.github/workflows/release.yml`) also publishes FFmpeg's source, `ffmpeg-8.1.1.tar.xz`,
which FFmpeg's GPL asks for: `fetch_ffmpeg.py --source` downloads it with its signature from
ffmpeg.org and stops unless the signature is FFmpeg's release key's
(`packaging/ffmpeg-devel.asc`, fingerprint `FCF986EA15E6E293A5644F10B4322F04D67658D8`).

CI (`.github/workflows/ci.yml`) runs the tests on Linux and builds the exe on Windows.

## Roadmap

1. **Lower delay**: Low-Latency HLS (partial segments) to get close to 1 second.
2. **Audio-only casting to HomePods and AirPlay speakers** (RAOP), which pyatv supports.
3. **Multi-room**: send the same stream to several Apple TVs.
4. **Screen mirroring for Apple TVs, and with sound**: *Mirror screen* sends TVs of other
   brands the screen with about 100 ms of delay (experimental), but no sound yet; Apple TVs and
   Macs also need the FairPlay setup step. See the research report in the project files
   (`research/airplay-mirroring-windows.html`).
5. **Lower CPU use with Quick Sync and AMF**: keep ddagrab frames on the GPU for them too,
   as Mirror screen does for NVENC.
