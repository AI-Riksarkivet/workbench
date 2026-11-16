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
import {
	Dictionary,
	Float16,
	Float64,
	Int32,
	Int64,
	Table,
	Utf8,
	tableToIPC,
	vectorFromArray,
} from 'apache-arrow';
import { athOf, readToken, verifyProof, type TokenClaims } from './dpop';
import { lakehouseCallsOf, readAsPerson, writeAsService } from './mock-lakehouse';
import { SHAPES, shapeOf } from './providers';
import { annotationsDoor } from './mock-annotations';
import { orgsOf, permissionsOn, projectsDoor, taskOf } from './mock-projects';
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

/** Two datasets: one with documents, chunks, thumbnails and an atlas; one with none of them. */
const DATASETS = {
	acme$letters: {
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
	acme$maps: {
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
	acme$notes: {
		tables: { notes: table('notes', 0, 1, ['note_id', 'text']) },
		capabilities: [],
		declared: { identity: { doc_key: 'note_id', key_fields: ['note_id'] }, atlas: [] },
	},
} as const;

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
	text: doc.chunks[chunk_id] ?? `Passage ${chunk_id} of ${doc.title.toLowerCase()}.`,
	collection: doc.collection,
	...(score === undefined ? {} : { _score: score }),
});

// ── the atlas ───────────────────────────────────────────────────────────────────────────────────

type Point = Chunk & { cluster: number; x: number; y: number };

/** The `umap` space's points: in `acme$letters` sixty, every chunk first, then further passages, in
 *  three clusters; in `acme$maps` twenty-four. */
const pointsOf = (dataset: string | null): Point[] =>
	dataset === 'acme$maps' ? MAP_POINTS : POINTS;

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
	dataset === 'acme$maps'
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
			if (who.name === 'Nobody') return json({ datasets: [] });
			return json({
				datasets: Object.entries(DATASETS).map(([id, d]) => ({
					id,
					tables: Object.fromEntries(
						Object.entries(d.tables).map(([name, t]) => [
							name,
							{ row_count: t.row_count, version: t.version, n_columns: t.columns.length },
						]),
					),
					capabilities: d.capabilities,
				})),
			});
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
				dataset === 'acme$letters'
					? DOCS.map(({ doc_id, title, collection, mime }) => ({ doc_id, title, collection, mime }))
					: [];
			ok(200);
			return json({
				total: docs.length,
				page,
				docs: docs.slice((page - 1) * perPage, page * perPage),
			});
		}
		if (url.pathname === '/api/columns' && req.method === 'GET') {
			ok(200);
			return json([{ name: 'collection', type: 'string' }]);
		}
		// Voice tables are built for the letters only: the other dataset offers no voice search.
		if (url.pathname === '/api/voice/status' && req.method === 'GET') {
			ok(200);
			const built = dataset === 'acme$letters';
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
			if ((dataset !== 'acme$letters' && dataset !== 'acme$maps') || fault === 'unbuilt') {
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
			() => writeAsService(who, who.host),
		);
		if (annotations) return annotations;
		const projects = await projectsDoor(
			req,
			url,
			who.sub,
			ok,
			fault === 'fail' || fault === 'unbuilt' || fault === 'slow' ? fault : undefined,
		);
		if (projects) return projects;
		ok(404);
		return json({ detail: `unstubbed ${req.method} ${url.pathname}` }, 404);
	},
});

console.log(
	`mock services: viewer :${MOCK_VIEWER_PORT}, search :${MOCK_SEARCH_PORT}, annotator :${MOCK_ANNOTATOR_PORT}`,
);
