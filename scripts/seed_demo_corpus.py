#!/usr/bin/env python
"""Seed REAL, SEARCHABLE dev datasets, so every media surface has something to show.

The media plane reads its datasets from the sources a deployment declares (`client_kit.sources`). The local stack
declares three (`scripts/local-sources.json`), one of each kind, and this writes all three under one root:

- ``<root>/lakehouse``: a directory catalog laid out as a lakehouse's (``<warehouse>$<namespace>``), standing in for one,
  holding the two stamped datasets below, each one's descriptor on its row table's schema metadata, where the registry
  reads it;
- ``<root>/archive``: a lance-namespace directory catalog holding one UNSTAMPED table two namespaces deep
  (``field$survey`` → ``observations``: text with a full-text index, numbers, a timestamp, a list, a vector with a vector
  index), which the workbench shows from its schema alone;
- ``<root>/loose/scans.lance``: a bare Lance table read by path (text, numbers, a timestamp, a vector and an image blob),
  no index and no descriptor.

The two stamped datasets, in one warehouse:

- ``<warehouse>$corpus``: page images. Three volumes of a few pages each, every page a drawn PNG with distinct prose and
  an FTS index, a thumbnail per volume, and an atlas projection.
- ``<warehouse>$recordings``: audio. Three short WAV recordings synthesized here, each a few seconds of two alternating
  voices, cut into timed segments with an FTS index, word alignments (the transcript window) and speaker turns (the
  speakers timeline).

There is no video dataset: a browser's ``<video>`` plays H.264, VP8/9 or AV1, and no encoder for any of them is in the
lockfile, so a video could only be a binary shipped beside this script.

**Why a corpus and not one row.** A dataset that declared no `search` answered every query with zero hits and looked
broken while being "correct". So: several documents, several rows each, distinct prose per row, and an FTS index, so a
query for a common word returns MANY hits across MANY documents.

This is a DEV FIXTURE, not production data. Every image and sound is made here rather than shipped as a binary, so the
fixture stays reviewable in a diff.

    uv run python scripts/seed_demo_corpus.py <root>

Then run the media services with the declaration copied into ``<root>`` (`scripts/local-stack.sh` does both).
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import struct
import sys
import wave
from collections.abc import Sequence

import lance
import lance_namespace
import lancedb
import lancedb.index
import pyarrow as pa
from lance_namespace import (
    CreateNamespaceRequest,
    DropTableRequest,
    LanceNamespace,
    ListTablesRequest,
    NamespaceAlreadyExistsError,
    TableNotFoundError,
)
from PIL import Image, ImageDraw

from client_kit.lancekit.descriptor import DESCRIPTOR_METADATA_KEY, Declared
from service_kit.naming import CATALOG_DELIMITER


#: The warehouse the fixture is written in when the command line names none.
DEFAULT_WAREHOUSE = "demo"
#: The two datasets' namespaces inside the warehouse.
PAGES = "corpus"
RECORDINGS = "recordings"

#: The page fixture: three volumes, each a handful of pages. The prose is deliberately OVERLAPPING (every page mentions
#: the archive and most a protokoll) so a one-word query returns hits across several documents, and DIFFERENT enough
#: that a narrower query (e.g. "sjöfart") narrows the result set.
DOCUMENTS: list[dict] = [
    {
        "doc_id": "fe00cd746463ad2c",
        "title": "Landskapshandlingar 1663",
        "pages": [
            "Protokoll hållet vid Riksarkivet den tredje maj, rörande jordeböcker och skatt.",
            "Vidare antecknades att kronans uppbörd av spannmål blivit fördröjd detta år.",
            "Domboken upptager tvist om ägogränser mellan två hemman i socknen.",
            "Avskrift av brev från landshövdingen angående vägarnas underhåll.",
        ],
    },
    {
        "doc_id": "a7419c0b2d5e8f31",
        "title": "Sjöfartshandlingar 1701",
        "pages": [
            "Protokoll rörande sjöfart och tullens uppbörd i hamnen under sommaren.",
            "Skeppslistan upptager fjorton fartyg, därav tre från främmande land.",
            "Anteckning om last av salt och järn, samt om besättningens rullor.",
        ],
    },
    {
        "doc_id": "b2c8e5417d90a6f4",
        "title": "Domboksregister 1688",
        "pages": [
            "Domboken för häradet upptager mål om arv, skuld och olovligt skogshygge.",
            "Protokoll från tinget: nämnden avkunnade dom i tvist om fiskevatten.",
            "Register över parter och vittnen, med hänvisning till Riksarkivet.",
        ],
    },
]

#: The audio fixture: three recordings, each a few segments of two speakers taking turns. Each segment is one turn, so
#: the transcript window and the speakers timeline agree on who spoke when.
RECORDINGS_FIXTURE: list[dict] = [
    {
        "doc_id": "c41f0e2a9b7d3e15",
        "title": "Intervju om hamnen 1952",
        "segments": [
            ("A", "Berätta om hamnen och tullen när ni började arbeta där."),
            ("B", "Hamnen var full av fartyg som lastade salt och järn varje sommar."),
            ("A", "Och protokollen från tullen, var förvarades de?"),
        ],
    },
    {
        "doc_id": "d82e6b1c4a0f9e73",
        "title": "Minnen från tinget 1961",
        "segments": [
            ("B", "På tinget avkunnade nämnden dom i tvisten om fiskevattnet."),
            ("A", "Var det många vittnen som hördes i målet?"),
            ("B", "Fyra vittnen, och registret finns kvar i arkivet."),
        ],
    },
    {
        "doc_id": "e6a3d9f20c5b1847",
        "title": "Samtal om arkivet 1974",
        "segments": [
            ("A", "Riksarkivet tog emot handlingarna från landskapet samma år."),
            ("B", "Jordeböckerna och skattelängderna kom i flera omgångar."),
        ],
    },
]

#: Each speaker's voice: a fundamental and its overtones' weights, so the two are told apart by ear.
VOICES: dict[str, tuple[float, tuple[float, ...]]] = {"A": (196.0, (1.0, 0.5, 0.25)), "B": (294.0, (1.0, 0.35, 0.15))}
SAMPLE_RATE = 16_000
SEGMENT_SECONDS = 2.0


def page_image(title: str, page_no: int, width: int = 900, height: int = 1200) -> bytes:
    """Render a page-like image: ruled lines and blocks a bbox can plausibly enclose.

    The title and page number are stamped on so a screenshot of the canvas is self-identifying: otherwise every page
    looks identical and a drive cannot show it opened the page it meant to.
    """
    img = Image.new("RGB", (width, height), (250, 248, 242))
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 40, width - 40, height - 40], outline=(200, 195, 185), width=2)
    draw.text((70, 70), f"{title} — page {page_no} (synthetic fixture)", fill=(60, 55, 50))
    y = 130
    for row in range(24):
        run = 760 if row % 4 != 3 else 420
        draw.rectangle([70, y, 70 + run, y + 18], fill=(70, 66, 62))
        y += 42
    return _png(img)


def thumbnail(title: str) -> bytes:
    """A volume's cover, small enough for a gallery tile."""
    img = Image.open(io.BytesIO(page_image(title, 0)))
    img.thumbnail((180, 240))
    return _png(img)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _atlas_xy(doc_index: int, page_no: int) -> tuple[float, float]:
    """A STABLE 2-D position per chunk, one loose cluster per document.

    Not a real projection and not pretending to be: the fixture has no embedding model. What it must be is
    DETERMINISTIC (re-seeding cannot reshuffle the map under a saved lasso) and SEPARATED (pages of one volume land
    together, so a selection is visibly a selection). A document's angle around the unit circle plus a hashed jitter
    gives both, with no numpy dependency in a seeding script.
    """
    theta = 2.0 * math.pi * doc_index / max(len(DOCUMENTS), 1)
    digest = hashlib.sha256(f"{doc_index}:{page_no}".encode()).digest()
    jitter_x = (digest[0] / 255.0 - 0.5) * 0.45
    jitter_y = (digest[1] / 255.0 - 0.5) * 0.45
    return (math.cos(theta) + jitter_x, math.sin(theta) + jitter_y)


