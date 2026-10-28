"""Where a mirrored frame's time goes, written to the log every few seconds.

For each frame sent, the stages between FFmpeg and the network:

    pipe    from when the frame was taken (its pts on the session clock, clock.PtsMapper) to when
            its last byte came out of FFmpeg's pipe, above the fastest frame of the last seconds:
            the jitter of capture and encoding (their fixed delay is not visible from here)
    queue   from the pipe to the sender taking the frame (thread hand-over and any backlog)
    pack    NAL units, header and ChaCha20-Poly1305
    send    the socket write, until the connection took it all
    age     from when the frame was taken to when it was sent, over the same fixed delay

each as median (p50) and 95th percentile (p95), in ms, with the frame rate, bitrate, keyframes
and dropped frames. The TV shows a frame its lead (latencyMs) after it was taken, so an age
that comes near the lead means frames arrive too late to be shown smoothly.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Optional

from .clock import NS, now_ns

_LOGGER = logging.getLogger(__name__)

STAGES = ("pipe", "queue", "pack", "send", "age")
LOG_SECONDS = 5.0
KEEP = 20000  # frames kept for the session's summary (about 5 minutes at 60 fps)
BIG_KEYFRAME_FRAMES = 8  # a keyframe this many average frames big arrives as one burst


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
        self.frame_budget = kbps * 1000 // 8 // fps if kbps and fps else 0  # bytes of an average frame
        self.frames = 0
        self.bytes = 0
        self.keyframes = 0
        self.big_keyframes = 0
        self.largest_keyframe = 0
        self.dropped = 0
        self._window: dict[str, list[float]] = {stage: [] for stage in STAGES}
        self._all: dict[str, deque] = {stage: deque(maxlen=KEEP) for stage in STAGES}
        self._window_frames = 0
        self._window_bytes = 0
        self._window_start = now_ns()

    def add(self, size: int, keyframe: bool, **stages: int) -> None:
        """One frame sent: its size in bytes and its stages, in ns (names from STAGES)."""
        self.frames += 1
        self.bytes += size
        self._window_frames += 1
        self._window_bytes += size
        if keyframe:
            self.keyframes += 1
            self.largest_keyframe = max(self.largest_keyframe, size)
            if self.frame_budget and size > BIG_KEYFRAME_FRAMES * self.frame_budget:
                self.big_keyframes += 1
        for stage, value in stages.items():
            ms = value / 1e6
            self._window[stage].append(ms)
            self._all[stage].append(ms)

    def drop(self) -> None:
        self.dropped += 1

    def due(self, now: Optional[int] = None) -> bool:
        return (now or now_ns()) - self._window_start >= self.interval_ns

    def log_window(self, now: Optional[int] = None) -> None:
        """Log the frames since the last time, and start a new window."""
        now = now or now_ns()
        seconds = max(1e-9, (now - self._window_start) / NS)
        parts = [f"{stage} {percentile(values, 0.5):.1f}/{percentile(values, 0.95):.1f}"
                 for stage, values in self._window.items() if values]
        _LOGGER.info(
            "Mirroring: %.1f fps, %.1f Mbit/s; ms p50/p95: %s; lead %d ms; keyframes %d (largest %d KB%s); "
            "dropped %d",
            self._window_frames / seconds, self._window_bytes * 8 / seconds / 1e6, ", ".join(parts) or "no frames",
            self.lead_ms, self.keyframes, self.largest_keyframe // 1024,
            f", {self.big_keyframes} over {BIG_KEYFRAME_FRAMES} frames' share of the bitrate" if self.big_keyframes
            else "", self.dropped,
        )
        self._window = {stage: [] for stage in STAGES}
        self._window_frames = 0
        self._window_bytes = 0
        self._window_start = now

    def summary(self) -> dict:
        """p50/p95 (ms) of each stage over the session, and the counts."""
        out: dict = {stage: (round(percentile(list(values), 0.5), 2), round(percentile(list(values), 0.95), 2))
                     for stage, values in self._all.items() if values}
        out.update(frames=self.frames, bytes=self.bytes, keyframes=self.keyframes, dropped=self.dropped,
                   largest_keyframe=self.largest_keyframe)
        return out
