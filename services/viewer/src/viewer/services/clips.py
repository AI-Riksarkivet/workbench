"""Clip excerpts with MP3 audio — the codec workaround for webview hosts.

ffmpeg cuts the window from the source media, which the route writes to a temporary file first, read through the
caller's own handle on the dataset (the lakehouse decides that read). Nothing is fetched over HTTP, and no token of the
caller's is handed to ffmpeg or forwarded anywhere. Disk cache capped at 50 clips; at most ``MAX_CONCURRENT_BUILDS``
transcodes at once, refusing rather than queueing.

VS Code's webview Chromium ships without the AAC decoder (microsoft/vscode
#167685), so archive H.264+AAC files play silently inside MCP apps. This module
cuts the requested window out of the source and re-encodes audio to
MP3, which every webview supports. Results are small complete MP4s served with
normal Range support, cached on disk and evicted oldest-first.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from viewer.services.build_slots import BuildSlots
from workbench.exceptions import ServiceUnavailableError


logger = logging.getLogger(__name__)

CACHE_DIR = Path(tempfile.gettempdir()) / "media-clip-cache"
MAX_CLIP_S = 660.0  # 2×(max transcript half-window) + slack
_MAX_CACHED = 50
_FFMPEG_TIMEOUT_S = 120

#: How many clips may transcode at once. Still small — the box's CPU belongs to the model servers and
#: a clip is seconds-cheap — but a NUMBER rather than the word "one", so the bound is stated.
MAX_CONCURRENT_BUILDS = 2

#: How coarsely a window is keyed, in milliseconds. The key was `int(lo * 1000)`, so `hi=1.001` and
#: `hi=1.002` were different files: a caller varying the third decimal never hit the cache and every
#: request became a fresh libx264 transcode. That is what turned a queue into a weapon. 250 ms is
#: below the threshold at which a viewer perceives a different excerpt and far above the noise a
#: client's own float arithmetic produces.
_CLIP_QUANTUM_MS = 250

# BOUNDED, refusing rather than queueing (`viewer.services.build_slots`): publish is made race-free by the tmp+rename
# below, not by the slots.
build_slots = BuildSlots("clip", MAX_CONCURRENT_BUILDS)


def clip_cache_path(cache_key: str, lo: float, hi: float) -> Path:
    """Deterministic cache file for one (dataset+doc, window). ``cache_key`` is
    built from the dataset id (a directory stem) and the pattern-whitelisted
    doc key, so the name can't traverse.

    The window is QUANTIZED to ``_CLIP_QUANTUM_MS``. Keyed to the millisecond, this
    was a free miss generator: ``hi=1.001`` and ``hi=1.002`` named different files, so
    a caller varying the third decimal transcoded on every request. Coarse enough that
    client-side float noise collapses onto one clip, fine enough that two windows a
    person could tell apart never do."""
    lo_ms = int(lo * 1000) // _CLIP_QUANTUM_MS
    hi_ms = int(hi * 1000) // _CLIP_QUANTUM_MS
    return CACHE_DIR / f"{cache_key}_{lo_ms}_{hi_ms}.mp4"


def _touch(path: Path) -> Path:
    """Mark a cache hit, so `evict_old_clips`' existing mtime sort becomes a real LRU.

    Reads never touched mtime, so the eviction was FIFO: it dropped the oldest-CREATED regardless of
    how hot the clip was, and a long-lived popular excerpt was eventually evicted on age alone and paid
    another 120s-capped ffmpeg transcode. One `utime` makes mtime mean "last served" and the sort that
    is already there does the rest — no second index to keep in step with the files.

    Best-effort: a clip evicted between the `exists()` above and this call must still be SERVED (the
    caller holds the path and the inode outlives the unlink), so a failure here is not worth an error.
    """
    with suppress(OSError):
        os.utime(path, None)
    return path


def evict_old_clips(cache_dir: Path, keep: int = _MAX_CACHED) -> None:
    """Drop the least-recently-SERVED cached clips beyond ``keep``.

    By mtime, which `_touch` refreshes on every cache hit — so this is an LRU rather than the FIFO it
    was when only writes moved the timestamp.
    """
    clips = sorted(cache_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in clips[keep:]:
        stale.unlink(missing_ok=True)


def build_clip(write_source: Callable[[Path], None], cache_key: str, lo: float, hi: float) -> Path:
    """Return the cached clip for the window, transcoding it on first request.

    ``write_source`` writes the source media to the path it is given; it runs only on a cache miss, inside a build
    slot. Video is re-encoded (not copied) so the output starts frame-accurately at ``lo`` and the viewer's
    ``media_offset_s`` mapping holds. ffmpeg reads a local file alone (``-protocol_whitelist file``).
    """
    out = clip_cache_path(cache_key, lo, hi)
    if out.exists():
        return _touch(out)
    with build_slots.acquire():
        if out.exists():  # built while we waited for a slot
            return _touch(out)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp.mp4")
        source = out.with_suffix(".source")
        write_source(source)
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-ss",
            f"{lo:.3f}",
            "-i",
            str(source),
            "-t",
            f"{hi - lo:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "4",
            "-movflags",
            "+faststart",
            "-y",
            str(tmp),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            tmp.unlink(missing_ok=True)
            raise ServiceUnavailableError("clip transcode timed out") from exc
        finally:
            source.unlink(missing_ok=True)
        if proc.returncode != 0 or not tmp.exists():
            tmp.unlink(missing_ok=True)
            logger.warning("ffmpeg clip build failed: %s", proc.stderr[-500:])
            raise ServiceUnavailableError("clip transcode failed")
        tmp.rename(out)
        evict_old_clips(CACHE_DIR)
    return out
