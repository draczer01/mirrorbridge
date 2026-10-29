# Changelog

All notable changes to MirrorBridge, which was called AirCast up to version 0.2.1. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- TVs that have no `POST /play`, the first AirPlay video request (TCL, LG and others built on
  Apple's AirPlay SDK for TVs, which answer it with 404), get the stream through the play queue
  (AirPlay video v2), as an iPhone sends it: chosen from the feature bits the TV advertises
  (no bit 0, with bit 33 or 49), and tried once, on a new connection, with any receiver that
  answers `/play` with 404. The way that worked is remembered for each TV until MirrorBridge
  quits (what *Test this TV* tries is not). Apple TVs, and every receiver that advertises
  `/play`, get exactly the requests they got before. The log says which way was used and why,
  and what the TV said while it played.
- *More options* > *Test this TV…* and `MirrorBridge.exe probe "TV name"`: tries `POST /play`
  and the play queue with Apple's public test video, then the play queue with a short cast of
  MirrorBridge's test pattern, then about 10 seconds of screen mirroring with a moving test
  pattern, says what the TV did with each (whether it fetched the stream from the PC, with which
  player; whether it accepted screen mirroring and kept the video open, and which SETUP order,
  avcC ending and timing worked, which is saved for *Mirror screen*), and writes
  `probe-<date>-<time>.log` in the log folder, to send with a bug report. Each test has a time
  limit and is torn down before the next; the whole run takes about three minutes. The
  mirroring test shows the pattern as soon as the TV takes a SETUP, says when to look at the TV
  (a notification, the tray, the command line), and uses the rest of its 75 seconds on what
  else the TV takes (without the audio stream, PTP timing, the other SETUP order). It is
  skipped, with a line that says why, for TVs that need FairPlay, TVs that are not paired and
  receivers that do not offer screen mirroring.
- Screen mirroring (experimental, not yet tried on a real TV): `mirror` sends the screen, or a
  test pattern, to an AirPlay 2 TV of another brand a fraction of a second behind instead of
  a cast's seconds. Each frame goes as soon as FFmpeg has encoded it, encrypted, over a
  connection of its own, timed by MirrorBridge's own clock that the TV asks over UDP (allow
  inbound UDP 7880-7889 in Windows Firewall). Apple TVs and Macs need FairPlay and are not
  supported. No sound yet. When the network or the TV cannot keep up for more than about a
  second, frames are dropped up to a keyframe that is on time instead of piling up; a TV that
  takes no video for 5 seconds ends the session with an error, and ending mirroring on the TV
  stops it. If FFmpeg cannot capture or encode at the start (the driver refuses ddagrab, the
  GPU encoder is busy), it goes on with GDI capture, then with x264, as a cast does.
- *Show my screen on (your TV)*, the tray menu's main item, right under the status line, does
  what the window's big button does: Mirror screen on a TV that takes it (pairing first, with
  the PIN the TV shows, when MirrorBridge is not paired with it yet), the cast on an Apple TV or
  a Mac. Mirror screen sends what *What to cast* names (a display, a window or the virtual
  screen) with *Quality*'s resolution, bitrate and encoder (and what *Test this TV* or
  `mirror --probe` found for the TV), showing Connecting, Casting and Stopping as a cast does.
  *Stop mirroring* and *Quit* end it, and a failure is explained in the app's language like a
  failed cast. The item's tooltip says what it will do, or why it is off (a speaker). Only one
  cast, mirroring or test runs at a time.
- *Mirror screen settings* in the tray menu, for the next Mirror screen: *Latency* Low (the TV
  shows each frame 60 ms after it was captured), Normal (the wait saved for the TV, 100 ms
  unless it needed more; the default) or Smooth (200 ms, for Wi-Fi that falters), and *Frame
  rate* Automatic (60 fps with a hardware encoder and Desktop Duplication, 30 with x264 or GDI
  capture, and never more than the TV takes; the default), 30 or 60 fps. Choosing a Latency
  puts *Debug > TV wait* back on Automatic. *Quality*'s delay is now the casts' only, and its
  frame rate the casts' and Google Cast mirroring's (which does not read *Mirror screen
  settings* yet); both say so, the frame rate without "when transmitting" while a Google Cast TV
  is chosen.
- Mirror screen sends a keyframe (a whole picture) only when it is needed: at the start, and
  after frames were dropped, when FFmpeg is started again for a fresh one at once (at most every
  2 seconds), also on a screen that does not change meanwhile. The keyframe every 2 seconds made
  text blur every 2 seconds: in a one-frame buffer, a 1440p desktop at 8 Mbit/s on NVENC got its
  keyframes at quantizer 39 against 7 for the other frames. NVENC and x264 make no other
  keyframe; Quick Sync, AMF and Media Foundation, not yet tried that way, still make one every
  10 seconds (AMF by its IDR period alone: the keyframes FFmpeg forces on it are not IDRs).
  *Test this TV* keeps one every 2 seconds.
