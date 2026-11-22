"""The picture of an item a model is shown: what assist and a judge's ask carry (`annotator.model_endpoint.protocol`)."""

from __future__ import annotations

import base64
import re
from typing import Final

from annotator.annotations.unit import Unit
from annotator.corpus import Corpus
from annotator.model_endpoint import protocol
from annotator.model_endpoint.client import ModelRefusedAsk
from workbench.exceptions import NotFoundError
from workbench.lancekit.registry import table_dataset
from workbench.media.frames import frame_row


#: A picture's media type as the protocol carries it (`protocol.ImageContent.mime`).
IMAGE_TYPE: Final = re.compile(protocol.IMAGE_TYPE_PATTERN)


def picture_of(corpus: Corpus, unit: Unit) -> protocol.ImageContent:
    """The unit's picture, the one the canvas draws: its frame, or its document's thumbnail when the unit is a whole
    document. Blocking (a scan and a blob read)."""
    handle, declared = corpus.handle, corpus.declared
    doc, *rest = unit.values.values()
    if rest:
        if not all(isinstance(value, int) for value in rest):
            raise ModelRefusedAsk("a model is shown a unit's frame, and this unit's key names none")
        frame = frame_row(handle, str(doc), tuple(int(value) for value in rest), 0)
        ds, column, row, mime = frame.ds, frame.column, frame.row, frame.mime
    else:
        binding = declared.document
        if binding is None or binding.thumbnail is None:
            raise NotFoundError("this item has no picture a model can be shown")
        ds, column = table_dataset(handle, binding.table), binding.thumbnail
        columns = [binding.thumbnail_mime] if binding.thumbnail_mime else []
        found = ds.to_table(columns=columns, filter=unit.where, with_row_id=True, limit=1)
        if found.num_rows == 0:
            raise NotFoundError("this item has no picture a model can be shown")
        row = int(found.column("_rowid")[0].as_py())
        mime = (found.column(binding.thumbnail_mime)[0].as_py() if binding.thumbnail_mime else None) or "image/jpeg"
    # The stored type without its parameters (`image/jpeg; charset=binary`), held to what the protocol carries.
    mime = mime.partition(";")[0].strip().lower()
    if not IMAGE_TYPE.fullmatch(mime):
        raise ModelRefusedAsk(f"a model is shown a picture, and this item's is {mime[:80]!r}")
    blob = ds.take_blobs(column, ids=[row])[0]
    if blob is None:
        raise NotFoundError("this item has no picture a model can be shown")
    with blob as payload:
        size = payload.size()
        if size > protocol.MAX_IMAGE_BYTES:
            raise ModelRefusedAsk(f"this item's picture is {size} bytes, more than the {protocol.MAX_IMAGE_BYTES} a model is sent")
        data = payload.read()
    return protocol.ImageContent(mime=mime, data=base64.b64encode(data).decode("ascii"))


__all__ = ["picture_of"]
