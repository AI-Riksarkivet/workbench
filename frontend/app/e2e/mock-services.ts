// Seed-driven stand-ins for the workbench's three services, each on its own port behind the e2e edge,
// as a deployment's HTTPRoutes put them: the viewer (`/api/viewer`), search (`/api/search`) and the
// annotator (`/api/annotator`). Each answers its own paths below that base, as the edge rewrites it.
//
// EVERY CALL IS HELD TO THE SERVICES' RULE (`service_kit.governed.deps`), as a deployment configures it for the
// provider that issued the token (`providers.ts`): a token addressed to the workbench's audience; at a provider that
// binds tokens, `Authorization: DPoP <token>` and one `DPoP` proof whose `htm` is the method and whose `htu` is the
// PUBLIC URL the browser called without its query (the edge passes the public base in `x-forwarded-prefix`, as a
// service is configured with `WORKBENCH_PUBLIC_URL`), whose `ath` hashes the token, and whose key is the one the
// token is bound to; at a provider that binds none (`WORKBENCH_DPOP_REQUIRED=false`), a bearer naming no key. A call
// that breaks any rule is refused 401 and recorded as refused. The one door that takes no token is the signed media
// link's GET: the link is the permission.
//
// The dataset list is read from the lakehouse as the person, and an annotation save is written to it as the
// workbench's own client (`mock-lakehouse.ts`); `GET /api/viewer/__lakehouse?sub=<sub>` answers what it admitted.
//
// `GET /api/viewer/__calls?sub=<sub>` answers what every service recorded for one person, so a spec
// can say each call it caused carried a valid proof. `POST /api/viewer/__fault?sub=<sub>&path=<path>&kind=`
// makes one door fail (`fail`), answer that nothing is built there yet (`unbuilt`) or answer 2.5 s late
// (`slow`), for that person only;
// `clear` puts it back.
import { readFileSync } from 'node:fs';
import {
	Dictionary,
	Float16,
	Float64,
	Int32,
	Int64,
	Table,
	Utf8,
	type Vector,
	tableToIPC,
	vectorFromArray,
} from 'apache-arrow';
import { athOf, readToken, verifyProof, type TokenClaims } from './dpop';
import { lakehouseCallsOf, readAsPerson } from './mock-lakehouse';
import { SHAPES, shapeOf } from './providers';
import { annotationsDoor, assistDoor } from './mock-annotations';
import {
	draftOf,
	orgsOf,
	permissionsOn,
	projectsDoor,
	registerName,
	taskOf,
	writeDraft,
} from './mock-projects';
import { MOCK_ANNOTATOR_PORT, MOCK_SEARCH_PORT, MOCK_VIEWER_PORT } from './ports';

/** One call as a service saw it. */
export interface Call {
	service: string;
	method: string;
	path: string;
	/** The query string it carried (the viewer records it). */
	query?: string;
	status: number;
	/** Why it was refused, when it was. */
	refused?: string;
}

const ledger = new Map<string, Call[]>();
function record(sub: string, call: Call): void {
	ledger.set(sub, [...(ledger.get(sub) ?? []), call]);
}

const json = (body: unknown, status = 200, headers: Record<string, string> = {}): Response =>
	new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json', ...headers },
	});

/** The person a call's token proves, and the token itself, with its DPoP proof where one is due; or the 401 that
 *  refuses it. */
async function authenticate(
	service: string,
	req: Request,
	url: URL,
): Promise<(TokenClaims & { token: string; host: string }) | Response> {
	const host = req.headers.get('x-forwarded-host') ?? url.host;
	const prefix = req.headers.get('x-forwarded-prefix') ?? '';
	// The path as it came over the wire (`URL.pathname` keeps its percent-encoding), never a decoded one: the services
	// read ASGI's `raw_path`, and the comparison normalises both sides (`dpop.ts`).
	const htu = `http://${host}${prefix}${url.pathname}`;
	const [scheme, token = ''] = (req.headers.get('authorization') ?? '').split(' ');
	const claims = readToken(token);
	const refuse = (reason: string): Response => {
		record(claims?.sub ?? 'unknown', {
			service,
			method: req.method,
			path: url.pathname,
			status: 401,
			refused: reason,
		});
		return json({ detail: reason }, 401, { 'www-authenticate': 'DPoP error="invalid_token"' });
	};
	if (!claims) return refuse('no token, or not one this issuer minted');
	const shape = SHAPES[shapeOf(claims.iss)];
	if (!claims.aud.includes(shape.workbenchAudience))
		return refuse('the token is not addressed to the workbench');
	if (claims.exp < Date.now() / 1000) return refuse('the token expired');
	if (scheme === 'Bearer' && !shape.dpopRequired) {
		if (claims.cnf) return refuse('a DPoP-bound token must be presented with its DPoP proof');
		return { ...claims, token, host };
	}
	if (scheme !== 'DPoP') return refuse('the token is not presented with the DPoP scheme');
	if (req.headers.get('dpop')?.includes(',')) return refuse('more than one DPoP proof');
	try {
		const proof = await verifyProof(req.headers.get('dpop'), req.method, htu);
		if (proof.ath !== athOf(token)) return refuse('ath is not the hash of the access token');
		if (proof.jkt !== claims.cnf?.jkt) return refuse('the token is not bound to the proof key');
	} catch (err) {
		return refuse(`invalid_dpop_proof: ${err instanceof Error ? err.message : String(err)}`);
	}
	return { ...claims, token, host };
}

// ── the seed ────────────────────────────────────────────────────────────────────────────────────

interface Doc {
	doc_id: string;
	title: string;
	collection: string;
	mime: string;
	hue: number;
	/** The document's chunks: its text, a row each. */
	chunks: string[];
}

const DOCS: Doc[] = [
	{
		doc_id: 'letter-001',
		title: 'A letter from the harbour',
		collection: 'Letters',
		mime: 'image/svg+xml',
		hue: 20,
		chunks: ['The ships came into the harbour at dawn.', 'The harbour master counted twelve.'],
	},
	{
		doc_id: 'letter-002',
		title: 'A letter about the harvest',
		collection: 'Letters',
		mime: 'image/svg+xml',
		hue: 140,
		chunks: ['The harvest was late this year.'],
	},
	{
		doc_id: 'map-003',
		title: 'A map of the harbour',
		collection: 'Maps',
		mime: 'image/svg+xml',
		hue: 260,
		chunks: ['Soundings along the harbour mouth.'],
	},
];

/** A table's facts as the viewer's descriptor names them. */
const table = (name: string, rows: number, version: number, columns: string[]) => ({
	name,
	row_count: rows,
	version,
	columns: columns.map((column) => ({ name: column, arrow_type: 'string', nullable: true })),
	indexes: [],
});

const CHUNK_ROWS = DOCS.reduce((n, d) => n + d.chunks.length, 0);

/** The memos' rows: thirty lines of three memos, more than one browse page holds. */
const MEMOS = Array.from({ length: 30 }, (_, i) => ({
	memo_id: `memo${Math.floor(i / 10)}`,
	line: i % 10,
	title: `Memo ${Math.floor(i / 10) + 1}`,
	text: `Line ${(i % 10) + 1}: the harbour master writes of salt and grain.`,
}));

/** The lakehouse's datasets: the letters (documents, chunks, thumbnails and an atlas), the maps, the notes (none of
 *  them), and the memos (a corpus of texts). */