def speech(turns: Sequence[str]) -> bytes:
    """A mono 16-bit WAV of ``turns``, one :data:`SEGMENT_SECONDS` turn per speaker named: each turn a voice's tone,
    pulsed at a syllable's pace and faded at both ends so turns do not click."""
    frames = bytearray()
    per_turn = int(SAMPLE_RATE * SEGMENT_SECONDS)
    for speaker in turns:
        fundamental, overtones = VOICES[speaker]
        norm = sum(overtones)
        for i in range(per_turn):
            t = i / SAMPLE_RATE
            tone = sum(weight * math.sin(2 * math.pi * fundamental * (n + 1) * t) for n, weight in enumerate(overtones)) / norm
            syllables = 0.55 + 0.45 * math.sin(2 * math.pi * 4.0 * t) ** 2
            fade = min(1.0, i / 800, (per_turn - i) / 800)
            frames += struct.pack("<h", int(12_000 * tone * syllables * fade))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(bytes(frames))
    return buf.getvalue()


def _alignments(text: str, start: float, end: float) -> str:
    """One segment's word alignments as the transcript window reads them: the words spread evenly over the segment, each
    after the first carrying its leading space, as a speech recognizer's word tokens do (the window renders them as
    written)."""
    words = text.split()
    step = (end - start) / len(words)
    timed = [{"text": w if i == 0 else f" {w}", "start": round(start + i * step, 3), "end": round(start + (i + 1) * step, 3)} for i, w in enumerate(words)]
    return json.dumps([{"start": start, "end": end, "text": text, "words": timed}], ensure_ascii=False)


