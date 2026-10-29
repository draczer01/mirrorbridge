"""Capture Windows system audio (WASAPI loopback) and feed it to ffmpeg over TCP.

FFmpeg on Windows cannot record "what you hear" by itself, so a small thread records
the default speaker's loopback with the `soundcard` package and writes raw float32
PCM to the socket ffmpeg listens on. Screen mirroring's sound takes the blocks itself
instead (a sink), each with the time it came.
"""

from __future__ import annotations

import logging
import socket
import sys
import threading
import time
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

SAMPLE_RATE = 48000
CHANNELS = 2
BLOCK_FRAMES = 480  # 10 ms
SPEAKER_CHECK_SECONDS = 2  # how often the recorder looks for a new default output device


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


class _SinkGone(Exception):
    """The sink raised: what it fed has stopped, so the recording stops too."""


class LoopbackFeeder(threading.Thread):
    """Thread that records system audio and streams it to 127.0.0.1:port.

    Follows the default output device: if it changes or is unplugged mid-cast, the
    recorder is reopened on the new one and silence fills the gap, so FFmpeg's audio
    track never stalls.

    With a sink instead of a port, each block of block_frames goes to
    sink(float32 array of (frames, 2), time.perf_counter_ns() when it came, fresh), fresh
    being True for the first block of each recorder opened (speaker_id and speaker_name
    then name its device). Nothing is sent for a gap: the sink fills it as it needs. If
    the sink raises, the thread ends.
    """

    def __init__(self, port: int | None = None, source=None, *, samplerate: int = SAMPLE_RATE,
                 block_frames: int = BLOCK_FRAMES, buffer_frames: int | None = None,
                 sink: Callable[[Any, int, bool], None] | None = None, name: str = "mirrorbridge-audio") -> None:
        if port is None and sink is None:
            raise ValueError("LoopbackFeeder needs a port or a sink")
        super().__init__(name=name, daemon=True)
        self.port = port
        self._source = source  # injectable for tests: callable(frames) -> bytes
        self.samplerate = samplerate
        self.block_frames = block_frames
        self.buffer_frames = buffer_frames  # WASAPI's buffer (blocksize); None: block_frames
        self._sink = sink
        self._stop_event = threading.Event()  # not "_stop": threading.Thread uses that name
        self.error: Exception | None = None
        self.speaker_id = ""  # set each time a recorder opens
        self.speaker_name = ""

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
        if self._sink is not None:
            self._run_sink()
            return
        try:
            sock = self._connect()
        except OSError as ex:
            self.error = ex
            _LOGGER.error("Could not connect audio to ffmpeg: %s", ex)
            return
        try:
            if self._source is not None:
                while not self._stop_event.is_set():
                    self._send(sock, self._source(self.block_frames))
                return
            self._run_soundcard(lambda arr, fresh: self._send(sock, arr.tobytes()),
                                lambda seconds: self._send_silence(sock, seconds))
        except _FFmpegGone as ex:
            if not self._stop_event.is_set():
                self.error = ex
                _LOGGER.warning("Audio stream to ffmpeg ended: %s", ex)
        except Exception as ex:  # pylint: disable=broad-except
            self.error = ex
            _LOGGER.exception("System audio capture failed")
        finally:
            sock.close()

    def _run_sink(self) -> None:
        try:
            if self._source is not None:
                import numpy as np  # type: ignore

                fresh = True
                while not self._stop_event.is_set():
                    data = np.frombuffer(self._source(self.block_frames), dtype="<f4").reshape(-1, CHANNELS)
                    self._deliver(data, fresh)
                    fresh = False
                return
            self._run_soundcard(self._deliver, self._stop_event.wait)
        except _SinkGone as ex:
            _LOGGER.debug("System audio capture stopped: the sound took no more (%r)", ex.__cause__)
        except Exception as ex:  # pylint: disable=broad-except
            self.error = ex
            _LOGGER.exception("System audio capture failed")

    def _deliver(self, data, fresh: bool) -> None:
        arrived = time.perf_counter_ns()
        try:
            self._sink(data, arrived, fresh)
        except Exception as ex:  # pylint: disable=broad-except
            raise _SinkGone(ex) from ex

    def _send_silence(self, sock: socket.socket, seconds: float) -> None:
        block = bytes(self.block_frames * CHANNELS * 4)
        end = time.monotonic() + seconds
        while not self._stop_event.is_set() and time.monotonic() < end:
            self._send(sock, block)
            time.sleep(self.block_frames / self.samplerate)

    def _run_soundcard(self, emit: Callable[[Any, bool], None], silence: Callable[[float], Any]) -> None:
        """Record until stop, handing each block to emit(array, fresh) and calling silence(seconds)
        for a gap without a device."""
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
                _LOGGER.warning("No audio output to capture (%s); %s", ex,
                                "sending silence" if self._sink is None else "trying again in 1 s")
                silence(1.0)
                continue
            _LOGGER.info("Capturing system audio from: %s", speaker.name)
            self.speaker_id, self.speaker_name = speaker.id, speaker.name
            try:
                self._record(emit, mic, speaker.id, soundcard)
            except (_FFmpegGone, _SinkGone):
                raise
            except Exception as ex:  # pylint: disable=broad-except
                if self._stop_event.is_set():
                    return
                _LOGGER.warning("Audio output changed or failed (%s); reopening", ex)
                silence(0.5)

    def _record(self, emit: Callable[[Any, bool], None], mic, speaker_id: str, soundcard) -> None:
        """Record from one device until stop, or until the default output changes."""
        import numpy as np  # type: ignore

        silence = np.zeros((self.block_frames, CHANNELS), dtype=np.float32)
        next_check = time.monotonic() + SPEAKER_CHECK_SECONDS
        fresh = True
        with mic.recorder(samplerate=self.samplerate, channels=CHANNELS,
                          blocksize=self.buffer_frames or self.block_frames) as rec:
            while not self._stop_event.is_set():
                data = rec.record(numframes=self.block_frames)
                if data is None or len(data) == 0:
                    data = silence
                emit(np.ascontiguousarray(data, dtype=np.float32), fresh)
                fresh = False
                if time.monotonic() >= next_check:
                    next_check = time.monotonic() + SPEAKER_CHECK_SECONDS
                    if soundcard.default_speaker().id != speaker_id:
                        _LOGGER.info("Default audio output changed, switching to it")
                        return