const DATASETS = {
	'lake~acme$letters': {
		tables: {
			documents: table('documents', DOCS.length, 3, ['doc_id', 'title', 'collection', 'mime']),
			chunks: table('chunks', CHUNK_ROWS, 3, ['doc_id', 'chunk_id', 'title', 'text']),
		},
		capabilities: ['search', 'atlas'],
		declared: {
			identity: { doc_key: 'doc_id', key_fields: ['doc_id', 'chunk_id'] },
			document: { table: 'documents', media_blob: 'media', mime: 'mime', thumbnail: 'thumb' },
			display: {
				title: ['title'],
				body: 'text',
				caption: 'caption',
				metadata: [{ field: 'collection', label: 'Collection' }],
			},
			atlas: [
				{ name: 'umap', x: 'x', y: 'y', cluster: 'topic', source_column: 'v', table: 'chunks' },
			],
			search: { row_table: 'chunks', fts: { table: 'chunks', column: 'text' } },
			capabilities: { graph: 'kg', topics: 'topics.tree' },
		},
	},
	// A second dataset with an atlas and a graph of its own, so moving between two datasets shows each
	// page drawing the one it is on.
	'lake~acme$maps': {
		tables: {
			documents: table('documents', 1, 2, ['doc_id', 'title', 'collection', 'mime']),
			chunks: table('chunks', 1, 2, ['doc_id', 'chunk_id', 'title', 'text']),
		},
		capabilities: ['search', 'atlas'],
		declared: {
			identity: { doc_key: 'doc_id', key_fields: ['doc_id', 'chunk_id'] },
			document: { table: 'documents', media_blob: 'media', mime: 'mime', thumbnail: 'thumb' },
			display: { title: ['title'], body: 'text', metadata: [] },
			atlas: [
				{ name: 'umap', x: 'x', y: 'y', cluster: 'topic', source_column: 'v', table: 'chunks' },
			],
			search: { row_table: 'chunks', fts: { table: 'chunks', column: 'text' } },
			capabilities: { graph: 'kg' },
		},
	},
	'lake~acme$notes': {
		tables: { notes: table('notes', 0, 1, ['note_id', 'text']) },
		capabilities: [],
		declared: { identity: { doc_key: 'note_id', key_fields: ['note_id'] }, atlas: [] },
	},
	// A corpus of texts: a row table and no document, so its items are its rows (`GET /api/rows`).
	'lake~acme$memos': {
		tables: { memos: table('memos', MEMOS.length, 2, ['memo_id', 'line', 'title', 'text']) },
		capabilities: ['search'],
		declared: {
			identity: { doc_key: 'memo_id', key_fields: ['memo_id', 'line'] },
			display: { title: ['title'], body: 'text', metadata: [] },
			atlas: [],
			search: { row_table: 'memos', fts: { table: 'memos', column: 'text' } },
		},
	},
} as const;

/** A dataset with no workbench descriptor, in a read-only lance-namespace source: shown as a plain table, its
 *  rows read from its schema (`viewer.services.plain_rows`). Its note column carries a full-text index and its
 *  embedding a vector index, so search and similar rows work; its tags are a list, which Lance does not order. */
const PLAIN_ID = 'archive~field$survey';
const PLAIN_TABLE = 'observations';
const column = (
	name: string,
	arrow_type: string,
	kind: string,
	extra: Record<string, unknown> = {},
) => ({
	name,
	arrow_type,
	kind,
	sortable: false,
	text_search: false,
	similar: false,
	dim: null,
	...extra,
});
const PLAIN_COLUMNS = [
	column('n', 'int64', 'integer', { sortable: true }),
	column('note', 'string', 'text', { sortable: true, text_search: true }),
	column('tags', 'list<item: string>', 'json'),
	column('embedding', 'fixed_size_list<item: float>[4]', 'vector', { similar: true, dim: 4 }),
	column('image', 'extension<lance.blob.v2<BlobType>>', 'blob'),
	column('site', 'string', 'text', { sortable: true }),
	column('src', 'string', 'text', { sortable: true }),
	column('dst', 'string', 'text', { sortable: true }),
	column('place', 'string', 'text', { sortable: true }),
];
const PLAIN_ROWS = [
	{
		n: 3,
		note: 'salt in the harbour',
		tags: '["coast"]',
		site: 'Visby',
		src: 'Visby',
		dst: 'Kalmar',
		place: 'Sverige/Gotland/Visby',
	},
	{
		n: 1,
		note: 'iron to the works',
		tags: '["inland"]',
		site: 'Kalmar',
		src: 'Kalmar',
		dst: 'Visby',
		place: 'Sverige/Småland/Kalmar',
	},
	{
		n: 4,
		note: 'salt and grain',
		tags: '["coast","trade"]',
		site: 'Visby',
		src: 'Visby',
		dst: 'Lübeck',
		place: 'Sverige/Gotland/Visby',
	},
	{
		n: 2,
		note: 'letters from the governor',
		tags: '[]',
		site: 'Uppsala',
		src: 'Uppsala',
		dst: 'Visby',
		place: 'Sverige/Uppland/Uppsala',
	},
].map((row, rowid) => ({
	rowid,
	cells: {
		...row,
		embedding: { dim: 4, head: [rowid, 0.5, 0.25, 0] },
		image: { size: 1024 + rowid },
	} as Record<string, unknown>,
	media: {},
	score: null as number | null,
}));
const READ_ONLY = "source 'archive' is not marked annotatable";
const NOT_WRITTEN =
	'the catalog lists it, but holds no version of it to read: declared and never written, or its data removed';
const PLAIN_FACTS = {
	id: PLAIN_ID,
	source: 'archive',
	kind: 'namespace',
	stamped: false,
	tables: {
		[PLAIN_TABLE]: {
			name: PLAIN_TABLE,
			row_count: PLAIN_ROWS.length,
			version: 3,
			columns: PLAIN_COLUMNS.map((c) => ({
				name: c.name,
				arrow_type: c.arrow_type,
				nullable: true,
			})),
			indexes: [
				{ name: 'note_idx', index_type: 'Inverted', columns: ['note'] },
				{ name: 'embedding_idx', index_type: 'Vector', columns: ['embedding'] },
			],
		},
	},
	capabilities: [],
	problem: null,
	unreadable: { drafts: NOT_WRITTEN },
	read_only: READ_ONLY,
	setup_limits: { vector_max_dim: 2048 },
};

/** One page of the plain table, as the viewer answers it: sorted, searched or ranked. */
function plainPage(params: URLSearchParams): unknown {
	const offset = Number(params.get('offset') ?? 0);
	const limit = Number(params.get('limit') ?? 50);
	let rows = [...PLAIN_ROWS];
	const q = params.get('q');
	const near = params.get('near');
	const sort = params.get('sort');
	if (q) {
		rows = rows.filter((r) => String(r.cells.note).includes(q)).map((r) => ({ ...r, score: 0.5 }));
	} else if (near !== null) {
		rows = rows
			.filter((r) => r.rowid !== Number(near))
			.map((r, i) => ({ ...r, score: 0.1 * (i + 1) }));
	} else if (sort === 'n' || sort === 'note') {
		rows.sort((a, b) => (String(a.cells[sort]) < String(b.cells[sort]) ? -1 : 1));
		if (params.get('order') === 'desc') rows.reverse();
	}
	return {
		table: PLAIN_TABLE,
		qualified: `${PLAIN_ID}$${PLAIN_TABLE}`,
		version: 3,
		columns: PLAIN_COLUMNS,
		total: rows.length,
		offset,
		limit,
		rows: rows.slice(offset, offset + limit),
	};
}