def _fresh_table(client: LanceNamespace, table_id: list[str], data: pa.Table) -> lance.LanceDataset:
    """Write ``data`` as a NEW table at ``table_id``, dropping any table already there.

    Dropped rather than overwritten: an overwrite rewrites the data but leaves earlier ``_indices/<uuid>`` behind, and a
    re-index can leave the table pointing at an index whose files were collected; the reader then dies with
    ``tokens.lance not found`` and every query 400s. Re-seeding must be idempotent, so the fixture is rebuilt from
    nothing rather than layered onto its own history. Blob v2 needs file format >= 2.2, named here rather than left to
    the default, and stable row ids are what the media routes take blobs by.
    """
    with contextlib.suppress(TableNotFoundError):
        client.drop_table(DropTableRequest(id=table_id))
    return lance.write_dataset(data, namespace_client=client, table_id=table_id, mode="create", data_storage_version="2.2", enable_stable_row_ids=True)


def _namespace(client: LanceNamespace, namespace: list[str]) -> None:
    """Create each level of ``namespace`` the catalog lacks."""
    for depth in range(1, len(namespace) + 1):
        with contextlib.suppress(NamespaceAlreadyExistsError):
            client.create_namespace(CreateNamespaceRequest(id=namespace[:depth]))


def _index_and_stamp(client: LanceNamespace, connection: lancedb.DBConnection, namespace: list[str], row_table: str, declared: Declared) -> None:
    """Index ``row_table.text`` for FTS and stamp ``declared`` on it.

    The FTS index is built through LANCEDB: the search service opens the row table with `lancedb` and runs
    `tbl.search(MatchQuery(...), query_type="fts")`, which needs an index built by that same stack; an unindexed column
    answers zero hits for every query, indistinguishable from an empty corpus. The registry reads a dataset's declared
    roles off the `lance_media.descriptor` schema-metadata stamp of its first table (by name) that carries one, so the
    descriptor rides the data rather than a file beside it; it is stamped on the latest version, which the index made.
    """
    connection.open_table(row_table, namespace_path=namespace).create_index("text", config=lancedb.index.FTS(), replace=True)
    lance.dataset(namespace_client=client, table_id=[*namespace, row_table]).update_schema_metadata(
        {DESCRIPTOR_METADATA_KEY.decode(): declared.model_dump_json()}
    )