- When Windows takes the screen capture away (a display driver installed or removed, a logoff,
  a change of resolution), Mirror screen starts FFmpeg again on the same display, looked up
  again (the virtual screen too): up to five times in 10 seconds, the first a quarter of a
  second after; only then does it stop, saying that Windows stopped the screen capture. It
  never falls back to GDI capture for that. The log has FFmpeg's last 30 lines whenever it
  stops.
- With NVENC and Desktop Duplication, the picture stays on the graphics card: at the size the TV
  gets it goes from the capture to the encoder untouched. A screen that has to be scaled is
  scaled on the processor, unless the FFmpeg's Windows Graphics Capture (`gfxcapture`) can
  repeat its last frame while the screen stays as it is (`dup_frames`, in FFmpeg after 9.1, not
  in the FFmpeg 8.1.1 inside `MirrorBridge.exe`): then it takes the screen at the TV's size,
  scaled on the graphics card. Without repeats it gives a frame only when the screen changes,
  and a frame that changes all of it, such as a new slide, comes out blurred through
  mirroring's small buffer and stays so until something on the screen moves; Desktop
  Duplication repeats frames, so a still slide is sharp a few frames later. In a test of
  FFmpeg's `gfxcapture` alone on an RTX 4080 SUPER (driver 621.01), it made a 1440p screen into
  1080p at 60 fps for 0.05 of a processor core, where the processor's chain takes 1.3 cores;
  MirrorBridge's own command for it has not yet been tried on hardware. FFmpeg's `scale_d3d11`
  is not used, because there it could not make its picture ("Could not create the texture
  (80070057)"). The display is given to `gfxcapture` by its handle, since its numbering is not
  Desktop Duplication's, and the capture is asked for 10 frames a second more than the frame
  rate (in that test a 200 Hz monitor gave 51 fps for 60), then kept to the frame rate without
  the `fps` filter's repeats, each frame at the time it was captured (to the millisecond); a
  frame left out so is still sent in `gfxcapture`'s first repeat of it, about 21 ms later.
  NVENC is told the capture's top rate, with the bitrate raised to match, so that a frame gets
  the share of bits it gets at the frame rate. A capture that ends before its first picture
  (but for Windows keeping the screen from it), or a picture of another size than asked for,
  sends that display back to the processor, at once and until MirrorBridge quits; a capture
  that Windows closes (the display gone) is started again, as Desktop Duplication's is, and a
  display that comes back at a size to be scaled gets its chain chosen again. The log's line
  for each FFmpeg, and the tray as the session starts, say which chain runs and whether
  `gfxcapture` repeats frames; the log also says what NVENC is told. Quick Sync, AMF and Media
  Foundation scale on the processor, as before.
- With *Extend screen* on, *Mirror screen* (and `mirror --extend`, with `--extend-mode`) sends
  the virtual screen, as a cast does, instead of the PC's main display: the TV works as a second
  screen a fraction of a second behind, for PowerPoint's Presenter View with the notes on the
  laptop and the slides on the TV, for example. The virtual screen is added before the TV is
  asked (*Creating the virtual screen*), at the *Extended screen size* but 1080p at most (4K
  is added at 1080p: shrinking 4K took 30 to 170 ms a frame on a real PC, past the TV's
  lead), fitted into the TV's screen (*Quality > Resolution* does not apply, as for a cast),
  kept for the whole session, also when the session
  is set up again or FFmpeg falls back to GDI capture or x264, and removed when the session ends,
  however it ends. If it goes away first (sleep), the session ends saying so; a missing or broken
  driver gets the same offer to install or fix it as a cast. *Test this TV* and `mirror --probe`
  still mirror only their test pattern. The `mirror` command also takes `--monitor` and
  `--window`, as `cast` does.
- `mirror --probe` finds out how a TV takes screen mirroring (the SETUP order, the audio stream,
  PTP or NTP timing, the avcC ending), reports what it answered to each step, and saves what
  worked in `mirroring.json` for the next session. Before a probe, a TV that drops the video at
  once gets one more try with the other avcC ending.
- The log says every 5 seconds where mirrored frames' time goes (FFmpeg's pipe, packing and
  encryption, the network), as median and 95th percentile, with the frame rate FFmpeg delivers
  (and a warning when it falls short), what waits to be sent, and how often the TV asked for
  the time.