// ── a plain table's set-up reads (`viewer.api.v1.endpoints.derived_views`) ─────────────────────

const plainCells = PLAIN_ROWS.map((r) => r.cells as Record<string, string | number>);
const extent = { total: PLAIN_ROWS.length, read: PLAIN_ROWS.length, sampled: false, cut: null };

/** What the viewer's profile says of the plain table: each column's kind and what its four rows hold. */
function plainProfile(): unknown {
	const profiled = PLAIN_COLUMNS.map((c) => {
		const values =
			c.kind === 'text' || c.kind === 'integer' ? plainCells.map((r) => String(r[c.name])) : [];
		const distinct = new Set(values);
		const separated = values.filter((v) => v.includes('/')).length / (values.length || 1);
		return {
			name: c.name,
			kind: c.kind,
			dim: c.dim,
			distinct: values.length ? distinct.size : null,
			distinct_capped: false,
			nulls: 0,
			separator: separated > 0 ? '/' : null,
			separated,
			examples: [...distinct].sort().slice(0, 3),
			refers_to: {},
		};
	});
	return { table: PLAIN_TABLE, version: 3, extent, columns: profiled };
}

/** The atlas the plain table's vector draws: four points, coloured and labelled by the picked columns. */
function plainAtlas(params: URLSearchParams): Uint8Array {
	const color = params.get('color');
	const label = params.get('label');
	// oxlint-disable-next-line typescript/no-explicit-any -- a table of several column types, as `new Table` takes it
	const columns: Record<string, Vector<any>> = {
		x: vectorFromArray([-0.8, 0.6, -0.05, 0.9], new Float16()),
		y: vectorFromArray([0.5, -0.6, 0.02, 0.2], new Float16()),
		rowid: vectorFromArray(
			PLAIN_ROWS.map((r) => BigInt(r.rowid)),
			new Int64(),
		),
	};
	if (color) {
		columns.color = vectorFromArray(
			plainCells.map((r) => String(r[color])),
			new Dictionary(new Utf8(), new Int32()),
		);
	}
	if (label) {
		columns.label = vectorFromArray(
			plainCells.map((r) => String(r[label])),
			new Dictionary(new Utf8(), new Int32()),
		);
	}
	const table = new Table(columns);
	const meta = table.schema.metadata;
	for (const [key, value] of Object.entries({
		count: '4',
		total: '4',
		read: '4',
		sampled: 'false',
		method: params.get('vector') ? 'pca' : 'columns',
		...(params.get('vector') ? { variance: '0.9100' } : {}),
		...(color ? { color_kind: 'category' } : {}),
	})) {
		meta.set(key, value);
	}
	return tableToIPC(table, 'stream');
}

/** A graph node's typed key, as the viewer keys it: `i:` for an integer column, `s:` for text. */
const graphKey = (column: string, value: string | number | undefined): string =>
	`${PLAIN_COLUMNS.find((c) => c.name === column)?.kind === 'integer' ? 'i' : 's'}:${value}`;

/** The rows of the plain table whose from or to names the graph's node `key`. */
function rowsNaming(key: string, source: string, target: string): number[] {
	return PLAIN_ROWS.flatMap((r, i) =>
		graphKey(source, plainCells[i]?.[source]) === key ||
		graphKey(target, plainCells[i]?.[target]) === key
			? [r.rowid]
			: [],
	);
}

/** The graph the picked from and to columns link, each node with the rows that name it. */
function plainGraph(params: URLSearchParams) {
	const source = params.get('source') ?? '';
	const target = params.get('target') ?? '';
	const edges = plainCells.map((r) => ({
		source: graphKey(source, r[source]),
		target: graphKey(target, r[target]),
		description: '',
	}));
	const ids = [...new Set(edges.flatMap((e) => [e.source, e.target]))].sort();
	const sources = new Set(edges.map((e) => e.source));
	return {
		built: true,
		nodes: ids.map((id) => {
			const naming = rowsNaming(id, source, target);
			return {
				id,
				name: id.slice(2),
				type: '',
				mentions: edges.filter((e) => e.source === id || e.target === id).length,
				rows: naming.length,
				first: naming[0] ?? 0,
			};
		}),
		edges,
		diagnostics: {
			extent,
			nodes: ids.length,
			edges: edges.length,
			null_rows: 0,
			dropped_weights: 0,
			self_loops: 0,
			unmatched_targets: [...new Set(edges.map((e) => e.target))].filter((t) => !sources.has(t))
				.length,
			mixed_types: false,
			clipped_keys: 0,
			cut_nodes: 0,
			cut_edges: 0,
		},
	};
}

/** The rows the graph door's node `key` stands for. */
const plainGraphRows = (params: URLSearchParams): number[] =>
	rowsNaming(params.get('key') ?? '', params.get('source') ?? '', params.get('target') ?? '');

interface PlainNode {
	name: string;
	id?: string;
	value?: number;
	rows: number;
	children?: PlainNode[];
}

/** A path's levels as the viewer reads them: split at the separator, empty levels dropped. */
const levelsOf = (path: string, separator: string): string[] =>
	path.split(separator).filter((level) => level !== '');

/** The tree the picked path column nests, each node keyed by its levels and counting the rows below it. */
function plainTree(params: URLSearchParams) {
	const column = params.get('node') ?? '';
	const separator = params.get('separator') ?? '/';
	const root: PlainNode = { name: 'All rows', rows: 0, children: [] };
	let nodes = 0;
	for (const r of plainCells) {
		let here = root;
		const levels = levelsOf(String(r[column]), separator);
		root.rows += 1;
		levels.forEach((level, depth) => {
			const id = JSON.stringify({ path: levels.slice(0, depth + 1) });
			here.children ??= [];
			let next = here.children.find((c) => c.id === id);
			if (!next) {
				next = { name: level, id, rows: 0 };
				here.children.push(next);
				nodes += 1;
			}
			next.rows += 1;
			here = next;
		});
		here.value = (here.value ?? 0) + 1;
	}
	return {
		built: true,
		hierarchy: root,
		noise_label: '',
		diagnostics: {
			extent,
			nodes,
			roots: 1,
			orphans: 0,
			cycles: 0,
			null_rows: 0,
			dropped_sizes: 0,
			clipped: 0,
			depth: 3,
			cut_nodes: 0,
		},
	};
}

/** The rows of the plain table at or under the path tree's node `key`. */
function plainTreeRows(params: URLSearchParams): number[] {
	const column = params.get('node') ?? '';
	const separator = params.get('separator') ?? '/';
	const { path } = JSON.parse(params.get('key') ?? '{"path":[]}') as { path: string[] };
	return PLAIN_ROWS.flatMap((r, i) => {
		const levels = levelsOf(String(plainCells[i]?.[column]), separator);
		return path.every((level, at) => levels[at] === level) ? [r.rowid] : [];
	});
}

/** One page of the rows a node stands for, as the node-rows doors answer it. */
function nodePage(rowids: number[], params: URLSearchParams): unknown {
	const offset = Number(params.get('offset') ?? 0);
	const limit = Number(params.get('limit') ?? 50);
	const rows = PLAIN_ROWS.filter((r) => rowids.includes(r.rowid));
	return {
		table: PLAIN_TABLE,
		qualified: `${PLAIN_ID}$${PLAIN_TABLE}`,
		version: 3,
		columns: PLAIN_COLUMNS,
		total: rows.length,
		offset,
		limit,
		rows: rows.slice(offset, offset + limit),
		extent,
	};
}