def seed_pages(client: LanceNamespace, connection: lancedb.DBConnection, namespace: list[str]) -> None:
    """The page-image dataset: ``chunks`` (one row per page), ``documents`` (one row per volume) and the descriptor."""
    _namespace(client, namespace)
    rows: list[dict] = []
    for doc_index, document in enumerate(DOCUMENTS):
        for page_no, prose in enumerate(document["pages"]):
            x, y = _atlas_xy(doc_index, page_no)
            rows.append(
                {
                    "doc_id": document["doc_id"],
                    "speech_id": page_no,
                    "chunk_id": page_no,
                    "frame_idx": 0,
                    "image": page_image(document["title"], page_no),
                    "image_mime": "image/png",
                    "caption": f"{document['title']} · page {page_no}",
                    "text": prose,
                    "title": document["title"],
                    "atlas_x": x,
                    "atlas_y": y,
                    "atlas_cluster": doc_index,
                }
            )
    # speech_id/chunk_id are INTEGERS: the viewer builds its frame filter with unquoted numeric literals. The page image
    # is a blob v2 column (`lance.blob_field`, a real Arrow extension type), or the registry refuses the dataset.
    # `image_mime` is the frames' type column: the viewer serves a frame by the first `*_mime` column beside its blob,
    # else as image/jpeg.
    chunks = pa.schema(
        [
            pa.field("doc_id", pa.utf8()),
            pa.field("speech_id", pa.int64()),
            pa.field("chunk_id", pa.int64()),
            # The frames capability selects a frame per chunk by `frame_idx` (0 = representative).
            pa.field("frame_idx", pa.int64()),
            lance.blob_field("image"),
            pa.field("image_mime", pa.utf8()),
            pa.field("caption", pa.utf8()),
            pa.field("text", pa.utf8()),
            pa.field("title", pa.utf8()),
            # The atlas projection: `AtlasSpace` binds x/y/cluster to real columns.
            pa.field("atlas_x", pa.float32()),
            pa.field("atlas_y", pa.float32()),
            pa.field("atlas_cluster", pa.int32()),
        ]
    )
    columns = {name: [row[name] for row in rows] for name in chunks.names}
    columns["image"] = lance.blob_array(columns["image"])
    _fresh_table(client, [*namespace, "chunks"], pa.table(columns, schema=chunks))

    # The doc-level table `document.table` points at: ONE row per document, since `/api/documents` pages it row for row.
    # Its cover and thumbnail are page 0. `document.media_blob`, `mime`, `thumbnail` and `thumbnail_mime` each name a
    # column of THIS table.
    documents = pa.schema(
        [
            pa.field("doc_id", pa.utf8()),
            pa.field("title", pa.utf8()),
            pa.field("n_chunks", pa.int32()),
            lance.blob_field("image"),
            pa.field("mime", pa.utf8()),
            lance.blob_field("thumb"),
            pa.field("thumb_mime", pa.utf8()),
        ]
    )
    doc_columns = {
        "doc_id": [d["doc_id"] for d in DOCUMENTS],
        "title": [d["title"] for d in DOCUMENTS],
        "n_chunks": [len(d["pages"]) for d in DOCUMENTS],
        "image": lance.blob_array([page_image(d["title"], 0) for d in DOCUMENTS]),
        "mime": ["image/png"] * len(DOCUMENTS),
        "thumb": lance.blob_array([thumbnail(d["title"]) for d in DOCUMENTS]),
        "thumb_mime": ["image/png"] * len(DOCUMENTS),
    }
    _fresh_table(client, [*namespace, "documents"], pa.table(doc_columns, schema=documents))

    # Built through the model the registry reads it with, so a malformed descriptor fails here, at seed time.
    declared = Declared.model_validate(
        {
            "identity": {"key_fields": ["doc_id", "speech_id", "chunk_id"], "doc_key": "doc_id"},
            "document": {"table": "documents", "media_blob": "image", "kind": "image", "mime": "mime", "thumbnail": "thumb", "thumbnail_mime": "thumb_mime"},
            # `/api/documents` projects exactly `doc_key + metadata`, so a title declared only under `display.title` comes
            # back null in the browse gallery.
            "display": {
                "title": ["title"],
                "body": "text",
                "caption": "caption",
                "metadata": [{"field": "title", "label": "Title"}, {"field": "n_chunks", "label": "Pages"}],
            },
            "search": {"row_table": "chunks", "fts": {"table": "chunks", "column": "text"}, "filterable": ["doc_id"]},
            # The channel colours points by volume, which makes a lasso selection legible as a selection.
            "atlas": [
                {
                    "name": "text",
                    "table": "chunks",
                    "x": "atlas_x",
                    "y": "atlas_y",
                    "cluster": "atlas_cluster",
                    "source_column": "text",
                    "channels": [{"name": "volume", "column": "title"}],
                }
            ],
            # Declared, not probed: the frames capability is what gives the annotator and the player each page's image.
            "capabilities": {"frames": "chunks.image"},
        }
    )
    _index_and_stamp(client, connection, namespace, "chunks", declared)


