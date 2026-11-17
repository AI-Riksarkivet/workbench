#!/usr/bin/env python
"""Seed REAL, SEARCHABLE dev datasets, so every media surface has something to show.

The media plane reads its datasets from the sources a deployment declares (`client_kit.sources`). The local stack
declares three (`scripts/local-sources.json`), one of each kind, and this writes all three under one root:

- ``<root>/lakehouse``: a directory catalog laid out as a lakehouse's (``<warehouse>$<namespace>``), standing in for one,
  holding the two stamped datasets below, each one's descriptor on its row table's schema metadata, where the registry
  reads it;
- ``<root>/archive``: a lance-namespace directory catalog holding one UNSTAMPED table two namespaces deep
  (``field$survey`` → ``observations``: text with a full-text index, numbers, a timestamp, a list, a vector with a vector
  index, a site and a kind to colour by, the ports a cargo went from and to, the observation each follows, and the place
  as a path), which the workbench shows from its schema alone and a person can set an atlas, a graph and a tree up on;
- ``<root>/loose/scans.lance``: a bare Lance table read by path (text, numbers, a timestamp, a vector, an image blob, the
  collection each scan is filed under as a path and the scan it cites), no index and no descriptor.

The three stamped datasets, in one warehouse:

- ``<warehouse>$corpus``: page images. Three volumes of a few pages each, every page a drawn PNG with distinct prose and
  an FTS index, a thumbnail per volume, an atlas projection, a two-layer topic tree over the pages, and a small knowledge
  graph of the people, places and works the pages name.
- ``<warehouse>$recordings``: audio. Three short WAV recordings synthesized here, each a few seconds of two alternating
  voices, cut into timed segments with an FTS index, word alignments (the transcript window) and speaker turns (the
  speakers timeline).

- ``<warehouse>$films``: video. Two short films synthesized here, each a few seconds of a shape moving across a plain
  ground (an object to follow through keyframes) with a tone that swells and fades as its sound, encoded as WebM (VP8 and
  Opus, which every browser's ``<video>`` plays) by PyAV, cut into timed shots with an FTS index.

**Why a corpus and not one row.** A dataset that declared no `search` answered every query with zero hits and looked
broken while being "correct". So: several documents, several rows each, distinct prose per row, and an FTS index, so a
query for a common word returns MANY hits across MANY documents.

This is a DEV FIXTURE, not production data. Every image and sound is made here rather than shipped as a binary, so the
fixture stays reviewable in a diff.

    uv run python scripts/seed_demo_corpus.py [--once] <root>

Then run the media services with the declaration copied into ``<root>`` (`scripts/local-stack.sh` does both). The media
image carries this file (`.docker/media.dockerfile`), and the chart's demo Job runs it there with ``--once`` into the
volume its demo source reads (`chart/templates/demo.yaml`).
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
from fractions import Fraction

import av
import lance
import lance_namespace
import lancedb
import lancedb.index
import numpy as np
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
FILMS = "films"

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
        "topics": [("Förvaltning", "Skatt"), ("Förvaltning", "Skatt"), ("Rättskipning", "Ägotvister"), ("Förvaltning", "Vägar")],
    },
    {
        "doc_id": "a7419c0b2d5e8f31",
        "title": "Sjöfartshandlingar 1701",
        "pages": [
            "Protokoll rörande sjöfart och tullens uppbörd i hamnen under sommaren.",
            "Skeppslistan upptager fjorton fartyg, därav tre från främmande land.",
            "Anteckning om last av salt och järn, samt om besättningens rullor.",
        ],
        "topics": [("Sjöfart", "Tull"), ("Sjöfart", "Fartyg"), ("Sjöfart", "Last")],
    },
    {
        "doc_id": "b2c8e5417d90a6f4",
        "title": "Domboksregister 1688",
        "pages": [
            "Domboken för häradet upptager mål om arv, skuld och olovligt skogshygge.",
            "Protokoll från tinget: nämnden avkunnade dom i tvist om fiskevatten.",
            "Register över parter och vittnen, med hänvisning till Riksarkivet.",
        ],
        "topics": [("Rättskipning", "Arv och skuld"), ("Rättskipning", "Fiske"), ("Rättskipning", "Register")],
    },
]

#: Each page's topic, broadest layer first (``topic_l0``, ``topic_l1``: the layer columns the search's topic filter and the
#: viewer's topic tree read, `client_kit.lancekit.topics_meta`). Every name is unique across the layers, since a topic
#: filters the rows whose ANY layer names it.
TOPIC_ROOT = "Ämnen"

#: The page corpus's knowledge graph: each entity's name, type and the pages that mention it (by ``(document index, page)``),
#: every name a word of those pages' prose, so a search for the name finds them; and the relationships between them. An
#: entity's id is ``sha1(name)[:16]``, the id the viewer's graph doors take.
ENTITIES: list[tuple[str, str, list[tuple[int, int]]]] = [
    ("Riksarkivet", "ORG", [(0, 0), (2, 2)]),
    ("Landshövdingen", "PERSON", [(0, 3)]),
    ("Domboken", "WORK", [(0, 2), (2, 0)]),
    ("Socknen", "GEO", [(0, 2)]),
    ("Protokoll", "CONCEPT", [(0, 0), (1, 0), (2, 1)]),
    ("Hamnen", "GEO", [(1, 0)]),
    ("Sjöfart", "CONCEPT", [(1, 0)]),
    ("Skeppslistan", "WORK", [(1, 1)]),
    ("Tinget", "EVENT", [(2, 1)]),
    ("Nämnden", "ORG", [(2, 1)]),
]
RELATIONSHIPS: list[tuple[str, str, str]] = [
    ("Riksarkivet", "Domboken", "keeps"),
    ("Riksarkivet", "Protokoll", "keeps"),
    ("Landshövdingen", "Riksarkivet", "writes to"),
    ("Domboken", "Socknen", "records a dispute in"),
    ("Domboken", "Tinget", "records"),
    ("Nämnden", "Tinget", "sits at"),
    ("Protokoll", "Sjöfart", "records"),
    ("Skeppslistan", "Hamnen", "lists the ships in"),
    ("Sjöfart", "Hamnen", "passes through"),
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
                    "caption": f"{document['title']} · page {page_no + 1}",
                    "text": prose,
                    "title": document["title"],
                    "atlas_x": x,
                    "atlas_y": y,
                    "atlas_cluster": doc_index,
                    "topic_l0": document["topics"][page_no][0],
                    "topic_l1": document["topics"][page_no][1],
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
            # The topic layers, broadest first: the search's `topic=` filter matches any of them.
            pa.field("topic_l0", pa.utf8()),
            pa.field("topic_l1", pa.utf8()),
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
            # `topic` is the one filter that matches the topic layers rather than a column of its name.
            "search": {"row_table": "chunks", "fts": {"table": "chunks", "column": "text"}, "filterable": ["doc_id", "topic"]},
            # The channel colours points by volume, which makes a lasso selection legible as a selection.
            "atlas": [
                {
                    "name": "text",
                    "table": "chunks",
                    "x": "atlas_x",
                    "y": "atlas_y",
                    "cluster": "atlas_cluster",
                    "source_column": "text",
                    "channels": [{"name": "volume", "column": "title"}, {"name": "topic", "column": "topic_l0"}],
                }
            ],
            # Declared, not probed: the frames capability is what gives the annotator and the player each page's image;
            # `topics` names the topic tree's table and column, `graph` the knowledge graph's entities table, beside
            # which its chunks, mentions and relationships tables are found by name.
            "capabilities": {"frames": "chunks.image", "topics": "topics.hierarchy", "graph": "kg_entities"},
            # The column of the graph's chunks a clip is titled by.
            "graph_presets": {"clip_title_column": "title"},
        }
    )
    _seed_topics(client, namespace)
    _seed_graph(client, namespace)
    _index_and_stamp(client, connection, namespace, "chunks", declared)


def _seed_topics(client: LanceNamespace, namespace: list[str]) -> None:
    """The page corpus's topic tree: the one row the viewer's topics door reads (`layers`, `n_chunks`, and the hierarchy
    as JSON), each leaf valued by its pages, so the treemap's areas sum to the corpus."""
    counts: dict[str, dict[str, int]] = {}
    for document in DOCUMENTS:
        for broad, fine in document["topics"]:
            counts.setdefault(broad, {}).setdefault(fine, 0)
            counts[broad][fine] += 1
    hierarchy = {
        "name": TOPIC_ROOT,
        "children": [{"name": broad, "children": [{"name": fine, "value": n} for fine, n in fines.items()]} for broad, fines in counts.items()],
    }
    pages = sum(len(document["pages"]) for document in DOCUMENTS)
    schema = pa.schema([pa.field("layers", pa.int32()), pa.field("n_chunks", pa.int64()), pa.field("hierarchy", pa.utf8())])
    row = {"layers": 2, "n_chunks": pages, "hierarchy": json.dumps(hierarchy, ensure_ascii=False)}
    _fresh_table(client, [*namespace, "topics"], pa.Table.from_pylist([row], schema=schema))