- *More options* > *Debug* > *Latency meter*: a big clock (seconds and milliseconds, with the
  tenths of a second as a row of cells) full screen on every screen, the virtual screen of
  *Extend screen* too, all on one timer. Photograph the PC's screen and the TV together while
  mirroring: the difference between the two clocks is the whole delay from screen to TV, which
  the log cannot see. The last "Mirroring:" line of the log (frame rate, age of the frames,
  lead) shows below the clock. Esc or a click closes every window.
- *More options* > *Debug (for testing)*, next to *Latency meter*: switches for tests of
  *Mirror screen* and `mirror`, which start from the defaults. *TV wait (test)*: 60, 100, 150,
  200 or 300 ms in place of the Latency's (*Automatic*, where choosing a Latency puts it back),
  and never saved for the TV; *Keyframes*: only when needed (the default), every 10 seconds or
  every 2 seconds; *Scaling*: *Automatic* (the default, as above), *Processor (as before)* or
  *Windows capture on the graphics card (test)*, which scales with `gfxcapture` even without
  repeats: then at most 2 frames a second over the frame rate, none left out (50 fps on a
  200 Hz monitor, 48 on a 144 Hz one), and a buffer of 4 frames instead of 1 for a new
  slide's frame (sharper after the mouse moved, not for slides changed one after the other; a
  frame can reach the TV up to 50 ms later on a network no faster than the bitrate), and a
  still screen, which then sends no frames, does not end the session nor warn of a slow frame
  rate (how long a TV keeps a still picture is still to be tried); *Show frames on arrival
  (test)*: codec packets without RespectTimestamps, so that a TV shows each frame as it comes.
  The log says, as each session starts, what it used, and each time FFmpeg starts again, why.
  *Test this TV* and `mirror --probe` leave them out.
- Experimental, for testing: `mirror --timing ptp` (or `MIRRORBRIDGE_TIMING=ptp`) makes
  MirrorBridge the TV's PTP clock (IEEE 1588, UDP 319 and 320) instead of answering its NTP
  requests. The session waits for the TV to follow that clock and goes on with NTP, on a fresh
  connection, when it does not; the log says which, with what the TV sent. Without it, NTP timing
  is exactly as before, and a session with PTP timing is never saved in `mirroring.json`.

### Changed

