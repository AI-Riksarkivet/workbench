"""A recording's waveform: the min and max of its sound per short stretch of time, decoded once from its blob.

The waveform a labeling canvas draws an hour of sound with cannot be the sound itself: an hour of 16 kHz mono is 115 MB of
samples, which the browser would have to fetch and decode whole (Web Audio's ``decodeAudioData`` takes the whole file and
answers the whole decoded buffer). So the viewer decodes it here, once, and answers only its peaks: for each bucket of
``1 / rate`` seconds the smallest and largest sample, as signed bytes. The browser builds its coarser zoom levels from
these by taking min and max over neighbouring buckets (`#lib/media/time/waveform/peaks.ts`).

WHAT IS LIBRARY AND WHAT IS NOT. Demuxing and decoding any container and codec is FFmpeg's, through PyAV (its wheels carry
the libraries): the blob is handed to it as a file object, read through Lance's blob API in ranges, and decoded packet by
packet, so a recording is never held whole. Downmixing to one channel and resampling to :data:`DECODE_RATE` is FFmpeg's
resampler. The reduction of each bucket to its min and max is numpy over the resampled samples, which is the whole of
what a peaks tool (BBC's audiowaveform) computes; it is a few lines here rather than a second binary in the image.

BOUNDS, each reported in the answer's ``cut`` when it stopped the decode: the bytes read from the blob
(:data:`MAX_READ_BYTES`), the length decoded (:data:`MAX_SECONDS`), and the time spent (:data:`DEADLINE_SECONDS`). The
answer is at most :data:`MAX_BUCKETS` buckets whatever the length or the length its header claims: a recording longer
than :data:`MAX_BUCKETS` / :data:`FINE_RATE` seconds gets fewer buckets per second. The time bound and the caller's
cancellation are checked between the packets of every stream, a video's included.
"""

from __future__ import annotations

import base64
import io
import math
import time
from collections.abc import Buffer, Callable
from typing import Final, Literal

import av
import numpy as np
from av.audio.stream import AudioStream
from av.container import InputContainer
from pydantic import BaseModel


#: The rate the sound is resampled to before its peaks are taken: a peak is the envelope, and 8 kHz keeps every
#: envelope a person can see at the finest zoom while costing a sixth of 48 kHz to decode into.
DECODE_RATE: Final = 8_000
#: Buckets per second for a recording short enough to have them all (`MAX_BUCKETS` / this = one hour).
FINE_RATE: Final = 100
#: The most buckets one answer carries: two signed bytes each, so at most 720 KB before base64.
MAX_BUCKETS: Final = 360_000
#: The most seconds of sound decoded.
MAX_SECONDS: Final = 4 * 3600
#: The most bytes read from the blob: a long video's picture is demuxed past too, and every byte of it is read.
MAX_READ_BYTES: Final = 2 * 1024**3
#: The longest a decode may take.
DEADLINE_SECONDS: Final = 120.0
#: How many packets are decoded between two checks of the deadline and of whether anyone still waits.
_CHECK_EVERY: Final = 64


type Cut = Literal["bytes", "length", "time"]


class Peaks(BaseModel):
    """A recording's waveform peaks.

    ``peaks`` is base64 of signed bytes, two per bucket (the bucket's min, then its max, each the sample times 127),
    ``rate`` buckets per second from time 0. ``decoded`` is how many seconds the buckets cover, which falls short of
    ``duration`` only when ``cut`` says why. ``audio`` is false for a container with no sound: no buckets, and the
    duration still answered so a time axis can be drawn.
    """

    duration: float
    decoded: float
    rate: float
    audio: bool
    peaks: str
    cut: Cut | None = None


class _BoundedReader(io.RawIOBase):
    """``source`` as FFmpeg reads it, ending at ``limit`` bytes read (counting every read, re-reads after a seek too)."""

    def __init__(self, source: io.RawIOBase | io.BufferedIOBase, limit: int) -> None:
        self._source = source
        self._limit = limit
        self.read_bytes = 0
        self.exhausted = False

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return self._source.seek(offset, whence)

    def tell(self) -> int:
        return self._source.tell()

    def readinto(self, buffer: Buffer, /) -> int:
        room = self._limit - self.read_bytes
        if room <= 0:
            self.exhausted = True
            return 0
        whole = memoryview(buffer).cast("B")
        view = whole[: min(len(whole), room)]
        data = self._source.read(len(view))
        n = len(data)
        view[:n] = data
        self.read_bytes += n
        return n


def _samples_per_bucket(duration: float) -> int:
    """The decoded samples one bucket spans: :data:`FINE_RATE` buckets a second, fewer when that would pass
    :data:`MAX_BUCKETS` over the recording's ``duration`` (unknown: 0)."""
    fine = DECODE_RATE // FINE_RATE
    if duration <= 0:
        return fine
    return max(fine, math.ceil(min(duration, MAX_SECONDS) * DECODE_RATE / MAX_BUCKETS))