def _entity_id(name: str) -> str:
    return hashlib.sha1(name.encode(), usedforsecurity=False).hexdigest()[:16]


def chunk_id(doc_index: int, page_no: int) -> str:
    """A page's id in the knowledge graph's chunks: its document and its page."""
    return f"{DOCUMENTS[doc_index]['doc_id']}:{page_no}"


def _seed_graph(client: LanceNamespace, namespace: list[str]) -> None:
    """The page corpus's knowledge graph, as the viewer's graph doors read it: ``kg_entities``, ``kg_chunks`` (one per page,
    a page having no time span), ``kg_mentions`` (entity → chunk) and ``kg_relationships`` (entity → entity)."""
    chunks = [
        {"chunk_id": chunk_id(d, p), "doc_id": document["doc_id"], "start_s": 0.0, "end_s": 0.0, "text": prose, "title": document["title"]}
        for d, document in enumerate(DOCUMENTS)
        for p, prose in enumerate(document["pages"])
    ]
    entities = [{"entity_id": _entity_id(name), "name": name, "entity_type": kind, "mention_count": len(pages)} for name, kind, pages in ENTITIES]
    mentions = [{"source_entity_id": _entity_id(name), "target_chunk_id": chunk_id(d, p)} for name, _, pages in ENTITIES for d, p in pages]
    relationships = [
        {"source_entity_id": _entity_id(source), "target_entity_id": _entity_id(target), "description": description}
        for source, target, description in RELATIONSHIPS
    ]
    tables = {
        "kg_entities": (
            entities,
            pa.schema(
                [
                    pa.field("entity_id", pa.utf8()),
                    pa.field("name", pa.utf8()),
                    pa.field("entity_type", pa.utf8()),
                    pa.field("mention_count", pa.int64()),
                ]
            ),
        ),
        "kg_chunks": (
            chunks,
            pa.schema(
                [
                    pa.field("chunk_id", pa.utf8()),
                    pa.field("doc_id", pa.utf8()),
                    pa.field("start_s", pa.float64()),
                    pa.field("end_s", pa.float64()),
                    pa.field("text", pa.utf8()),
                    pa.field("title", pa.utf8()),
                ]
            ),
        ),
        "kg_mentions": (mentions, pa.schema([pa.field("source_entity_id", pa.utf8()), pa.field("target_chunk_id", pa.utf8())])),
        "kg_relationships": (
            relationships,
            pa.schema([pa.field("source_entity_id", pa.utf8()), pa.field("target_entity_id", pa.utf8()), pa.field("description", pa.utf8())]),
        ),
    }
    for name, (rows, schema) in tables.items():
        _fresh_table(client, [*namespace, name], pa.Table.from_pylist(rows, schema=schema))


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


