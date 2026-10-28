"""Where a mirrored frame's time goes, written to the log every few seconds.

For each frame sent, the stages between FFmpeg and the network:

    pipe    from when the frame was taken (its pts on the session clock, clock.PtsMapper) to when
            its last byte came out of FFmpeg's pipe, above the fastest frame of the last seconds:
            the jitter of capture and encoding
    behind  the same, above the fastest frame of the whole session: what FFmpeg holds back for
            good (a backlog of captured frames, an encoder that has slowed down), which "pipe"
            takes in as its new normal after a couple of seconds
    queue   from the pipe to the sender taking the frame (thread hand-over and any backlog)
    pack    NAL units, header and ChaCha20-Poly1305
    send    the socket write, until the connection took it all
    age     from when the frame was taken to when it was sent, over the same fixed delay

each as median (p50) and 95th percentile (p95), in ms. The fixed delay of capture and encoding
themselves is not in any of them: it cannot be seen from here. Also: the frame rate FFmpeg
delivers (whether sent or dropped) against the one asked for, with a warning when it falls short, the
bitrate, the most bytes the video connection held unsent, keyframes, dropped frames and how
often the TV asked for the time. The TV shows a frame its lead (latencyMs) after it was taken,
so an age that comes near the lead means frames arrive too late to be shown smoothly.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Optional

from .clock import NS, now_ns

_LOGGER = logging.getLogger(__name__)

STAGES = ("pipe", "behind", "queue", "pack", "send", "age")
LOG_SECONDS = 5.0
KEEP = 20000  # frames kept for the session's summary (about 5 minutes at 60 fps)
BIG_KEYFRAME_FRAMES = 8  # a keyframe this many average frames big arrives as one burst
SLOW_FPS = 0.9  # FFmpeg delivering less than this share of the frame rate asked for: a warning


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


class StageTimes:
    """Collects each frame's stage durations (ns) and logs p50/p95 every LOG_SECONDS."""

    def __init__(self, *, kbps: int = 0, fps: int = 0, lead_ms: int = 0, interval: float = LOG_SECONDS) -> None:
        self.interval_ns = int(interval * NS)
        self.lead_ms = lead_ms
        self.fps = fps
        self.frame_budget = kbps * 1000 // 8 // fps if kbps and fps else 0  # bytes of an average frame
        self.frames = 0
        self.bytes = 0
        self.keyframes = 0
        self.big_keyframes = 0
        self.largest_keyframe = 0
        self.dropped = 0
        self.slow_windows = 0  # windows in which FFmpeg delivered too few frames
        self._window: dict[str, list[float]] = {stage: [] for stage in STAGES}
        self._all: dict[str, deque] = {stage: deque(maxlen=KEEP) for stage in STAGES}
        self._window_frames = 0
        self._window_dropped = 0
        self._window_arrived = 0
        self._window_bytes = 0
        self._window_buffered = 0
        self._window_start = now_ns()
        self._last: Optional[int] = None  # when the last frame came, was sent or was dropped
        self._ntp_requests: Optional[int] = None
        self._lag: list[float] = []  # ms, this window

    def _frame_seen(self) -> None:
        now = now_ns()
        if self._last is None:
            self._window_start = now  # the first window counts from the first frame, not FFmpeg's start
        self._last = now

    def arrived(self) -> None:
        """A frame came out of FFmpeg's pipe (sent or dropped later, or never when the session
        ends first): the frame rate FFmpeg delivers, whatever the TV takes."""
        self._frame_seen()
        self._window_arrived += 1

    def add(self, size: int, keyframe: bool, buffered: int = 0, **stages: int) -> None:
        """One frame sent: its size in bytes, what the connection still held after it, and
        its stages, in ns (names from STAGES)."""
        self._frame_seen()
        self.frames += 1
        self.bytes += size
        self._window_frames += 1
        self._window_bytes += size
        self._window_buffered = max(self._window_buffered, buffered)
        if keyframe:
            self.keyframes += 1
            self.largest_keyframe = max(self.largest_keyframe, size)
            if self.frame_budget and size > BIG_KEYFRAME_FRAMES * self.frame_budget:
                self.big_keyframes += 1
        for stage, value in stages.items():
            ms = value / 1e6
            self._window[stage].append(ms)
            self._all[stage].append(ms)

    def loop_lag(self, ns: int) -> None:
        self._lag.append(ns / 1e6)

    def drop(self, count: int = 1) -> None:
        if count > 0:
            self._frame_seen()
            self.dropped += count
            self._window_dropped += count

    def due(self, now: Optional[int] = None) -> bool:
        return (now or now_ns()) - self._window_start >= self.interval_ns

    def log_window(self, now: Optional[int] = None, ntp_requests: Optional[int] = None) -> None:
        """Log the frames since the last time, and start a new window. `ntp_requests` is how
        many times the TV has asked for the time so far."""
        now = now or now_ns()
        seconds = max(1e-9, (now - self._window_start) / NS)
        parts = [f"{stage} {percentile(values, 0.5):.1f}/{percentile(values, 0.95):.1f}"
                 for stage, values in self._window.items() if values]
        delivered = (self._window_arrived or self._window_frames + self._window_dropped) / seconds
        timing = ""
        if ntp_requests is not None:
            if self._ntp_requests is not None:
                timing = f"; the TV asked for the time {ntp_requests - self._ntp_requests} times"
            self._ntp_requests = ntp_requests
        lag = ""
        if self._lag:  # experiment: how late the session's loop runs a 10 ms sleep
            lag = (f"; loop lag ms p50/p95/max {percentile(self._lag, 0.5):.1f}/{percentile(self._lag, 0.95):.1f}/"
                   f"{max(self._lag):.1f}")
            self._lag = []
        _LOGGER.info(
            "Mirroring: %.1f fps (of %d), %.1f Mbit/s; ms p50/p95: %s (after capture and encoding); lead %d ms; "
            "up to %d KB waiting to go; keyframes %d (largest %d KB%s); dropped %d%s%s",
            delivered, self.fps, self._window_bytes * 8 / seconds / 1e6, ", ".join(parts) or "no frames",
            self.lead_ms, self._window_buffered // 1024, self.keyframes, self.largest_keyframe // 1024,
            f", {self.big_keyframes} over {BIG_KEYFRAME_FRAMES} frames' share of the bitrate" if self.big_keyframes
            else "", self.dropped, timing, lag,
        )
        if self.fps and seconds >= 1 and delivered < SLOW_FPS * self.fps:
            self.slow_windows += 1
            behind = self._window["behind"]
            _LOGGER.warning(
                "FFmpeg makes %.1f frames a second of the %d asked for: the screen capture or the encoder cannot keep "
                "up (frames come %.0f ms later than at best). A lower frame rate or resolution, or a hardware encoder, "
                "would help.", delivered, self.fps, percentile(behind, 0.5) if behind else 0.0,
            )
        self._window = {stage: [] for stage in STAGES}
        self._window_frames = 0
        self._window_dropped = 0
        self._window_arrived = 0
        self._window_bytes = 0
        self._window_buffered = 0
        self._window_start = now

    def log_last(self, ntp_requests: Optional[int] = None) -> None:
        """Log the frames since the last window, which ends with the last frame: the time the
        session took to stop after it is not FFmpeg's."""
        if self._last is not None and (self._window_frames or self._window_dropped or self._window_arrived):
            self.log_window(max(self._last, self._window_start + 1), ntp_requests)

    def summary(self) -> dict:
        """p50/p95 (ms) of each stage over the session, and the counts."""
        out: dict = {stage: (round(percentile(list(values), 0.5), 2), round(percentile(list(values), 0.95), 2))
                     for stage, values in self._all.items() if values}
        out.update(frames=self.frames, bytes=self.bytes, keyframes=self.keyframes, dropped=self.dropped,
                   largest_keyframe=self.largest_keyframe, slow_windows=self.slow_windows)
        return out