def seed_recordings(client: LanceNamespace, connection: lancedb.DBConnection, namespace: list[str]) -> None:
    """The audio dataset: ``segments`` (one timed row per turn), ``recordings`` (one row per recording, its WAV),
    ``turns`` (the speaker turns) and the descriptor."""
    _namespace(client, namespace)
    segments: list[dict] = []
    turns: list[dict] = []
    for recording in RECORDINGS_FIXTURE:
        for index, (speaker, text) in enumerate(recording["segments"]):
            start, end = index * SEGMENT_SECONDS, (index + 1) * SEGMENT_SECONDS
            segments.append(
                {
                    "doc_id": recording["doc_id"],
                    "speech_id": 0,
                    "chunk_id": index,
                    "start": start,
                    "end": end,
                    "speaker": speaker,
                    "text": text,
                    "title": recording["title"],
                    "alignments": _alignments(text, start, end),
                }
            )
            # The turns table's columns are the diarization capability's contract (`viewer.api.v1.endpoints.diarization`).
            turns.append({"doc_id": recording["doc_id"], "turn_id": index, "speaker_label": f"Speaker {speaker}", "start": start, "end": end})
    segment_schema = pa.schema(
        [
            pa.field("doc_id", pa.utf8()),
            pa.field("speech_id", pa.int64()),
            pa.field("chunk_id", pa.int64()),
            pa.field("start", pa.float64()),
            pa.field("end", pa.float64()),
            pa.field("speaker", pa.utf8()),
            pa.field("text", pa.utf8()),
            pa.field("title", pa.utf8()),
            pa.field("alignments", pa.utf8()),
        ]
    )
    _fresh_table(client, [*namespace, "segments"], pa.Table.from_pylist(segments, schema=segment_schema))
    turn_schema = pa.schema(
        [
            pa.field("doc_id", pa.utf8()),
            pa.field("turn_id", pa.int64()),
            pa.field("speaker_label", pa.utf8()),
            pa.field("start", pa.float64()),
            pa.field("end", pa.float64()),
        ]
    )
    _fresh_table(client, [*namespace, "turns"], pa.Table.from_pylist(turns, schema=turn_schema))
    recording_schema = pa.schema(
        [
            pa.field("doc_id", pa.utf8()),
            pa.field("title", pa.utf8()),
            pa.field("duration", pa.float64()),
            lance.blob_field("audio"),
            pa.field("mime", pa.utf8()),
        ]
    )
    recordings = {
        "doc_id": [r["doc_id"] for r in RECORDINGS_FIXTURE],
        "title": [r["title"] for r in RECORDINGS_FIXTURE],
        "duration": [len(r["segments"]) * SEGMENT_SECONDS for r in RECORDINGS_FIXTURE],
        "audio": lance.blob_array([speech([speaker for speaker, _ in r["segments"]]) for r in RECORDINGS_FIXTURE]),
        "mime": ["audio/wav"] * len(RECORDINGS_FIXTURE),
    }
    _fresh_table(client, [*namespace, "recordings"], pa.table(recordings, schema=recording_schema))

    declared = Declared.model_validate(
        {
            "identity": {"key_fields": ["doc_id", "speech_id", "chunk_id"], "doc_key": "doc_id"},
            "document": {"table": "recordings", "media_blob": "audio", "kind": "audio", "mime": "mime"},
            # The time axis the player seeks a hit to and the chunk timeline draws.
            "time": {"start": "start", "end": "end"},
            "display": {
                "title": ["title"],
                "body": "text",
                "caption": "speaker",
                "metadata": [{"field": "title", "label": "Title"}, {"field": "duration", "label": "Seconds"}],
            },
            "search": {"row_table": "segments", "fts": {"table": "segments", "column": "text"}, "filterable": ["doc_id", "speaker"]},
            # The transcript window reads `alignments`, the speakers timeline `diarization`.
            "capabilities": {"alignments": "segments.alignments", "diarization": "turns"},
        }
    )
    _index_and_stamp(client, connection, namespace, "segments", declared)


def seed(client: LanceNamespace, connection: lancedb.DBConnection, warehouse: str) -> list[str]:
    """Write both stamped datasets into ``warehouse``; their ids in the catalog."""
    pages, recordings = [warehouse, PAGES], [warehouse, RECORDINGS]
    seed_pages(client, connection, pages)
    seed_recordings(client, connection, recordings)
    return [CATALOG_DELIMITER.join(pages), CATALOG_DELIMITER.join(recordings)]


#: The unstamped fixture's namespace in the archive catalog, two levels deep, and its table.
ARCHIVE_NAMESPACE = ["field", "survey"]
OBSERVATIONS = "observations"
#: The bare table's directory name under the loose root, and its table name there.
SCANS = "scans"


def _vectors(texts: Sequence[str], dim: int) -> pa.FixedSizeListArray:
    """A deterministic small embedding per text (a hash spread over ``dim`` floats), so similar-row queries have something
    to rank; it is a fixture's stand-in for a model's vectors, not one."""
    values: list[float] = []
    for text in texts:
        digest = hashlib.sha256(text.encode()).digest()
        values.extend((digest[i] - 128) / 128 for i in range(dim))
    return pa.FixedSizeListArray.from_arrays(pa.array(values, pa.float32()), dim)