#: The video fixture: two films, each shot by shot, every shot :data:`SHOT_SECONDS` long with a line saying what it shows.
#: The shape each film follows moves on a path of its own, so a track drawn on it has somewhere to go.
FILMS_FIXTURE: list[dict] = [
    {
        "doc_id": "f1a07c3e9d2b4e68",
        "title": "Lastbåten lägger till 1958",
        "colour": (230, 180, 40),
        "ground": (30, 60, 90),
        "shots": ["Lastbåten glider in mot kajen.", "Båten saktar in och lägger till vid hamnen."],
    },
    {
        "doc_id": "a93e5d1f0c7b2846",
        "title": "Vagnen över bron 1964",
        "colour": (200, 70, 60),
        "ground": (70, 90, 50),
        "shots": ["En vagn rullar ut på bron.", "Vagnen når andra sidan och stannar."],
    },
]
SHOT_SECONDS = 3.0
FILM_FPS = 25
FILM_SIZE = (320, 240)
FILM_AUDIO_RATE = 48_000


def film(colour: tuple[int, int, int], ground: tuple[int, int, int], seconds: float, seed: int) -> bytes:
    """A WebM of ``seconds``: a box of ``colour`` crossing a plain ``ground`` on a gentle curve (each film its own), and
    a tone that swells and fades, VP8 and Opus through PyAV, encoded for speed rather than size."""
    width, height = FILM_SIZE
    frames = int(seconds * FILM_FPS)
    out_buffer = io.BytesIO()
    with av.open(out_buffer, mode="w", format="webm") as out:
        video = out.add_stream("libvpx", rate=FILM_FPS)
        video.width, video.height, video.pix_fmt = width, height, "yuv420p"
        video.options = {"deadline": "realtime", "cpu-used": "8", "b": "400k"}
        sound = out.add_stream("libopus", rate=FILM_AUDIO_RATE)
        sound.layout = "mono"
        for i in range(frames):
            t = i / FILM_FPS
            picture = np.empty((height, width, 3), np.uint8)
            picture[:] = ground
            x = int(16 + (width - 96) * t / seconds)
            y = int(height / 2 - 30 + 40 * math.sin(t * (1.5 + seed)))
            picture[y : y + 50, x : x + 64] = colour
            for packet in video.encode(av.VideoFrame.from_ndarray(picture, format="rgb24")):
                out.mux(packet)
        for packet in video.encode(None):
            out.mux(packet)
        count = int(seconds * FILM_AUDIO_RATE)
        tt = np.arange(count) / FILM_AUDIO_RATE
        samples = (0.3 * np.sin(2 * np.pi * (260 + 70 * seed) * tt) * (0.5 + 0.5 * np.sin(2 * np.pi * 0.8 * tt))).astype(np.float32)
        chunk = 960
        for start in range(0, count, chunk):
            frame = av.AudioFrame.from_ndarray(samples[start : start + chunk][None, :], format="flt", layout="mono")
            frame.sample_rate, frame.pts, frame.time_base = FILM_AUDIO_RATE, start, Fraction(1, FILM_AUDIO_RATE)
            for packet in sound.encode(frame):
                out.mux(packet)
        for packet in sound.encode(None):
            out.mux(packet)
    return out_buffer.getvalue()