// ── the demo's plain tables, as the viewer itself answers them (`tests/derived_recording.py`) ────────────

/** One answer the viewer gave: JSON, or an Arrow IPC stream in base64. */
type Recorded = { json: unknown } | { arrow: string } | { bytes: string };

/** An answer's key: its path and its query's parameters, decoded and sorted, as the recording keys it. */
const seededKey = (path: string, params: URLSearchParams): string =>
	`${decodeURIComponent(path)}\u0000${JSON.stringify([...params.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)))}`;

/** The recording, written by `scripts/record_derived_answers.py` and held to the viewer by its own suite. */
const SEEDED = new Map(
	Object.entries(
		JSON.parse(readFileSync(new URL('./seeded-answers.json', import.meta.url), 'utf8')) as Record<
			string,
			Recorded
		>,
	).map(([key, answer]) => {
		const [path = '', query = ''] = key.split('?');
		return [seededKey(path, new URLSearchParams(query)), answer] as const;
	}),
);
/** The demo's recording and film the canvas specs label: their media, served by the signed link as the viewer serves
 *  it. The film is its recorded WebM; the recording a WAV of tones of its own six seconds (the recording records the
 *  waveform the viewer decodes, `/peaks`, not the recording's bytes). */
const TIMED_MEDIA: Record<string, () => { body: Uint8Array; type: string }> = {
	c41f0e2a9b7d3e15: () => ({ body: toneWav(6), type: 'audio/wav' }),
	f1a07c3e9d2b4e68: () => {
		const answer = SEEDED.get(seededKey('/__media/f1a07c3e9d2b4e68', new URLSearchParams()));
		const bytes = answer && 'bytes' in answer ? String(answer.bytes) : '';
		return { body: Uint8Array.from(Buffer.from(bytes, 'base64')), type: 'video/webm' };
	},
};

/** A mono 16-bit WAV of a 220 Hz tone swelling and fading, `seconds` long, at 8 kHz. */
function toneWav(seconds: number): Uint8Array {
	const rate = 8000;
	const count = rate * seconds;
	const wav = Buffer.alloc(44 + count * 2);
	wav.write('RIFF', 0);
	wav.writeUInt32LE(36 + count * 2, 4);
	wav.write('WAVEfmt ', 8);
	wav.writeUInt32LE(16, 16);
	wav.writeUInt16LE(1, 20);
	wav.writeUInt16LE(1, 22);
	wav.writeUInt32LE(rate, 24);
	wav.writeUInt32LE(rate * 2, 28);
	wav.writeUInt16LE(2, 32);
	wav.writeUInt16LE(16, 34);
	wav.write('data', 36);
	wav.writeUInt32LE(count * 2, 40);
	for (let i = 0; i < count; i += 1) {
		const t = i / rate;
		wav.writeInt16LE(
			Math.round(9000 * Math.sin(2 * Math.PI * 220 * t) * (0.5 + 0.5 * Math.sin(2 * Math.PI * t))),
			44 + i * 2,
		);
	}
	return Uint8Array.from(wav);
}

/** The links signed for the timed media, by token. */
const signedTimed = new Map<string, { doc: string; exp: number }>();

/** The recorded datasets: a read of theirs the recording lacks is refused, not answered by another mock. */
const SEEDED_DATASETS = ['seeded~field$survey', 'loose~'];

type DatasetId = keyof typeof DATASETS;
const isDataset = (id: string | null): id is DatasetId => id !== null && id in DATASETS;

/** A small picture per document, so a thumbnail and its media are visibly that document's; a chunk's frame turns the
 *  document's hue by its chunk, so each page of a document is visibly its own. */
const picture = (doc: Doc, role: string, chunk = 0): string =>
	`<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160" viewBox="0 0 160 160">` +
	`<rect width="160" height="160" fill="hsl(${doc.hue + chunk * 120} 60% 45%)"/>` +
	`<text x="80" y="88" font-size="16" text-anchor="middle" fill="white">${doc.doc_id} ${role}</text></svg>`;

/** The signed links this viewer handed out: the link names one cell and is its own permission. */
const signed = new Map<string, { doc: Doc; role: string; chunk: number; exp: number }>();
/** How long a person's links serve, in seconds: 300, as the viewer's default, unless a spec shortens its
 *  own (`/__ttl`) to see a link expire in real time without touching another spec's. */
const ttl = new Map<string, number>();

/** One chunk of a document: a row of the chunks table. */
interface Chunk {
	doc: Doc;
	chunk_id: number;
}

const CHUNKS: Chunk[] = DOCS.flatMap((doc) => doc.chunks.map((_, chunk_id) => ({ doc, chunk_id })));

/** A row of the chunks table, as the viewer and search answer it. A chunk past a document's own is one of
 *  the atlas's further points, a passage with no frame. */
const chunkRow = ({ doc, chunk_id }: Chunk, score?: number) => ({
	doc_id: doc.doc_id,
	chunk_id,
	title: doc.title,
	// Where in its document the chunk is, in the words a person reads: what names an item in a project's queue.
	caption: `${doc.title} · page ${chunk_id + 1}`,
	text: doc.chunks[chunk_id] ?? `Passage ${chunk_id} of ${doc.title.toLowerCase()}.`,
	collection: doc.collection,
	...(score === undefined ? {} : { _score: score }),
});

// ── the atlas ───────────────────────────────────────────────────────────────────────────────────

type Point = Chunk & { cluster: number; x: number; y: number };

/** The `umap` space's points: in `lake~acme$letters` sixty, every chunk first, then further passages, in
 *  three clusters; in `lake~acme$maps` twenty-four. */
const pointsOf = (dataset: string | null): Point[] =>
	dataset === 'lake~acme$maps' ? MAP_POINTS : POINTS;

const POINTS: Point[] = Array.from({ length: 60 }, (_, i) => {
	const chunk: Chunk = CHUNKS[i] ?? { doc: DOCS[i % DOCS.length]!, chunk_id: 100 + i };
	const cluster = i % 3;
	const angle = (i * 2.399) % (Math.PI * 2);
	const radius = 0.4 + ((i * 7) % 10) / 12;
	return {
		...chunk,
		cluster,
		x: [-3, 3, 0][cluster]! + Math.cos(angle) * radius,
		y: [-1, -1, 3][cluster]! + Math.sin(angle) * radius,
	};
});

const MAP_POINTS: Point[] = Array.from({ length: 24 }, (_, i) => ({
	doc: DOCS[2]!,
	chunk_id: 200 + i,
	cluster: i % 2,
	x: Math.cos(i / 2) * (2 + (i % 3)),
	y: Math.sin(i / 2) * (2 + (i % 3)),
}));

const pointsEtag = (dataset: string): string => `"points-${dataset.replace('$', '-')}-umap-v5"`;

/** The points as the viewer's `build_points` writes them: float16 `x`/`y`, the non-doc key fields, the
 *  stable `rowid`, the `doc` dictionary and the `cluster` codes, with `count`, `space` and `docFiles`. */
