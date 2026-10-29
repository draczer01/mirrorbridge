"""Capture Windows system audio (WASAPI loopback) and feed it to ffmpeg over TCP.

FFmpeg on Windows cannot record "what you hear" by itself, so a small thread records
the default speaker's loopback with the `soundcard` package and writes raw float32
PCM to the socket ffmpeg listens on.
"""

from __future__ import annotations

import logging
import socket
import sys
import threading
import time

_LOGGER = logging.getLogger(__name__)

SAMPLE_RATE = 48000
CHANNELS = 2
BLOCK_FRAMES = 480  # 10 ms


def loopback_available() -> tuple[bool, str]:
    """Return (available, description of the speaker being captured)."""
    try:
        import soundcard  # type: ignore

        speaker = soundcard.default_speaker()
        return True, speaker.name
    except Exception as ex:  # pylint: disable=broad-except
        return False, f"system audio capture unavailable: {ex}"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _FFmpegGone(Exception):
    """The socket to FFmpeg closed: the encoder stopped, so audio stops too."""


class LoopbackFeeder(threading.Thread):
    """Thread that records system audio and streams it to 127.0.0.1:port.

    Follows the default output device: if it changes or is unplugged mid-cast, the
    recorder is reopened on the new one and silence fills the gap, so FFmpeg's audio
    track never stalls.
    """

    def __init__(self, port: int, source=None) -> None:
        super().__init__(name="mirrorbridge-audio", daemon=True)
        self.port = port
        self._source = source  # injectable for tests: callable(frames) -> bytes
        self._stop_event = threading.Event()  # not "_stop": threading.Thread uses that name
        self.error: Exception | None = None

    def stop(self) -> None:
        self._stop_event.set()

    def _connect(self, timeout: float = 15.0) -> socket.socket:
        deadline = time.monotonic() + timeout
        while True:
            try:
                sock = socket.create_connection(("127.0.0.1", self.port), timeout=2)
                sock.settimeout(None)  # the 2 s limit is for connecting; FFmpeg may pause longer
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                return sock
            except OSError:
                if self._stop_event.is_set() or time.monotonic() > deadline:
                    raise
                time.sleep(0.1)

    @staticmethod
    def _send(sock: socket.socket, data: bytes) -> None:
        try:
            sock.sendall(data)
        except OSError as ex:
            raise _FFmpegGone(ex) from ex

    def run(self) -> None:
        try:
            sock = self._connect()
        except OSError as ex:
            self.error = ex
            _LOGGER.error("Could not connect audio to ffmpeg: %s", ex)
            return
        try:
            if self._source is not None:
                while not self._stop_event.is_set():
                    self._send(sock, self._source(BLOCK_FRAMES))
                return
            self._run_soundcard(sock)
        except _FFmpegGone as ex:
            if not self._stop_event.is_set():
                self.error = ex
                _LOGGER.warning("Audio stream to ffmpeg ended: %s", ex)
        except Exception as ex:  # pylint: disable=broad-except
            self.error = ex
            _LOGGER.exception("System audio capture failed")
        finally:
            sock.close()

    def _send_silence(self, sock: socket.socket, seconds: float) -> None:
        block = bytes(BLOCK_FRAMES * CHANNELS * 4)
        end = time.monotonic() + seconds
        while not self._stop_event.is_set() and time.monotonic() < end:
            self._send(sock, block)
            time.sleep(BLOCK_FRAMES / SAMPLE_RATE)

    def _run_soundcard(self, sock: socket.socket) -> None:
        import soundcard  # type: ignore

        if sys.platform == "win32":  # WASAPI needs COM initialised on this thread
            import ctypes

            ctypes.windll.ole32.CoInitializeEx(None, 0)

        while not self._stop_event.is_set():
            try:
                speaker = soundcard.default_speaker()
                # Match by endpoint id: names are not unique, and a name substring can
                # also match a real microphone.
                mic = soundcard.get_microphone(speaker.id, include_loopback=True)
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.warning("No audio output to capture (%s); sending silence", ex)
                self._send_silence(sock, 1.0)
                continue
            _LOGGER.info("Capturing system audio from: %s", speaker.name)
            try:
                self._record(sock, mic, speaker.id, soundcard)
            except _FFmpegGone:
                raise
            except Exception as ex:  # pylint: disable=broad-except
                if self._stop_event.is_set():
                    return
                _LOGGER.warning("Audio output changed or failed (%s); reopening", ex)
                self._send_silence(sock, 0.5)

    def _record(self, sock: socket.socket, mic, speaker_id: str, soundcard) -> None:
        """Stream from one device until stop, or until the default output changes."""
        import numpy as np  # type: ignore

        silence = np.zeros((BLOCK_FRAMES, CHANNELS), dtype=np.float32)
        next_check = time.monotonic() + 2
        with mic.recorder(samplerate=SAMPLE_RATE, channels=CHANNELS, blocksize=BLOCK_FRAMES) as rec:
            while not self._stop_event.is_set():
                data = rec.record(numframes=BLOCK_FRAMES)
                if data is None or len(data) == 0:
                    data = silence
                self._send(sock, np.ascontiguousarray(data, dtype=np.float32).tobytes())
                if time.monotonic() >= next_check:
                    next_check = time.monotonic() + 2
                    if soundcard.default_speaker().id != speaker_id:
                        _LOGGER.info("Default audio output changed, switching to it")
                        return