def seed_films(client: LanceNamespace, connection: lancedb.DBConnection, namespace: list[str]) -> None:
    """The video dataset: ``shots`` (one timed row per shot), ``films`` (one row per film, its WebM) and the
    descriptor."""
    _namespace(client, namespace)
    shots = [
        {
            "doc_id": f["doc_id"],
            "speech_id": 0,
            "chunk_id": index,
            "start": index * SHOT_SECONDS,
            "end": (index + 1) * SHOT_SECONDS,
            "text": text,
            "title": f["title"],
        }
        for f in FILMS_FIXTURE
        for index, text in enumerate(f["shots"])
    ]
    shot_schema = pa.schema(
        [
            pa.field("doc_id", pa.utf8()),
            pa.field("speech_id", pa.int64()),
            pa.field("chunk_id", pa.int64()),
            pa.field("start", pa.float64()),
            pa.field("end", pa.float64()),
            pa.field("text", pa.utf8()),
            pa.field("title", pa.utf8()),
        ]
    )
    _fresh_table(client, [*namespace, "shots"], pa.Table.from_pylist(shots, schema=shot_schema))
    film_schema = pa.schema(
        [
            pa.field("doc_id", pa.utf8()),
            pa.field("title", pa.utf8()),
            pa.field("duration", pa.float64()),
            lance.blob_field("video"),
            pa.field("mime", pa.utf8()),
        ]
    )
    films = {
        "doc_id": [f["doc_id"] for f in FILMS_FIXTURE],
        "title": [f["title"] for f in FILMS_FIXTURE],
        "duration": [len(f["shots"]) * SHOT_SECONDS for f in FILMS_FIXTURE],
        "video": lance.blob_array([film(f["colour"], f["ground"], len(f["shots"]) * SHOT_SECONDS, i) for i, f in enumerate(FILMS_FIXTURE)]),
        "mime": ["video/webm"] * len(FILMS_FIXTURE),
    }
    _fresh_table(client, [*namespace, "films"], pa.table(films, schema=film_schema))
    declared = Declared.model_validate(
        {
            "identity": {"key_fields": ["doc_id", "speech_id", "chunk_id"], "doc_key": "doc_id"},
            "document": {"table": "films", "media_blob": "video", "kind": "video", "mime": "mime"},
            "time": {"start": "start", "end": "end"},
            "display": {"title": ["title"], "body": "text", "metadata": [{"field": "duration", "label": "Seconds"}]},
            "search": {"row_table": "shots", "fts": {"table": "shots", "column": "text"}, "filterable": ["doc_id"]},
        }
    )
    _index_and_stamp(client, connection, namespace, "shots", declared)