function pointsStream(points: Point[]): Uint8Array {
	const table = new Table({
		x: vectorFromArray(
			points.map((p) => p.x),
			new Float16(),
		),
		y: vectorFromArray(
			points.map((p) => p.y),
			new Float16(),
		),
		chunk_id: vectorFromArray(
			points.map((p) => p.chunk_id),
			new Int32(),
		),
		rowid: vectorFromArray(
			points.map((_, i) => BigInt(i)),
			new Int64(),
		),
		doc: vectorFromArray(
			points.map((p) => p.doc.doc_id),
			new Dictionary(new Utf8(), new Int32()),
		),
		cluster: vectorFromArray(
			points.map((p) => p.cluster),
			new Int32(),
		),
	});
	const docs = table.getChild('doc')?.data[0]?.dictionary?.toArray() as string[] | undefined;
	table.schema.metadata.set('count', String(points.length));
	table.schema.metadata.set('space', 'umap');
	table.schema.metadata.set(
		'docFiles',
		JSON.stringify((docs ?? []).map((id) => DOCS.find((d) => d.doc_id === id)?.title ?? id)),
	);
	return tableToIPC(table, 'stream');
}

// ── the knowledge graph and the topics ──────────────────────────────────────────────────────────

type Entity = { id: string; name: string; type: string; mentions: number; videos: number };
type Edge = { source: string; target: string; description: string };

const ENTITIES: Entity[] = [
	{ id: 'a000000000000001', name: 'The harbour master', type: 'PERSON', mentions: 3, videos: 2 },
	{ id: 'a000000000000002', name: 'The harbour', type: 'GEO', mentions: 4, videos: 2 },
	{ id: 'a000000000000003', name: 'The harvest', type: 'EVENT', mentions: 1, videos: 1 },
	{ id: 'a000000000000004', name: 'The fleet', type: 'ORG', mentions: 2, videos: 1 },
	{ id: 'a000000000000005', name: 'Soundings', type: 'CONCEPT', mentions: 1, videos: 1 },
];
const EDGES: Edge[] = [
	{ source: 'a000000000000001', target: 'a000000000000002', description: 'works at' },
	{ source: 'a000000000000004', target: 'a000000000000002', description: 'came into' },
	{ source: 'a000000000000005', target: 'a000000000000002', description: 'taken along' },
	{ source: 'a000000000000003', target: 'a000000000000001', description: 'counted by' },
];

const MAP_ENTITIES: Entity[] = [
	{ id: 'b000000000000001', name: 'The coast', type: 'GEO', mentions: 2, videos: 1 },
	{ id: 'b000000000000002', name: 'The lighthouse', type: 'WORK', mentions: 1, videos: 1 },
];
const MAP_EDGES: Edge[] = [
	{ source: 'b000000000000002', target: 'b000000000000001', description: 'stands on' },
];

/** A dataset's knowledge graph. */
const graphOf = (dataset: string | null): { entities: Entity[]; edges: Edge[] } =>
	dataset === 'lake~acme$maps'
		? { entities: MAP_ENTITIES, edges: MAP_EDGES }
		: { entities: ENTITIES, edges: EDGES };

const TOPICS = {
	name: 'All topics',
	children: [
		{
			name: 'Collections',
			children: [
				{ name: 'Letters', value: 3 },
				{ name: 'Maps', value: 1 },
			],
		},
		{ name: 'unclustered', value: 1 },
	],
};

/** Who asked a door to fail or to have nothing built, by person and path. */
/** What a spec plants on one path for one person: a failure, a dataset not built, a slow answer, or a session that
 *  ended (`unauthorized` on every call, `unauthorized-write` on a write alone). */
type Fault = 'fail' | 'unbuilt' | 'slow' | 'unauthorized' | 'unauthorized-write';
const FAULTS: readonly string[] = ['fail', 'unbuilt', 'slow', 'unauthorized', 'unauthorized-write'];
const faults = new Map<string, Map<string, Fault>>();
const faultOf = (sub: string, path: string): Fault | undefined => faults.get(sub)?.get(path);

function searchStream(hits: Chunk[]): Uint8Array {
	const rows = hits.map((hit, i) => chunkRow(hit, 1 / (i + 1)));
	const utf8 = (key: 'doc_id' | 'title' | 'text' | 'collection') =>
		vectorFromArray(
			rows.map((r) => r[key]),
			new Utf8(),
		);
	const table = new Table({
		doc_id: utf8('doc_id'),
		chunk_id: vectorFromArray(
			rows.map((r) => r.chunk_id),
			new Float64(),
		),
		title: utf8('title'),
		text: utf8('text'),
		collection: utf8('collection'),
		_score: vectorFromArray(
			rows.map((r) => r._score ?? 0),
			new Float64(),
		),
	});
	return tableToIPC(table, 'stream');
}

const arrow = (bytes: Uint8Array, headers: Record<string, string> = {}): Response =>
	new Response(new Blob([new Uint8Array(bytes)]), {
		headers: { 'content-type': 'application/vnd.apache.arrow.stream', ...headers },
	});

// ── the viewer ─────────────────────────────────────────────────────────────────────────────────

