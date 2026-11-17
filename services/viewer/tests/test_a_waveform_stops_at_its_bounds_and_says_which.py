"""A recording's waveform is decoded within its bounds, and an answer cut short says which bound stopped it.

`viewer.services.peaks.peaks_of` reads a blob as FFmpeg streams it; what it may read (bytes), decode (seconds) and spend
(time) is bounded, and the answer names the bound that cut it so the canvas can say the waveform is partial. Each case
lowers one bound below a three-second WAV and reads what the waveform covers.
"""

from __future__ import annotations

import base64
import io
import math
import struct
import wave

import av
import numpy as np
import pytest

from viewer.services import peaks


def _wav(seconds: float, rate: int = 16_000) -> io.BytesIO:
    frames = b"".join(struct.pack("<h", int(16_000 * math.sin(2 * math.pi * 220 * i / rate))) for i in range(int(seconds * rate)))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(frames)
    buffer.seek(0)
    return buffer


@pytest.mark.parametrize(
    ("bound", "value", "cut"),
    [
        pytest.param("MAX_READ_BYTES", 32_044, "bytes", id="bytes-read"),
        pytest.param("MAX_SECONDS", 1, "length", id="seconds-decoded"),
        pytest.param("DEADLINE_SECONDS", -1.0, "time", id="time-spent"),
    ],
)
def test_a_waveform_cut_by_a_bound_covers_less_and_names_the_bound(monkeypatch: pytest.MonkeyPatch, bound: str, value: float, cut: str) -> None:
    monkeypatch.setattr(peaks, bound, value)

    answer = peaks.peaks_of(_wav(3.0))

    assert (answer.duration, answer.cut, answer.decoded < 3.0) == (3.0, cut, True)


def _matroska_claiming(seconds: int, claimed_ms: float) -> io.BytesIO:
    """`seconds` of 8 kHz sound in Matroska whose header's Duration says `claimed_ms`."""
    out = io.BytesIO()
    with av.open(out, mode="w", format="matroska") as container:
        stream = container.add_stream("pcm_s16le", rate=8000, layout="mono")
        for i in range(seconds):
            frame = av.AudioFrame.from_ndarray(np.full((1, 8000), 1000, dtype=np.int16), format="s16", layout="mono")
            frame.rate, frame.pts = 8000, i * 8000
            for packet in stream.encode(frame):
                container.mux(packet)
    data = bytearray(out.getvalue())
    # Segment Info's Duration element (ID 0x4489), an 8-byte float in TimecodeScale units (1 ms).
    at = data.find(b"\x44\x89\x88")
    data[at + 3 : at + 11] = struct.pack(">d", claimed_ms)
    return io.BytesIO(bytes(data))


def _film_with_its_sound_last(frames: int) -> io.BytesIO:
    """A film whose picture comes first and whose one packet of sound comes last, as a sparse sound track lays out."""
    out = io.BytesIO()
    with av.open(out, mode="w", format="matroska") as container:
        video = container.add_stream("ffv1", rate=25)
        video.width, video.height, video.pix_fmt = 640, 480, "yuv420p"
        sound = container.add_stream("pcm_s16le", rate=8000, layout="mono")
        noise = np.random.default_rng(0)
        for i in range(frames):
            picture = av.VideoFrame.from_ndarray(noise.integers(0, 255, (480, 640, 3), dtype=np.uint8), format="rgb24").reformat(format="yuv420p")
            picture.pts = i
            for packet in video.encode(picture):
                container.mux(packet)
        last = av.AudioFrame.from_ndarray(np.full((1, 800), 1000, dtype=np.int16), format="s16", layout="mono")
        last.rate, last.pts = 8000, frames * 320
        for packet in [*sound.encode(last), *sound.encode(None), *video.encode(None)]:
            container.mux(packet)
    out.seek(0)
    return out


class _Counted(io.BytesIO):
    """A source that counts the bytes read from it."""

    read_bytes = 0

    def read(self, size: int | None = -1, /) -> bytes:
        data = super().read(size)
        self.read_bytes += len(data)
        return data


def test_a_header_misstating_the_length_neither_passes_the_bucket_bound_nor_shortens_the_length(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bucket size is chosen from the length the header declares; one that claims 10 ms over 30 s of sound still
    answers at most `MAX_BUCKETS`, over the length decoded."""
    monkeypatch.setattr(peaks, "MAX_BUCKETS", 100)

    answer = peaks.peaks_of(_matroska_claiming(30, 10.0))

    buckets = len(base64.b64decode(answer.peaks)) // 2
    assert (answer.duration, answer.decoded, answer.cut, buckets <= 100, buckets * (1 / answer.rate) >= 30.0) == (30.0, 30.0, None, True, True)


def test_the_deadline_is_asked_between_a_films_pictures_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deadline already passed stops the decode at its first check, which comes before the picture is read through to
    the sound that follows it: what is read is FFmpeg's probe of the streams (5 MB by default), not the 50 MB film."""
    monkeypatch.setattr(peaks, "DEADLINE_SECONDS", -1.0)
    film = _film_with_its_sound_last(120)
    size = len(film.getvalue())
    source = _Counted(film.getvalue())

    answer = peaks.peaks_of(source)

    assert (answer.cut, source.read_bytes < size / 4) == ("time", True)