def seed(client: LanceNamespace, connection: lancedb.DBConnection, warehouse: str) -> list[str]:
    """Write the three stamped datasets into ``warehouse``; their ids in the catalog."""
    pages, recordings, films = [warehouse, PAGES], [warehouse, RECORDINGS], [warehouse, FILMS]
    seed_pages(client, connection, pages)
    seed_recordings(client, connection, recordings)
    seed_films(client, connection, films)
    return [CATALOG_DELIMITER.join(pages), CATALOG_DELIMITER.join(recordings), CATALOG_DELIMITER.join(films)]


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


#: The archive's sites, each with where it lies as a path and the port its cargo goes to next.
SITES: dict[str, tuple[str, str]] = {
    "Uppsala": ("Sverige/Svealand/Uppland/Uppsala", "Visby"),
    "Visby": ("Sverige/Götaland/Gotland/Visby", "Kalmar"),
    "Kalmar": ("Sverige/Götaland/Småland/Kalmar", "Lübeck"),
    "Luleå": ("Sverige/Norrland/Norrbotten/Luleå", "Uppsala"),
}
KINDS = ("salt i hamnen", "järn till bruket", "tull på spannmål", "fisk från kusten", "brev från landshövdingen")


def _clustered(groups: Sequence[str], texts: Sequence[str], dim: int) -> pa.FixedSizeListArray:
    """A deterministic embedding per row near its group's own point, so a projection of it shows the groups apart; a
    fixture's stand-in for a model's vectors, not one."""
    centre = {group: [(b - 128) / 32 for b in hashlib.sha256(group.encode()).digest()[:dim]] for group in set(groups)}
    values: list[float] = []
    for group, text in zip(groups, texts, strict=True):
        digest = hashlib.sha256(text.encode()).digest()
        values.extend(centre[group][i] + (digest[i] - 128) / 256 for i in range(dim))
    return pa.FixedSizeListArray.from_arrays(pa.array(values, pa.float32()), dim)


def seed_archive(root: str) -> list[str]:
    """An UNSTAMPED table in a lance-namespace directory catalog (`lance_docs/ns_catalog/catalog/dir/index.md` § V2), two
    namespace levels deep: field observations with text (a full-text index), numbers, a timestamp and a vector (a vector
    index), and the columns a person sets an atlas, a graph and a tree up on: a site and a kind (a colour), the ports a
    cargo went from and to (an edge, a sink port no row leaves from and a cargo kept at home), the observation each
    follows at its site (a reference to another row), and the place as a path. No descriptor: the workbench shows it from
    its schema alone."""
    client = lance_namespace.connect("dir", {"root": root})
    _namespace(client, ARCHIVE_NAMESPACE)
    rows = [(year, site, what) for year in (1701, 1702, 1703) for site in SITES for what in KINDS]
    notes = [f"{site} {year}: {what}" for year, site, what in rows]
    count = len(notes)
    last_at: dict[str, int] = {}
    follows: list[int | None] = []
    for i, (_, site, _what) in enumerate(rows):
        follows.append(last_at.get(site))
        last_at[site] = i + 1
    table = pa.table(
        {
            "observation_id": pa.array(range(1, count + 1), pa.int64()),
            "note": pa.array(notes, pa.utf8()),
            "weight_kg": pa.array([round(12.5 + i * 3.25, 2) for i in range(count)], pa.float64()),
            "observed_at": pa.array([1_600_000_000_000_000 + i * 86_400_000_000 for i in range(count)], pa.timestamp("us", tz="UTC")),
            "tags": pa.array([["coast", "trade"] if i % 2 else ["inland"] for i in range(count)], pa.list_(pa.utf8())),
            "embedding": _clustered([site for _, site, _ in rows], notes, 16),
            "site": pa.array([site for _, site, _ in rows], pa.utf8()),
            "kind": pa.array([what.split(" ")[0] for _, _, what in rows], pa.utf8()),
            "shipped_from": pa.array([site for _, site, _ in rows], pa.utf8()),
            "shipped_to": pa.array([site if what.startswith("brev") else SITES[site][1] for _, site, what in rows], pa.utf8()),
            "follows": pa.array(follows, pa.int64()),
            "place": pa.array([SITES[site][0] for _, site, _ in rows], pa.utf8()),
        }
    )
    table_id = [*ARCHIVE_NAMESPACE, OBSERVATIONS]
    ds = _fresh_table(client, table_id, table)
    ds.create_scalar_index("note", "INVERTED")
    lance.dataset(namespace_client=client, table_id=table_id).create_index("embedding", "IVF_FLAT", num_partitions=1, metric="cosine")
    return [CATALOG_DELIMITER.join(ARCHIVE_NAMESPACE)]