- The tray menu: *Start casting to (your TV)* gave its place to *Show my screen on (your TV)*,
  and the cast (HLS, 3 to 7 seconds behind, with sound) is now *More options > Transmit (old
  way, 3-7 s delay)*, for a TV that does not show the screen otherwise. *Play a video file*
  stays where it was. *Device* lists every TV once, those found by Google Cast too, with what
  choosing it means (*Ready*, *A speaker*...), and *Include computer sound* says that it is only
  for casts: Mirror screen has no sound yet (while a Google Cast TV is chosen, whose mirroring
  has the sound, it says nothing more, and the main item's tooltip says the sound goes too).
  While the main item's way runs and the TV takes another, *Nothing on the TV? Try another way*,
  under *Stop*, goes to the next one, as the window's button does.
- `MirrorBridge.exe` carries FFmpeg 8.1.1 (gyan.dev's essentials build, checked against the
  size and SHA-256 in `packaging/ffmpeg_pin.json`) in place of the FFmpeg 7.1 of
  `imageio-ffmpeg`, for `gfxcapture` (Windows Graphics Capture, which scales the screen on the
  graphics card). Its NVENC needs an NVIDIA driver 570 or newer: with an
  older one MirrorBridge uses the next encoder, and the log's warning has FFmpeg's line on the
  driver it needs. An FFmpeg named in the settings still goes first, and `doctor` says which
  one is used.
- Mirror screen's video says which colours it carries: the processor's picture is now converted
  and tagged as BT.709 (HD's colours), and the graphics card's carries BT.709 primaries and
  transfer, where NVENC fed the screen directly said BT.601. Google Cast mirroring's VP8, which
  means BT.601, keeps its colours as before.
- *Mirror screen* rides out short network stalls: the picture freezes only while the network
  holds the video up, no longer until the next keyframe. With the PC on Ethernet and a TCL TV
  on Wi-Fi, the network now and then took 100 to 480 ms to take a frame, and each time every
  frame up to the next keyframe was dropped: up to 2 seconds of a frozen picture (1.4 s at the
  start of a session, for a stall of 108 ms). The frames that waited now go late, with the
  times they were taken for, as long as frames have been past the TV's lead for no more than a
  second; only a network that cannot keep up for longer gets frames dropped, up to a keyframe
  that is on time, so that the TV is not left behind. The log's "Mirroring:" lines count the
  frames sent late, and a line says how long the network held up and how many frames went late
  instead of being dropped.
- The stream's server answers the CORS preflight (OPTIONS) of video players that run in a web
  page, and lets them read its errors.
- `cast --file` and *Play a video file* pick `/play` or the play queue as a cast does.
- pyatv 0.18.0, and only that version, is required: MirrorBridge takes over parts of it, and
  screen mirroring builds on its pairing and encryption.

## [0.3.0] - 2026-10-28

A pre-release: not yet tried on a real TV.

### Changed

- AirCast is now MirrorBridge: `MirrorBridge.exe`, the `mirrorbridge` command,
  `%APPDATA%\MirrorBridge` and `mirrorbridge.log`. The first start copies AirCast's
  `settings.json` and `pairings.json`, so the TV stays paired, and moves Start with Windows over
  unless it was turned off in Task Manager. The AirCast folder is never changed.
  `MIRRORBRIDGE_FFMPEG` replaces `AIRCAST_FFMPEG`, which still works.
- AirPlay TVs of other brands, and receivers that do not say what they are, get the stream that
  Apple's AirPlay 2 TV rules ask for: 2-second segments, a 12-second playlist handed over only
  once it is full, PROGRAM-DATE-TIME, and a master playlist with CODECS, FRAME-RATE and
  AVERAGE-BANDWIDTH. Expect about 7 seconds of delay, and a start about 6 seconds later, on those
  TVs. Apple TVs get the same stream as before.
- The app is in English until you choose another language under *Language / Idioma*. It no
  longer follows the Windows display language, so it starts in English on a Spanish Windows too.
- Failed casts, pairings and FFmpeg problems are explained in the app's language, naming the menu
  item to try. The command line and the log stay in English.

### Added

- *Language / Idioma* in the tray menu and at the bottom of the window: English or Español. The
  menu, the window, and every dialog and notification after that switch at once, and the choice is
  kept for the next start. Another language needs only its strings and one entry in
  `gui/i18n.py`; any string it lacks is shown in English.
- Extend screen (experimental): the TV as a second monitor, at 1080p, 720p or 4K (*What to cast* >
  *Extend screen*, `cast --extend`, `vdd status`, `vdd install`). It uses Parsec's free Virtual
  Display Driver, which MirrorBridge never bundles: after you agree, it downloads Parsec's
  installer from parsec.app, checks its SHA-256 and runs it with one UAC prompt.
- `scan -v` and the log show what a receiver advertises, what it answered to each AirPlay request,
  and when, and with which video player, the TV fetched the stream.
- Release package: a version tag builds `MirrorBridge.exe` on GitHub Actions and publishes it as
  a pre-release with a zip (the exe, the guides and the licences), FFmpeg's source and
  `SHA256SUMS`. `THIRD-PARTY-NOTICES.txt` lists everything inside the exe.

### Fixed

- TVs that refuse Apple's optional AirPlay requests on any protocol, as a TCL 55QM7L does with
  "501 Not Implemented", no longer stop the cast, and their error replies no longer cost a wait
  each.
- Every failed AirPlay set-up (pair-verify, SETUP, RECORD, /play) is explained, with the Forget
  pairing advice when the TV has dropped the pairing, and pyatv's timing server is closed after
  each cast.
- Only requests from the TV's own address count as the TV fetching the stream, so a player on the
  PC or a phone no longer hides a TV that the firewall blocks.
- The device list agrees with pyatv about which receivers can play a video sent from a URL.
- A failed connection names the TV.

## [0.2.1] - 2026-10-27

### Fixed

- AirPlay 2 TVs that skip Apple's optional requests (a TCL 55QM7L answers the PUT /setProperty
  calls after /play with 501 Not Implemented) no longer fail the cast: only /play has to succeed.
- Receivers that do not report /playback-info keep the session open. The cast ends when the TV
  stops fetching video, and fails with a firewall hint when the TV never fetches the playlist.

## [0.2.0] - 2026-10-27

The first version, as AirCast.

### Added

- A tray app and a command line for Windows 10 and 11 that find AirPlay receivers, pair with the
  PIN the TV shows, and cast a display or a single window, with system audio, as a live HLS
  stream encoded on the GPU (NVENC, Quick Sync, AMF, Media Foundation, with x264 as fallback).
  The delay is 3 to 7 seconds, in three settings.
- Playing a local video file on the TV.
- A single `AirCast.exe` with FFmpeg included, in English, or in Spanish when Windows is in
  Spanish.

[0.3.0]: https://github.com/draczer01/mirrorbridge/releases/tag/v0.3.0
[0.2.1]: https://github.com/draczer01/mirrorbridge/commit/e8be4ff
[0.2.0]: https://github.com/draczer01/mirrorbridge/commit/300c552