Bun.serve({
	port: MOCK_VIEWER_PORT,
	async fetch(req) {
		const url = new URL(req.url);
		if (url.pathname === '/__calls')
			return json(ledger.get(url.searchParams.get('sub') ?? '') ?? []);
		if (url.pathname === '/__fault' && req.method === 'POST') {
			const sub = url.searchParams.get('sub') ?? '';
			const path = url.searchParams.get('path') ?? '';
			const kind = url.searchParams.get('kind');
			const held = faults.get(sub) ?? new Map<string, Fault>();
			if (kind === 'clear') held.delete(path);
			else held.set(path, kind !== null && FAULTS.includes(kind) ? (kind as Fault) : 'fail');
			faults.set(sub, held);
			return json({ ok: true });
		}
		if (url.pathname === '/__lakehouse')
			return json(lakehouseCallsOf(url.searchParams.get('sub') ?? ''));
		if (url.pathname === '/__ttl' && req.method === 'POST') {
			ttl.set(url.searchParams.get('sub') ?? '', Number(url.searchParams.get('seconds') ?? 300));
			return json({ ok: true });
		}

		// The signed link: no token, its own permission.
		const link = /^\/api\/signed-media\/([\w-]+)$/.exec(url.pathname);
		const timedLink = link ? signedTimed.get(link[1] ?? '') : undefined;
		if (link && req.method === 'GET' && timedLink) {
			// The media's bytes, in the ranges the element asks for, as the viewer serves a cell.
			const media = TIMED_MEDIA[timedLink.doc]?.();
			if (!media) return json({ detail: 'no such media' }, 404);
			const range = /bytes=(\d+)-(\d*)/.exec(req.headers.get('range') ?? '');
			const headers = {
				'content-type': media.type,
				'accept-ranges': 'bytes',
				'cache-control': 'no-store',
			};
			if (!range) return new Response(Buffer.from(media.body), { headers });
			const from = Number(range[1]);
			const to = range[2]
				? Math.min(Number(range[2]), media.body.length - 1)
				: media.body.length - 1;
			return new Response(Buffer.from(media.body.subarray(from, to + 1)), {
				status: 206,
				headers: { ...headers, 'content-range': `bytes ${from}-${to}/${media.body.length}` },
			});
		}
		if (link && req.method === 'GET') {
			const held = signed.get(link[1] ?? '');
			if (!held) return json({ detail: 'the media URL is not this viewer’s signature' }, 403);
			if (Date.now() / 1000 > held.exp) return json({ detail: 'the media URL expired' }, 403);
			return new Response(picture(held.doc, held.role, held.chunk), {
				headers: {
					'content-type': 'image/svg+xml',
					'content-security-policy': "sandbox; default-src 'none'",
					'x-content-type-options': 'nosniff',
					'cache-control': 'public, max-age=300, immutable',
				},
			});
		}

		const who = await authenticate('viewer', req, url);
		if (who instanceof Response) return who;
		const ok = (status: number): void =>
			record(who.sub, {
				service: 'viewer',
				method: req.method,
				path: url.pathname,
				query: url.search,
				status,
			});

		if (url.pathname === '/api/datasets' && req.method === 'GET') {
			// Who signs in picks the answer: `Nobody` is shown no dataset, `Broken` meets a viewer that fails.
			if (who.name === 'Broken') {
				ok(503);
				return json({ detail: 'the catalog did not answer' }, 503);
			}
			if (!(await readAsPerson(who, who.token, who.host))) {
				ok(403);
				return json({ detail: 'the lakehouse refused the read' }, 403);
			}
			ok(200);
			const lake = {
				name: 'lake',
				kind: 'rask',
				title: 'The lakehouse',
				unlisted: [],
				truncated: false,
			};
			if (who.name === 'Nobody') return json({ sources: [{ ...lake, datasets: [] }] });
			const stamped = Object.entries(DATASETS).map(([id, d]) => ({
				id,
				source: 'lake',
				stamped: true,
				tables: Object.fromEntries(
					Object.entries(d.tables).map(([name, t]) => [
						name,
						{ row_count: t.row_count, version: t.version, n_columns: t.columns.length },
					]),
				),
				capabilities: d.capabilities,
				problem: null,
				unreadable: {},
				read_only: null,
			}));
			const plainSummary = {
				id: PLAIN_ID,
				source: 'archive',
				stamped: false,
				tables: {
					[PLAIN_TABLE]: {
						row_count: PLAIN_ROWS.length,
						version: 3,
						n_columns: PLAIN_COLUMNS.length,
					},
				},
				capabilities: [],
				problem: null,
				unreadable: { drafts: NOT_WRITTEN },
				read_only: READ_ONLY,
			};
			return json({
				sources: [
					{ ...lake, datasets: stamped },
					{
						name: 'archive',
						kind: 'namespace',
						title: 'Field archive',
						datasets: [plainSummary],
						unlisted: [
							{ id: 'archive~sealed', reason: 'catalog denied the request: no read on sealed' },
						],
						truncated: false,
					},
				],
			});
		}
		const seeded =
			req.method === 'GET' ? SEEDED.get(seededKey(url.pathname, url.searchParams)) : undefined;
		if (seeded) {
			ok(200);
			if ('bytes' in seeded) return json({ detail: 'the media is served by its signed link' }, 404);
			return 'arrow' in seeded
				? arrow(Uint8Array.from(atob(seeded.arrow), (c) => c.charCodeAt(0)))
				: json(seeded.json);
		}
		if (
			req.method === 'GET' &&
			SEEDED_DATASETS.some((id) =>
				url.pathname.startsWith(`/api/datasets/${encodeURIComponent(id)}`),
			)
		) {
			// A read of the demo's plain tables the recording does not hold: the spec asks what the viewer was never asked.
			ok(404);
			return json({ detail: `not recorded: ${url.pathname}${url.search}` }, 404);
		}
		const facts = /^\/api\/datasets\/([^/]+)$/.exec(url.pathname);
		if (facts && req.method === 'GET') {
			const id = decodeURIComponent(facts[1] ?? '');
			if (id === PLAIN_ID) {
				ok(200);
				return json(PLAIN_FACTS);
			}
			if (!isDataset(id)) {
				ok(404);
				return json({ detail: `unknown dataset ${id}` }, 404);
			}
			ok(200);
			return json({
				id,
				source: 'lake',
				kind: 'rask',
				stamped: true,
				tables: DATASETS[id].tables,
				capabilities: DATASETS[id].capabilities,
				problem: null,
				unreadable: {},
				read_only: null,
				setup_limits: { vector_max_dim: 2048 },
			});
		}
		const plainRows = /^\/api\/datasets\/([^/]+)\/tables\/([^/]+)\/rows(?:\/(\d+))?$/.exec(
			url.pathname,
		);
		if (plainRows && req.method === 'GET' && decodeURIComponent(plainRows[1] ?? '') === PLAIN_ID) {
			ok(200);
			if (plainRows[3] === undefined) return json(plainPage(url.searchParams));
			const row = PLAIN_ROWS[Number(plainRows[3])];
			return row ? json(row) : json({ detail: 'no such row' }, 404);
		}
		const derived =
			/^\/api\/datasets\/([^/]+)\/tables\/([^/]+)\/(profile|atlas|graph|tree)(?:\/(check|rows))?$/.exec(
				url.pathname,
			);
		if (derived && req.method === 'GET' && decodeURIComponent(derived[1] ?? '') === PLAIN_ID) {
			ok(200);
			const [, , , read, part] = derived;
			const params = url.searchParams;
			if (read === 'profile') return json(plainProfile());
			if (read === 'graph' && part === 'rows')
				return json(nodePage(plainGraphRows(params), params));
			if (read === 'tree' && part === 'rows') return json(nodePage(plainTreeRows(params), params));
			if (read === 'graph') {
				const graph = plainGraph(params);
				return json(part === 'check' ? graph.diagnostics : graph);
			}
			if (read === 'tree') {
				const tree = plainTree(params);
				return json(part === 'check' ? tree.diagnostics : tree);
			}
			return arrow(plainAtlas(params));
		}
		const descriptor = /^\/api\/datasets\/([^/]+)\/descriptor$/.exec(url.pathname);
		if (descriptor && req.method === 'GET') {
			const id = decodeURIComponent(descriptor[1] ?? '');
			if (!isDataset(id)) {
				ok(404);
				return json({ detail: `unknown dataset ${id}` }, 404);
			}
			ok(200);
			return json({ id, tables: DATASETS[id].tables, declared: DATASETS[id].declared });
		}
		if (url.pathname === '/api/signed-media' && req.method === 'POST') {
			if (faultOf(who.sub, url.pathname) === 'fail') {
				ok(503);
				return json({ detail: 'the viewer could not sign the media' }, 503);
			}
			const date = new Date().toUTCString();
			const body = (await req.json()) as {
				dataset: string;
				items: { role: string; doc_id: string; keys?: number[] }[];
			};
			ok(200);
			return json(
				{
					urls: body.items.map((item) => {
						if (item.role === 'media' && TIMED_MEDIA[item.doc_id]) {
							const token = crypto.randomUUID();
							const exp = Date.now() / 1000 + 300;
							signedTimed.set(token, { doc: item.doc_id, exp });
							return { url: `/api/signed-media/${token}`, expires_at: exp };
						}
						const doc = DOCS.find((d) => d.doc_id === item.doc_id);
						if (!doc) return { detail: 'doc_id not found' };
						if (item.role === 'frame' && doc.chunks[item.keys?.[0] ?? -1] === undefined) {
							return { detail: 'no frame for that chunk' };
						}
						const token = crypto.randomUUID();
						const exp = Date.now() / 1000 + (ttl.get(who.sub) ?? 300);
						signed.set(token, {
							doc,
							role: item.role,
							chunk: item.role === 'frame' ? (item.keys?.[0] ?? 0) : 0,
							exp,
						});
						return { url: `/api/signed-media/${token}`, expires_at: exp };
					}),
				},
				200,
				{ date },
			);
		}
		const dataset = url.searchParams.get('dataset');
		if (url.pathname === '/api/health' && req.method === 'GET') {
			ok(200);
			return json({
				db: {
					path: 's3://lakehouse/acme',
					tables: ['chunks'],
					chunks: CHUNKS.length,
					documents: DOCS.length,
				},
				embed: { ok: true, url: 'http://embed' },
				rerank: { ok: true, url: 'http://rerank' },
			});
		}
		if (url.pathname === '/api/documents' && req.method === 'GET') {
			// `Broken` meets a viewer whose documents read fails.
			if (who.name === 'Broken') {
				ok(503);
				return json({ detail: 'the documents table could not be opened' }, 503);
			}
			if (!isDataset(dataset)) {
				ok(404);
				return json({ detail: 'unknown dataset' }, 404);
			}
			const page = Number(url.searchParams.get('page') ?? 1);
			const perPage = Number(url.searchParams.get('per_page') ?? 24);
			const docs =
				dataset === 'lake~acme$letters'
					? DOCS.map(({ doc_id, title, collection, mime }) => ({ doc_id, title, collection, mime }))
					: [];
			ok(200);
			return json({
				total: docs.length,
				page,
				docs: docs.slice((page - 1) * perPage, page * perPage),
			});
		}
		if (url.pathname === '/api/rows' && req.method === 'GET') {
			if (dataset !== 'lake~acme$memos') {
				ok(404);
				return json(
					{ detail: `dataset ${JSON.stringify(dataset)} declares no row table to list` },
					404,
				);
			}
			const page = Number(url.searchParams.get('page') ?? 1);
			const perPage = Number(url.searchParams.get('per_page') ?? 20);
			ok(200);
			return json({
				total: MEMOS.length,
				page,
				page_size: perPage,
				items: MEMOS.slice((page - 1) * perPage, page * perPage).map((row) => ({
					...row,
					alignments: [],
				})),
			});
		}
		if (url.pathname === '/api/columns' && req.method === 'GET') {
			ok(200);
			return json([{ name: 'collection', type: 'string' }]);
		}
		// Voice tables are built for the letters only: the other dataset offers no voice search.
		if (url.pathname === '/api/voice/status' && req.method === 'GET') {
			ok(200);
			const built = dataset === 'lake~acme$letters';
			return json({ built, turns: built ? CHUNKS.length : 0, speakers: built ? 2 : 0 });
		}
		if (url.pathname === '/api/voice/similar' && req.method === 'GET') {
			const anchor = url.searchParams.get('doc_id');
			ok(200);
			return json({
				query: {
					doc_id: anchor,
					speaker_label: 'SPEAKER_00',
					turn_id: 0,
					turn_start: 0,
					turn_end: 4,
				},
				hits: CHUNKS.filter((c) => c.doc.doc_id !== anchor).map((c, i) => ({
					...chunkRow(c, 1 - i / 10),
					speaker_label: 'SPEAKER_00',
					turn_id: i + 1,
					turn_start: 0,
					turn_end: 4,
					_distance: i / 10,
					turn_score: 0.9 - i / 10,
				})),
			});
		}
		const docChunks = /^\/api\/doc-chunks\/([^/]+)$/.exec(url.pathname);
		if (docChunks && req.method === 'GET') {
			const doc = DOCS.find((d) => d.doc_id === decodeURIComponent(docChunks[1] ?? ''));
			if (!doc) {
				ok(404);
				return json({ detail: 'doc_id not found' }, 404);
			}
			ok(200);
			return json({
				doc_id: doc.doc_id,
				chunks: CHUNKS.filter((c) => c.doc === doc).map((c) => ({
					...chunkRow(c),
					alignments: [],
				})),
			});
		}
		const diarization = /^\/api\/diarization\/([^/]+)$/.exec(url.pathname);
		if (diarization && req.method === 'GET') {
			ok(200);
			return json({
				built: false,
				doc_id: decodeURIComponent(diarization[1] ?? ''),
				turns: [],
				speakers: [],
			});
		}
		const fault = faultOf(who.sub, url.pathname);
		if (fault === 'slow') await Bun.sleep(2500);
		if (fault === 'fail') {
			ok(500);
			return json({ detail: 'the viewer could not read the table' }, 500);
		}
		if (url.pathname === '/api/atlas/status' && req.method === 'GET') {
			ok(200);
			const built = fault !== 'unbuilt';
			return json({
				projected: built,
				rows: built ? pointsOf(dataset).length : 0,
				space: 'umap',
				spaces: { umap: built },
			});
		}
		// A task's rows by key, as the viewer's by-key door answers them: the rows found, unordered, with the key fields.
		if (url.pathname === '/api/atlas/chunks/by-key' && req.method === 'POST') {
			const { keys } = (await req.json()) as { keys: { doc_id: string; keys: number[] }[] };
			ok(200);
			if (dataset === 'lake~acme$memos')
				return json({
					rows: MEMOS.filter((m) =>
						keys.some((k) => k.doc_id === m.memo_id && k.keys[0] === m.line),
					).map((m) => ({ ...m, alignments: [] })),
					key_fields: ['memo_id', 'line'],
				});
			// A recorded dataset's row, as the viewer answered it for that unit (`seeded-answers.json`).
			const recorded = keys.map((k) =>
				SEEDED.get(
					seededKey(
						'/api/atlas/chunks/by-key',
						new URLSearchParams({ dataset: dataset ?? '', unit: [k.doc_id, ...k.keys].join('/') }),
					),
				),
			);
			const [first] = recorded;
			if (first && 'json' in first) return json(first.json);
			return json({
				rows: CHUNKS.filter((c) =>
					keys.some((k) => k.doc_id === c.doc.doc_id && k.keys[0] === c.chunk_id),
				).map((c) => chunkRow(c)),
				key_fields: ['doc_id', 'chunk_id'],
			});
		}
		if (url.pathname === '/api/atlas/chunks' && req.method === 'POST') {
			const { rowids } = (await req.json()) as { rowids: number[] };
			ok(200);
			return arrow(
				searchStream(
					rowids.flatMap((rowid) => {
						const point = pointsOf(dataset)[rowid];
						return point ? [point] : [];
					}),
				),
			);
		}
		if (url.pathname === '/api/graph/status' && req.method === 'GET') {
			ok(200);
			const built = fault !== 'unbuilt';
			return json({
				built,
				entities: built ? graphOf(dataset).entities.length : 0,
				relations: built ? graphOf(dataset).edges.length : 0,
				mentions: 11,
				videos: 3,
			});
		}
		if (url.pathname === '/api/graph/subgraph' && req.method === 'GET') {
			ok(200);
			const graph = graphOf(dataset);
			const center = url.searchParams.get('entity_id');
			const edges = center
				? graph.edges.filter((e) => e.source === center || e.target === center)
				: graph.edges;
			const ids = new Set(center ? [center, ...edges.flatMap((e) => [e.source, e.target])] : []);
			const nodes = graph.entities.filter((e) => !center || ids.has(e.id));
			return json({ built: true, nodes, edges });
		}
		const entity = /^\/api\/graph\/entity\/([0-9a-f]{16})$/.exec(url.pathname);
		if (entity && req.method === 'GET') {
			const graph = graphOf(dataset);
			const found = graph.entities.find((e) => e.id === entity[1]);
			ok(200);
			const linked = graph.edges.filter((e) => e.source === found?.id || e.target === found?.id);
			return json({
				built: true,
				entity: found
					? {
							entity_id: found.id,
							name: found.name,
							entity_type: found.type,
							mention_count: found.mentions,
						}
					: null,
				clips: found
					? CHUNKS.slice(0, 2).map(({ doc, chunk_id }) => ({
							chunk_id: `${doc.doc_id}/${chunk_id}`,
							doc_id: doc.doc_id,
							title: doc.title,
							start: 12 + chunk_id * 30,
							end: 42 + chunk_id * 30,
							text: doc.chunks[chunk_id] ?? '',
						}))
					: [],
				neighbors: linked.map((e) => {
					const out = e.source === found?.id;
					const other = graph.entities.find((n) => n.id === (out ? e.target : e.source));
					return {
						entity_id: other?.id ?? '',
						name: other?.name ?? '',
						entity_type: other?.type ?? 'OTHER',
						direction: out ? 'out' : 'in',
						description: e.description,
					};
				}),
				cooccur: [],
			});
		}
		if (url.pathname === '/api/graph/search' && req.method === 'GET') {
			ok(200);
			const q = (url.searchParams.get('q') ?? '').toLowerCase();
			return json({
				built: true,
				matches: graphOf(dataset)
					.entities.filter((e) => e.name.toLowerCase().includes(q))
					.map((e) => ({
						entity_id: e.id,
						name: e.name,
						entity_type: e.type,
						mention_count: e.mentions,
						videos: e.videos,
					})),
			});
		}
		if (url.pathname === '/api/graph/cypher' && req.method === 'POST') {
			const { query } = (await req.json()) as { query: string };
			ok(200);
			// The console answers a query it cannot run with its own error, as the viewer's does.
			if (!/^\s*MATCH\b/i.test(query)) {
				return json({ built: true, columns: [], rows: [], error: 'Cypher: expected MATCH' });
			}
			return json({
				built: true,
				columns: ['a.name', 'a.entity_type', 'a.mention_count'],
				rows: graphOf(dataset).entities.map((e) => [e.name, e.type, e.mentions]),
				error: null,
			});
		}
		if (url.pathname === '/api/topics' && req.method === 'GET') {
			ok(200);
			const built = fault !== 'unbuilt';
			return json({
				built,
				layers: 2,
				n_chunks: CHUNKS.length,
				hierarchy: built ? TOPICS : null,
				noise_label: 'unclustered',
			});
		}
		if (url.pathname === '/api/atlas/points' && req.method === 'GET') {
			if (
				(dataset !== 'lake~acme$letters' && dataset !== 'lake~acme$maps') ||
				fault === 'unbuilt'
			) {
				ok(400);
				return json({ detail: 'umap 2D projection not built yet' }, 400);
			}
			const etag = pointsEtag(dataset);
			const headers = { etag, 'cache-control': 'private, no-cache' };
			if (req.headers.get('if-none-match') === etag) {
				ok(304);
				return new Response(null, { status: 304, headers });
			}
			ok(200);
			return arrow(pointsStream(pointsOf(dataset)), headers);
		}
		ok(404);
		return json({ detail: `unstubbed ${req.method} ${url.pathname}` }, 404);
	},
});