#: The scan the last scan cites, which the archive does not hold.
LOST_SCAN = "Bouppteckningar 1699"


def seed_loose(root: str) -> str:
    """A bare Lance table at ``<root>/scans.lance``, read by path: text, numbers, a timestamp, a vector, an image in a
    blob column, the collection each scan is filed under as a path and the scan it cites (one of them a scan it does not
    hold), with no index and no descriptor. Its path, the URI a `tables` source names."""
    titles = [document["title"] for document in DOCUMENTS]
    images = [page_image(title, 0) for title in titles]
    schema = pa.schema(
        [
            pa.field("scan", pa.utf8()),
            pa.field("pages", pa.int32()),
            pa.field("scanned_at", pa.timestamp("ms")),
            pa.field("embedding", pa.list_(pa.float32(), 8)),
            lance.blob_field("image"),
            pa.field("collection", pa.utf8()),
            pa.field("cites", pa.utf8()),
        ]
    )
    table = pa.table(
        {
            "scan": titles,
            "pages": [len(document["pages"]) for document in DOCUMENTS],
            "scanned_at": [1_700_000_000_000 + i * 3_600_000 for i in range(len(titles))],
            "embedding": _vectors(titles, 8),
            "image": lance.blob_array(images),
            "collection": [f"Arkivet/Handlingar/{title.split()[-1][:2]}00-talet" for title in titles],
            # Each scan cites the one before it, and the last a scan the archive does not hold: an orphan in a tree of
            # citations, and a reference that matches no row in their graph.
            "cites": [None, *titles[:-2], LOST_SCAN],
        },
        schema=schema,
    )
    path = os.path.join(root, f"{SCANS}.lance")
    os.makedirs(root, exist_ok=True)
    lance.write_dataset(table, path, mode="overwrite", data_storage_version="2.2", enable_stable_row_ids=True)
    return path


#: The file a completed seed leaves under its root, written after every table; its presence is what ``--once`` reads.
SEEDED_MARKER = ".seeded"


def _mark_seeded(root: str) -> None:
    """Record that every table under ``root`` was written, by a rename, so a seed cut short leaves no marker.

    The tree is flushed to the disk before the marker is, and the marker's rename after it: a node that crashes cannot
    leave a marker standing over tables still in its page cache."""
    os.sync()
    pending = os.path.join(root, f"{SEEDED_MARKER}.pending")
    with open(pending, "w", encoding="utf-8") as marker:
        marker.write("seeded by scripts/seed_demo_corpus.py\n")
        marker.flush()
        os.fsync(marker.fileno())
    os.replace(pending, os.path.join(root, SEEDED_MARKER))
    directory = os.open(root, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def main() -> None:
    """Seed every demo source under one root: the stamped datasets in ``<root>/lakehouse`` (a directory catalog standing
    in for a lakehouse), an unstamped table in ``<root>/archive`` and a bare table at ``<root>/loose/scans.lance``, the
    three the local stack declares (`scripts/local-sources.json`).

    With ``--once`` (the chart's demo Job, `chart/templates/demo.yaml`), a root a completed seed already filled is left
    as it is and the run exits 0, so a release installed again from git never rewrites data a person's annotations refer
    to; a root holding a seed cut short (no marker) is seeded again from nothing, which every table's drop-and-create
    makes safe. Without it every table is rewritten, as the local stack wants on each start."""
    args = sys.argv[1:]
    once = args[:1] == ["--once"]
    if once:
        args = args[1:]
    if len(args) != 1:
        raise SystemExit("usage: seed_demo_corpus.py [--once] <root>")
    root = args[0]
    if once and os.path.exists(os.path.join(root, SEEDED_MARKER)):
        print(f"{root} holds a completed seed ({SEEDED_MARKER}): nothing written")
        return
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
    _mark_seeded(root)


if __name__ == "__main__":
    main()
