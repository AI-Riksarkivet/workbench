// Seed-driven stand-ins for the workbench's three services, each on its own port behind the e2e edge,
// as a deployment's HTTPRoutes put them: the viewer (`/api/viewer`), search (`/api/search`) and the
// annotator (`/api/annotator`). Each answers its own paths below that base, as the edge rewrites it.
//
// EVERY CALL IS HELD TO THE SERVICES' RULE (`service_kit.governed.deps`): `Authorization: DPoP
// <token>`, one `DPoP` proof whose `htm` is the method and whose `htu` is the PUBLIC URL the browser
// called without its query (the edge passes the public base in `x-forwarded-prefix`, as a service is
// configured with `WORKBENCH_PUBLIC_URL`), whose `ath` hashes the token, and whose key is the one the
// token is bound to. A call that breaks any rule is refused 401 and recorded as refused. The one door
// that takes no token is the signed media link's GET: the link is the permission.
//
// `GET /api/viewer/__calls?sub=<sub>` answers what every service recorded for one person, so a spec
// can say each call it caused carried a valid proof.
import {
	Dictionary,
	Float16,
	Float64,
	Int32,
	Table,
	Utf8,
	tableToIPC,
	vectorFromArray,
} from 'apache-arrow';
import { athOf, readToken, verifyProof } from './dpop';
import { MOCK_ANNOTATOR_PORT, MOCK_SEARCH_PORT, MOCK_VIEWER_PORT } from './ports';

