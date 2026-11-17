"""The viewer's frame door as another service names it: a unit's frame, by its key path and its dataset.

The viewer serves a chunk's frame at ``/api/chunk-frame/<unit key>?dataset=<id>`` (one path segment per identity field,
doc key first, as the annotations doors name a unit); the annotator hands that path to an assist model server, which
fetches the frame from the viewer. Both read the shape from here, so the path a model server is given is one the viewer
serves.
"""

from __future__ import annotations

from urllib.parse import quote, urlencode


def chunk_frame_path(unit_key: str, dataset: str) -> str:
    """The viewer's path to the frame of the unit ``unit_key`` (``doc/field/…``) of ``dataset``."""
    path = "/".join(quote(segment, safe="") for segment in unit_key.split("/"))
    return f"/api/chunk-frame/{path}?{urlencode({'dataset': dataset})}"


__all__ = ["chunk_frame_path"]