// ── search ──────────────────────────────────────────────────────────────────────────────────────

Bun.serve({
	port: MOCK_SEARCH_PORT,
	async fetch(req) {
		const url = new URL(req.url);
		const who = await authenticate('search', req, url);
		if (who instanceof Response) return who;
		const ok = (status: number): void =>
			record(who.sub, { service: 'search', method: req.method, path: url.pathname, status });
		if (url.pathname === '/api/search' && req.method === 'GET') {
			if (!isDataset(url.searchParams.get('dataset'))) {
				ok(404);
				return json({ detail: 'unknown dataset' }, 404);
			}
			const q = (url.searchParams.get('q') ?? '').toLowerCase();
			// `fail` meets a search that fails; `repeated` answers several rows of one document, as a search over
			// a document's chunks does.
			if (q === 'fail') {
				ok(500);
				return json({ detail: 'the search index could not be read' }, 500);
			}
			ok(200);
			// Several rows of one document: its chunks.
			if (q === 'repeated') return arrow(searchStream(CHUNKS.slice(0, 3)));
			const n = Number(url.searchParams.get('n') ?? 30);
			const topic = url.searchParams.get('topic');
			// The one predicate shape the filter builder writes over the one filterable column.
			const where = /^collection (=|LIKE) '%?([^%']*)%?'$/.exec(
				url.searchParams.get('where') ?? '',
			);
			const kept = (c: Chunk): boolean =>
				!where ||
				(where[1] === '='
					? c.doc.collection === where[2]
					: c.doc.collection.toLowerCase().includes((where[2] ?? '').toLowerCase()));
			return arrow(
				searchStream(
					CHUNKS.filter(({ doc, chunk_id }) =>
						topic
							? doc.collection.toLowerCase() === topic.toLowerCase()
							: `${doc.title} ${doc.chunks[chunk_id]}`.toLowerCase().includes(q),
					)
						.filter(kept)
						.slice(0, n),
				),
			);
		}
		ok(404);
		return json({ detail: `unstubbed ${req.method} ${url.pathname}` }, 404);
	},
});