/** One call as a service saw it. */
export interface Call {
	service: string;
	method: string;
	path: string;
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

/** The person a call's token and proof prove, or the 401 that refuses it. */
async function authenticate(
	service: string,
	req: Request,
	url: URL,
): Promise<{ sub: string; name: string; email: string } | Response> {
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
	if (scheme !== 'DPoP') return refuse('the token is not presented with the DPoP scheme');
	if (!claims) return refuse('no token, or not one this issuer minted');
	if (claims.exp < Date.now() / 1000) return refuse('the token expired');
	if (req.headers.get('dpop')?.includes(',')) return refuse('more than one DPoP proof');
	try {
		const proof = await verifyProof(req.headers.get('dpop'), req.method, htu);
		if (proof.ath !== athOf(token)) return refuse('ath is not the hash of the access token');
		if (proof.jkt !== claims.cnf.jkt) return refuse('the token is not bound to the proof key');
	} catch (err) {
		return refuse(`invalid_dpop_proof: ${err instanceof Error ? err.message : String(err)}`);
	}
	return claims;
}

// ── the seed ────────────────────────────────────────────────────────────────────────────────────

interface Doc {
	doc_id: string;
	title: string;
	mime: string;
	hue: number;
}

const DOCS: Doc[] = [
	{ doc_id: 'letter-001', title: 'A letter from the harbour', mime: 'image/svg+xml', hue: 20 },
	{ doc_id: 'letter-002', title: 'A letter about the harvest', mime: 'image/svg+xml', hue: 140 },
	{ doc_id: 'map-003', title: 'A map of the harbour', mime: 'image/svg+xml', hue: 260 },
];

/** Two datasets: one with documents, thumbnails and an atlas; one with none of them. */
const DATASETS = {
	acme$letters: {
		tables: { documents: { row_count: DOCS.length, version: 3, n_columns: 5 } },
		capabilities: ['search', 'atlas'],
		declared: {
			identity: { doc_key: 'doc_id', key_fields: ['doc_id'] },
			document: { table: 'documents', media_blob: 'media', mime: 'mime', thumbnail: 'thumb' },
			atlas: [
				{ name: 'umap', x: 'x', y: 'y', cluster: 'topic', source_column: 'v', table: 'documents' },
			],
			search: { table: 'documents' },
		},
	},
	acme$notes: {
		tables: { notes: { row_count: 0, version: 1, n_columns: 2 } },
		capabilities: [],
		declared: { identity: { doc_key: 'note_id', key_fields: ['note_id'] }, atlas: [] },
	},
} as const;

type DatasetId = keyof typeof DATASETS;
const isDataset = (id: string | null): id is DatasetId => id !== null && id in DATASETS;

/** A small picture per document, so a thumbnail and its media are visibly that document's. */
const picture = (doc: Doc, role: string): string =>
	`<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160" viewBox="0 0 160 160">` +
	`<rect width="160" height="160" fill="hsl(${doc.hue} 60% 45%)"/>` +
	`<text x="80" y="88" font-size="16" text-anchor="middle" fill="white">${doc.doc_id} ${role}</text></svg>`;

/** The signed links this viewer handed out: the link names one cell and is its own permission. */
const signed = new Map<string, { doc: Doc; role: string }>();

const POINTS_ETAG = '"points-acme-letters-umap-v3"';

function pointsStream(): Uint8Array {
	const n = 60;
	const x = Array.from({ length: n }, (_, i) => Math.cos(i / 3) * (1 + (i % 7) / 4));
	const y = Array.from({ length: n }, (_, i) => Math.sin(i / 3) * (1 + (i % 5) / 3));
	const cluster = Array.from({ length: n }, (_, i) => ['harbour', 'harvest', 'maps'][i % 3] ?? '');
	const table = new Table({
		x: vectorFromArray(x, new Float16()),
		y: vectorFromArray(y, new Float16()),
		cluster: vectorFromArray(cluster, new Dictionary(new Utf8(), new Int32())),
	});
	return tableToIPC(table, 'stream');
}

function searchStream(hits: Doc[]): Uint8Array {
	const table = new Table({
		doc_id: vectorFromArray(
			hits.map((d) => d.doc_id),
			new Utf8(),
		),
		title: vectorFromArray(
			hits.map((d) => d.title),
			new Utf8(),
		),
		mime: vectorFromArray(
			hits.map((d) => d.mime),
			new Utf8(),
		),
		_score: vectorFromArray(
			hits.map((_, i) => 1 / (i + 1)),
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

		// The signed link: no token, its own permission.
		const link = /^\/api\/signed-media\/([\w-]+)$/.exec(url.pathname);
		if (link && req.method === 'GET') {
			const held = signed.get(link[1] ?? '');
			if (!held) return json({ detail: 'the media URL is not this viewer’s signature' }, 403);
			return new Response(picture(held.doc, held.role), {
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
			record(who.sub, { service: 'viewer', method: req.method, path: url.pathname, status });

		if (url.pathname === '/api/datasets' && req.method === 'GET') {
			// Who signs in picks the answer: `Nobody` is shown no dataset, `Broken` meets a viewer that fails.
			if (who.name === 'Broken') {
				ok(503);
				return json({ detail: 'the catalog did not answer' }, 503);
			}
			ok(200);
			if (who.name === 'Nobody') return json({ datasets: [] });
			return json({
				datasets: Object.entries(DATASETS).map(([id, d]) => ({
					id,
					tables: d.tables,
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
			const body = (await req.json()) as {
				dataset: string;
				items: { role: string; doc_id: string }[];
			};
			ok(200);
			return json({
				urls: body.items.map((item) => {
					const doc = DOCS.find((d) => d.doc_id === item.doc_id);
					if (!doc) return { detail: 'doc_id not found' };
					const token = crypto.randomUUID();
					signed.set(token, { doc, role: item.role });
					return {
						url: `/api/signed-media/${token}`,
						expires_at: Math.floor(Date.now() / 1000) + 300,
					};
				}),
			});
		}
		if (url.pathname === '/api/atlas/points' && req.method === 'GET') {
			if (url.searchParams.get('dataset') !== 'acme$letters') {
				ok(400);
				return json({ detail: 'no atlas spaces declared for dataset' }, 400);
			}
			const headers = { etag: POINTS_ETAG, 'cache-control': 'private, no-cache' };
			if (req.headers.get('if-none-match') === POINTS_ETAG) {
				ok(304);
				return new Response(null, { status: 304, headers });
			}
			ok(200);
			return arrow(pointsStream(), headers);
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
			if (q === 'repeated') return arrow(searchStream([DOCS[0]!, DOCS[0]!, DOCS[1]!]));
			return arrow(searchStream(DOCS.filter((d) => d.title.toLowerCase().includes(q))));
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
		if (url.pathname === '/me' && req.method === 'GET') {
			ok(200);
			return json({
				sub: who.sub,
				name: who.name,
				email: who.email,
				estate_admin: false,
				projects: [{ project: 'acme', role: 'member' }],
			});
		}
		ok(404);
		return json({ detail: `unstubbed ${req.method} ${url.pathname}` }, 404);
	},
});

console.log(
	`mock services: viewer :${MOCK_VIEWER_PORT}, search :${MOCK_SEARCH_PORT}, annotator :${MOCK_ANNOTATOR_PORT}`,
);
