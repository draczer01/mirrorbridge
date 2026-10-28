# Changelog

All notable changes to MirrorBridge, which was called AirCast up to version 0.2.1. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Screen mirroring (experimental, not yet tried on a real TV): `mirror` sends the screen, or a
  test pattern, to an AirPlay 2 TV of another brand a fraction of a second behind instead of
  a cast's seconds. Each frame goes as soon as FFmpeg has encoded it, encrypted, over a
  connection of its own, timed by MirrorBridge's own clock that the TV asks over UDP (allow
  inbound UDP 7880-7889 in Windows Firewall). Apple TVs and Macs need FairPlay and are not
  supported. No sound yet. When the network or the TV cannot keep up, frames are dropped up to
  the next keyframe instead of piling up; a TV that takes no video for 5 seconds ends the
  session with an error, and ending mirroring on the TV stops it.
- `mirror --probe` finds out how a TV takes screen mirroring (the SETUP order, the audio stream,
  PTP or NTP timing, the avcC ending), reports what it answered to each step, and saves what
  worked in `mirroring.json` for the next session. Before a probe, a TV that drops the video at
  once gets one more try with the other avcC ending.
- The log says every 5 seconds where mirrored frames' time goes (FFmpeg's pipe, packing and
  encryption, the network), as median and 95th percentile, with the frame rate FFmpeg delivers
  (and a warning when it falls short), what waits to be sent, and how often the TV asked for
  the time.

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
