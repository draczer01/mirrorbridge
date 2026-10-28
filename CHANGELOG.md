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
  supported. No sound yet. When the network or the TV cannot keep up, frames are dropped up to
  the next keyframe instead of piling up; a TV that takes no video for 5 seconds ends the
  session with an error, and ending mirroring on the TV stops it. If FFmpeg cannot capture or
  encode at the start (the driver refuses ddagrab, the GPU encoder is busy), it goes on with GDI
  capture, then with x264, as a cast does.
- *Mirror screen (low latency, experimental)* in the tray menu, next to *Start casting*, and in
  the window: mirrors what *What to cast* names (a display or a window) with the *Quality*
  settings (and what *Test this TV* or `mirror --probe` found for the TV), showing Connecting,
  Casting and Stopping as a cast does. *Stop mirroring* and *Quit* end it, and a failure is
  explained in the app's language like a failed cast. It is offered only for a paired TV that
  offers screen mirroring and does not need FairPlay; otherwise it is greyed out, and its
  tooltip says why (Apple TVs and Macs: coming in a later version). Only one cast, mirroring or
  test runs at a time.
- `mirror --probe` finds out how a TV takes screen mirroring (the SETUP order, the audio stream,
  PTP or NTP timing, the avcC ending), reports what it answered to each step, and saves what
  worked in `mirroring.json` for the next session. Before a probe, a TV that drops the video at
  once gets one more try with the other avcC ending.
- The log says every 5 seconds where mirrored frames' time goes (FFmpeg's pipe, packing and
  encryption, the network), as median and 95th percentile, with the frame rate FFmpeg delivers
  (and a warning when it falls short), what waits to be sent, and how often the TV asked for
  the time.

### Changed

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