// ── the annotator ───────────────────────────────────────────────────────────────────────────────

Bun.serve({
	port: MOCK_ANNOTATOR_PORT,
	async fetch(req) {
		const url = new URL(req.url);
		const who = await authenticate('annotator', req, url);
		if (who instanceof Response) return who;
		const ok = (status: number): void =>
			record(who.sub, { service: 'annotator', method: req.method, path: url.pathname, status });
		const fault = faultOf(who.sub, url.pathname);
		if (fault === 'unauthorized' || (fault === 'unauthorized-write' && req.method !== 'GET')) {
			ok(401);
			return json({ detail: 'the token expired' }, 401, {
				'www-authenticate': 'DPoP error="invalid_token"',
			});
		}
		if (url.pathname === '/me' && req.method === 'GET') {
			registerName(who.sub, who.name);
			ok(200);
			return json({
				sub: who.sub,
				name: who.name,
				email: who.email,
				estate_admin: orgsOf(who.sub).some((o) => o.role === 'admin'),
				orgs: orgsOf(who.sub),
			});
		}
		const annotations = await annotationsDoor(
			req,
			url,
			who.sub,
			(projectId) => permissionsOn(who.sub, projectId),
			(taskId) => taskOf(who.sub, taskId),
			ok,
			faultOf(who.sub, url.pathname),
			{
				read: (taskId) => draftOf(who.sub, taskId),
				write: (taskId, draft) => writeDraft(who.sub, taskId, draft),
			},
		);
		if (annotations) return annotations;
		const assist = await assistDoor(req, url, (projectId) => permissionsOn(who.sub, projectId), ok);
		if (assist) return assist;
		const projects = await projectsDoor(
			req,
			url,
			who.sub,
			ok,
			fault === 'fail' || fault === 'unbuilt' || fault === 'slow' ? fault : undefined,
			who.name,
		);
		if (projects) return projects;
		ok(404);
		return json({ detail: `unstubbed ${req.method} ${url.pathname}` }, 404);
	},
});

console.log(
	`mock services: viewer :${MOCK_VIEWER_PORT}, search :${MOCK_SEARCH_PORT}, annotator :${MOCK_ANNOTATOR_PORT}`,
);