def _fold(low: np.ndarray, high: np.ndarray) -> tuple[np.ndarray, np.ndarray, int]:
    """The buckets folded by the least whole factor that brings them within :data:`MAX_BUCKETS`, and that factor.

    The bucket size is chosen from the length the container declares, which any header can misstate; what was decoded
    is bounded by :data:`MAX_SECONDS`, so the buckets before folding are bounded too, and folding keeps the answer's
    bound whatever the header said. A last group short of the factor is a bucket of its own, as a last partial bucket
    is."""
    if low.size <= MAX_BUCKETS:
        return low, high, 1
    factor = math.ceil(low.size / MAX_BUCKETS)
    pad = -low.size % factor
    folded_low = np.concatenate((low, np.full(pad, np.inf, dtype=low.dtype))).reshape(-1, factor).min(axis=1)
    folded_high = np.concatenate((high, np.full(pad, -np.inf, dtype=high.dtype))).reshape(-1, factor).max(axis=1)
    return folded_low, folded_high, factor


def _duration_of(container: InputContainer, stream: AudioStream | None) -> float:
    if stream is not None and stream.duration is not None and stream.time_base is not None:
        return float(stream.duration * stream.time_base)
    return container.duration / 1_000_000 if container.duration is not None else 0.0


def peaks_of(source: io.RawIOBase | io.BufferedIOBase, *, check: Callable[[], None] = lambda: None) -> Peaks:
    """The waveform peaks of the media ``source`` holds.

    ``check`` is called between packets and raises to stop the decode (nobody waits for it any more); the bounds above
    stop it too, and say so in ``cut``.

    Raises:
        av.error.FFmpegError: ``source`` is no container FFmpeg can open.
    """
    reader = _BoundedReader(source, MAX_READ_BYTES)
    started = time.monotonic()
    with av.open(io.BufferedReader(reader, buffer_size=1 << 20), mode="r") as container:
        stream = container.streams.audio[0] if container.streams.audio else None
        duration = _duration_of(container, stream)
        if stream is None:
            return Peaks(duration=duration, decoded=0.0, rate=float(FINE_RATE), audio=False, peaks="")
        spb = _samples_per_bucket(duration)
        max_samples = MAX_SECONDS * DECODE_RATE
        resampler = av.AudioResampler(format="flt", layout="mono", rate=DECODE_RATE)
        lows: list[np.ndarray] = []
        highs: list[np.ndarray] = []
        carry = np.zeros(0, dtype=np.float32)
        taken = 0
        cut: Cut | None = None

        def take(samples: np.ndarray) -> None:
            nonlocal carry, taken
            joined = np.concatenate((carry, samples)) if carry.size else samples
            whole = joined.size // spb
            if whole:
                buckets = joined[: whole * spb].reshape(whole, spb)
                lows.append(buckets.min(axis=1))
                highs.append(buckets.max(axis=1))
            carry = joined[whole * spb :]
            taken += samples.size

        stream.codec_context.thread_type = "AUTO"
        # Every stream's packets come back, so the checks run between a video's packets too: demuxing the sound alone
        # reads the picture between two of its packets without yielding, and a file whose sound is sparse would be read
        # to `MAX_READ_BYTES` before the deadline was first asked (measured: 202 MB of a 211 MB film whose one audio
        # packet comes last). A packet of another stream is read, never decoded.
        for n, packet in enumerate(container.demux()):
            if n % _CHECK_EVERY == 0:
                check()
                if time.monotonic() - started > DEADLINE_SECONDS:
                    cut = "time"
                    break
            if packet.stream is not stream:
                continue
            for frame in packet.decode():
                if not isinstance(frame, av.AudioFrame):
                    continue
                for out in resampler.resample(frame):
                    take(out.to_ndarray()[0].astype(np.float32, copy=False))
            if taken >= max_samples:
                cut = "length"
                break
        else:
            for out in resampler.resample(None):
                take(out.to_ndarray()[0].astype(np.float32, copy=False))
            if reader.exhausted:
                cut = "bytes"
        if carry.size:
            lows.append(carry.min(keepdims=True))
            highs.append(carry.max(keepdims=True))
    low = np.concatenate(lows) if lows else np.zeros(0, dtype=np.float32)
    high = np.concatenate(highs) if highs else np.zeros(0, dtype=np.float32)
    low, high, fold = _fold(low, high)
    pairs = np.empty(low.size * 2, dtype=np.int8)
    pairs[0::2] = np.clip(np.round(low * 127), -127, 127)
    pairs[1::2] = np.clip(np.round(high * 127), -127, 127)
    decoded = min(taken, max_samples) / DECODE_RATE
    return Peaks(
        # The container's own length where it is longer (a film's picture can outlast its sound), else what was decoded:
        # a header can claim any length, and the sound that was there is the length the waveform covers.
        duration=max(duration, decoded),
        decoded=decoded,
        rate=DECODE_RATE / (spb * fold),
        audio=True,
        peaks=base64.b64encode(pairs.tobytes()).decode("ascii"),
        cut=cut,
    )


__all__ = ["DEADLINE_SECONDS", "DECODE_RATE", "FINE_RATE", "MAX_BUCKETS", "MAX_READ_BYTES", "MAX_SECONDS", "Peaks", "peaks_of"]
