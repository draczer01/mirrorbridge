# Changelog

All notable changes to MirrorBridge, which was called AirCast up to version 0.2.1. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- Failed casts, pairings and FFmpeg problems are explained in the app's language (English, or
  Spanish when Windows is in Spanish), naming the menu item to try. The command line and the log
  stay in English.

### Added

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