def seed_archive(root: str) -> list[str]:
    """An UNSTAMPED table in a lance-namespace directory catalog (`lance_docs/ns_catalog/catalog/dir/index.md` § V2), two
    namespace levels deep: field observations with text (a full-text index), numbers, a timestamp and a vector (a vector
    index). No descriptor: the workbench shows it from its schema alone."""
    client = lance_namespace.connect("dir", {"root": root})
    _namespace(client, ARCHIVE_NAMESPACE)
    notes = [
        f"{site}: {what}"
        for site in ("Uppsala", "Visby", "Kalmar", "Luleå")
        for what in ("salt i hamnen", "järn till bruket", "tull på spannmål", "fisk från kusten", "brev från landshövdingen")
    ]
    count = len(notes)
    table = pa.table(
        {
            "observation_id": pa.array(range(1, count + 1), pa.int64()),
            "note": pa.array(notes, pa.utf8()),
            "weight_kg": pa.array([round(12.5 + i * 3.25, 2) for i in range(count)], pa.float64()),
            "observed_at": pa.array([1_600_000_000_000_000 + i * 86_400_000_000 for i in range(count)], pa.timestamp("us", tz="UTC")),
            "tags": pa.array([["coast", "trade"] if i % 2 else ["inland"] for i in range(count)], pa.list_(pa.utf8())),
            "embedding": _vectors(notes, 16),
        }
    )
    table_id = [*ARCHIVE_NAMESPACE, OBSERVATIONS]
    ds = _fresh_table(client, table_id, table)
    ds.create_scalar_index("note", "INVERTED")
    lance.dataset(namespace_client=client, table_id=table_id).create_index("embedding", "IVF_FLAT", num_partitions=1, metric="cosine")
    return [CATALOG_DELIMITER.join(ARCHIVE_NAMESPACE)]


def seed_loose(root: str) -> str:
    """A bare Lance table at ``<root>/scans.lance``, read by path: text, numbers, a timestamp, a vector and an image in a
    blob column, with no index and no descriptor. Its path, the URI a `tables` source names."""
    titles = [document["title"] for document in DOCUMENTS]
    images = [page_image(title, 0) for title in titles]
    schema = pa.schema(
        [
            pa.field("scan", pa.utf8()),
            pa.field("pages", pa.int32()),
            pa.field("scanned_at", pa.timestamp("ms")),
            pa.field("embedding", pa.list_(pa.float32(), 8)),
            lance.blob_field("image"),
        ]
    )
    table = pa.table(
        {
            "scan": titles,
            "pages": [len(document["pages"]) for document in DOCUMENTS],
            "scanned_at": [1_700_000_000_000 + i * 3_600_000 for i in range(len(titles))],
            "embedding": _vectors(titles, 8),
            "image": lance.blob_array(images),
        },
        schema=schema,
    )
    path = os.path.join(root, f"{SCANS}.lance")
    os.makedirs(root, exist_ok=True)
    lance.write_dataset(table, path, mode="overwrite", data_storage_version="2.2", enable_stable_row_ids=True)
    return path


def main() -> None:
    """Seed every demo source under one root: the stamped datasets in ``<root>/lakehouse`` (a directory catalog standing
    in for a lakehouse), an unstamped table in ``<root>/archive`` and a bare table at ``<root>/loose/scans.lance``, the
    three the local stack declares (`scripts/local-sources.json`)."""
    if len(sys.argv) != 2:
        raise SystemExit("usage: seed_demo_corpus.py <root>")
    root = sys.argv[1]
    properties = {"root": os.path.join(root, "lakehouse")}
    client, connection = lance_namespace.connect("dir", properties), lancedb.connect_namespace("dir", properties)
    for dataset in seed(client, connection, DEFAULT_WAREHOUSE):
        namespace = dataset.split(CATALOG_DELIMITER)
        for name in sorted(client.list_tables(ListTablesRequest(id=namespace)).tables or []):
            ds = lance.dataset(namespace_client=client, table_id=[*namespace, name])
            print(f"  {dataset}${name}: {ds.count_rows()} rows, v{ds.version}")
        print(f"seeded lakehouse {dataset}")
    for dataset in seed_archive(os.path.join(root, "archive")):
        print(f"seeded archive {dataset} (no descriptor)")
    print(f"seeded {seed_loose(os.path.join(root, 'loose'))} (a bare table)")


if __name__ == "__main__":
    main()
