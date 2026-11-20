// The annotator's annotations door as the e2e mock serves it: a task's work is its draft (`mock-projects.ts`), read as
// Arrow IPC rows with the draft's revision in `X-Annotations-Version`, changed by one review delta that names the
// revision it was built from (a moved one is a 409), with its kept saves as its history. Its proposals are kept or
// removed all at once by `/proposals`, and its assist doors answer as the annotator does in front of the stand-in model
// endpoint (`annotator.model_endpoint.stand_in`).
//
// It refuses what the service's door refuses (`annotator.annotations.access`, `annotator.annotations.unit`), in its
// words: a key whose parts are not the dataset's identity (422), a project the caller may not view (404), a task that is
// not that project's (404), a unit that is not the task's (404), and a save the task takes no edit for (409 for its
// state, 403 for the caller).
import { mayReadWork, type Draft, type Row, type Task } from './mock-projects';
import {
	Field,
	Float32,
	Int32,
	List,
	Table,
	Utf8,
	tableToIPC,
	vectorFromArray,
	type DataType,
} from 'apache-arrow';

const COLUMNS: [string, DataType][] = [
	['id', new Utf8()],
	['shape_type', new Utf8()],
	['x', new Float32()],
	['y', new Float32()],
	['width', new Float32()],
	['height', new Float32()],
	['rotation', new Float32()],
	['polygon', new List(new Field('item', new Float32(), true))],
	['t_start', new Float32()],
	['t_end', new Float32()],
	['text', new Utf8()],
	['parent_id', new Utf8()],
	['char_start', new Int32()],
	['char_end', new Int32()],
	['label', new Utf8()],
	['status', new Utf8()],
	['source', new Utf8()],
	['reviewer', new Utf8()],
	['confidence', new Float32()],
	['uncertainty', new Float32()],
	['model_version', new Utf8()],
	['group', new Utf8()],
	['metadata', new Utf8()],
	['links', new Utf8()],
];

/** The rows the canvas reads, each with the links leaving it as its `links` cell (`[{"name", "to_shape"}]`). */
function ipc(rows: Row[], links: Draft['links']): Uint8Array {
	const leaving = (id: string): string =>
		JSON.stringify(
			links.filter((l) => l.from_shape === id).map((l) => ({ name: l.name, to_shape: l.to_shape })),
		);
	const columns = Object.fromEntries(
		COLUMNS.map(([name, type]) => [
			name,
			vectorFromArray(
				rows.map((r) => (name === 'links' ? leaving(r.id) : (r[name] ?? null))),
				type,
			),
		]),
	);
	return tableToIPC(new Table(columns), 'stream');
}

/** Each mock dataset's identity, as its descriptor declares it (`mock-services.ts`): a doc key and an integer chunk. */
const IDENTITY: Record<string, string[]> = {
	'lake~acme$letters': ['doc_id', 'chunk_id'],
	'lake~acme$maps': ['doc_id', 'chunk_id'],
	// The demo's recordings and films, as `scripts/seed_demo_corpus.py` declares them.
	'lake~demo$recordings': ['doc_id', 'speech_id', 'chunk_id'],
	'lake~demo$films': ['doc_id', 'speech_id', 'chunk_id'],
};

/** The service's refusal of a key its dataset's identity cannot name (`annotator.annotations.unit.parse_unit`). */
function keyRefusal(dataset: string, key: string): string | null {
	const fields = IDENTITY[dataset] ?? [];
	const parts = key.split('/');
	if (parts.length !== fields.length)
		return `unit key '${key}' has ${parts.length} part(s), and this dataset names a unit by ${fields.length}: ${fields.join('/')}`;
	for (const [i, part] of parts.slice(1).entries())
		if (!/^-?\d{1,19}$/.test(part))
			return `unit key '${key}': ${fields[i + 1]} is an integer, not '${part}'`;
	return null;
}

const json = (body: unknown, status = 200): Response =>
	new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

interface SaveBody {
	edits: ({ id: string } & Record<string, string>)[];
	inserts: Row[];
	geometry: {
		id: string;
		x: number;
		y: number;
		width: number;
		height: number;
		polygon: number[];
	}[];
	spans: { id: string; char_start: number; char_end: number }[];
	temporal: { id: string; t_start: number; t_end: number }[];
	deletes: string[];
	links?: Draft['links'];
	base_version: number | null;
}

/** Answer one call to `/api/annotations/…`, or null when the path is not that door. `perms` is what the caller holds
 *  on a project; `record` writes the call's status into the ledger; `fault` is the spec's switch for this path; `work`
 *  reads and writes a task's draft, the one store of its work in progress, which the task door serves too. Nothing here
 *  reaches the lakehouse: a save is the annotator's own until a publish. */
export async function annotationsDoor(
	req: Request,
	url: URL,
	sub: string,
	perms: (projectId: string) => string[],
	taskOf: (taskId: string) => Task | undefined,
	record: (status: number) => void,
	fault: string | undefined,
	work: {
		read: (taskId: string) => Draft | undefined;
		write: (taskId: string, draft: Draft) => void;
	},
): Promise<Response | null> {
	const match = /^\/api\/annotations\/(.+?)(\/versions|\/revert|\/proposals)?$/.exec(url.pathname);
	if (!match) return null;
	const answer = (body: unknown, status = 200): Response => {
		record(status);
		return json(body, status);
	};
	if (fault === 'fail')
		return answer({ detail: 'the annotator could not read the task’s work' }, 503);
	const project = url.searchParams.get('project');
	const taskId = url.searchParams.get('task');
	const dataset = url.searchParams.get('dataset') ?? '';
	if (!project || !taskId) return answer({ detail: 'project and task: Field required' }, 422);
	const held = perms(project);
	if (!held.includes('can_view'))
		return answer({ detail: `No project ${project} that you can see` }, 404);
	const task = taskOf(taskId);
	if (!task || task.project_id !== project)
		return answer({ detail: `No task ${taskId} that you can see` }, 404);
	const key = decodeURIComponent(match[1] ?? '');
	// A task's draft holds its item's first unit (`annotator.annotations.access.work_unit`).
	if (task.source.where !== dataset || task.source.keys[0] !== key)
		return answer({ detail: `unit ${key} of dataset ${dataset} is not this item's` }, 404);
	if (req.method === 'GET' && !mayReadWork(task, sub, held))
		return answer(
			{
				detail:
					"you may not read this replica's work: only its annotator, or someone who may review it",
			},
			403,
		);
	if (req.method !== 'GET') {
		const edits: Record<string, string> = { claimed: 'can_annotate', in_review: 'can_review' };
		const relation = edits[task.state];
		if (!relation)
			return answer(
				{
					detail: `this item is ${task.state} — its shapes take edits only while it is claimed or in review`,
				},
				409,
			);
		if (!held.includes(relation))
			return answer({ detail: `you lack ${relation} on this item` }, 403);
		if (task.state === 'claimed' && task.assignee !== sub)
			return answer({ detail: 'another person holds this item' }, 403);
		if (task.state === 'in_review' && task.submitted_by === sub)
			return answer({ detail: 'a reviewer may not edit their own submission' }, 403);
	}
	const refused = keyRefusal(dataset, key);
	if (refused) return answer({ detail: refused }, 422);
	const draft = work.read(taskId);
	const version = draft?.revision ?? 0;

	if (match[2] && req.method === 'GET') {
		return answer(
			(draft?.history ?? []).toReversed().map((h) => ({
				version: h.version,
				timestamp: h.timestamp,
				author: h.author,
				count: h.rows.length,
			})),
		);
	}
	if (req.method === 'GET') {
		const at = url.searchParams.get('version');
		const past = at === null ? null : draft?.history.find((h) => h.version === Number(at));
		if (at !== null && !past)
			return answer(
				{
					detail: `annotations version ${at} not found: the history of task ${taskId} does not keep it`,
				},
				404,
			);
		record(200);
		return new Response(
			new Blob([
				new Uint8Array(
					ipc(past ? past.rows : (draft?.rows ?? []), past ? [] : (draft?.links ?? [])),
				),
			]),
			{
				headers: {
					'content-type': 'application/vnd.apache.arrow.stream',
					'x-annotations-version': String(past ? past.version : version),
					'cache-control': 'no-store',
				},
			},
		);
	}
	if (req.method !== 'POST') return null;
	if (match[2] === '/proposals') {
		// Every proposal of the work kept (the caller's now, still naming its model) or removed with its links, as one
		// revision built on the one the caller read (`annotations.proposals`).
		const { action, base_version } = (await req.json()) as { action: string; base_version: number };
		if (action !== 'keep' && action !== 'remove')
			return answer({ detail: "action: Input should be 'keep' or 'remove'" }, 422);
		if (base_version !== version)
			return answer(
				{ detail: `annotations changed on the server (loaded v${base_version}, now v${version})` },
				409,
			);
		const held = draft?.rows ?? [];
		const proposed = new Set(held.filter((r) => r.status === 'prediction').map((r) => r.id));
		const rows =
			action === 'remove'
				? held.filter((r) => !proposed.has(r.id))
				: held.map((r) => (proposed.has(r.id) ? { ...r, status: 'accepted', reviewer: sub } : r));
		const links = (draft?.links ?? []).filter(
			(l) => action === 'keep' || (!proposed.has(l.from_shape) && !proposed.has(l.to_shape)),
		);
		if (proposed.size === 0) return answer({ settled: 0, version });
		const next = version + 1;
		work.write(taskId, {
			rows,
			links,
			revision: next,
			history: [
				...(draft?.history ?? []),
				{
					version: next,
					timestamp: new Date().toISOString(),
					author: sub.replace(/^user:/, ''),
					rows,
					links,
				},
			].slice(-50),
		});
		return answer({ settled: proposed.size, version: next });
	}
	if (match[2] === '/revert') {
		// The annotator's undo of one save (`annotations.revert`): the work as the save before it left it, rows and links
		// whole, while the work is still at the revision that save wrote; written as a new revision of its own.
		const { revision } = (await req.json()) as { revision: number };
		if (revision !== version)
			return answer(
				{ detail: `annotations changed on the server (loaded v${revision}, now v${version})` },
				409,
			);
		const prior =
			revision === 1
				? { rows: [], links: [] }
				: draft?.history.find((h) => h.version === revision - 1);
		if (!prior)
			return answer(
				{
					detail: `the history of this item's work no longer keeps the save that wrote v${revision}`,
				},
				409,
			);
		const rows = structuredClone(prior.rows);
		const links = structuredClone(prior.links ?? []);
		const next = version + 1;
		work.write(taskId, {
			rows,
			links,
			revision: next,
			history: [
				...(draft?.history ?? []),
				{
					version: next,
					timestamp: new Date().toISOString(),
					author: sub.replace(/^user:/, ''),
					rows,
					links,
				},
			].slice(-50),
		});
		return answer({ saved: 1, version: next });
	}
	const b = (await req.json()) as SaveBody;
	// A row's status is one of four; a model's row names its version and score whatever its status, only a model's
	// row is a proposal, and only a model's row names a version (`schema.NewAnnotation`, `schema.EditAnnotation`).
	const STATUSES = ['accepted', 'reviewed', 'rejected', 'prediction'];
	for (const row of b.inserts) {
		const fromModel = typeof row.source === 'string' && row.source.startsWith('model:');
		const refusal =
			row.status !== undefined && !STATUSES.includes(String(row.status))
				? `inserts: status: Input should be ${STATUSES.map((v) => `'${v}'`).join(', ')}`
				: row.status === 'prediction' && !fromModel
					? 'inserts: a proposal names the model that made it (source model:<name>)'
					: fromModel &&
						  !(
								typeof row.model_version === 'string' &&
								row.model_version !== '' &&
								typeof row.confidence === 'number'
						  )
						? "inserts: a model's row names the model's version and its score"
						: row.model_version != null && !fromModel
							? 'inserts: only a model’s row names a model version'
							: null;
		if (refusal) return answer({ detail: refusal }, 422);
	}
	const rowsById = new Map([...(draft?.rows ?? []), ...b.inserts].map((r) => [r.id, r]));
	for (const edit of b.edits) {
		if (edit.status === undefined) continue;
		if (!STATUSES.includes(edit.status))
			return answer(
				{ detail: `edits: status: Input should be ${STATUSES.map((v) => `'${v}'`).join(', ')}` },
				422,
			);
		const source = rowsById.get(edit.id)?.source;
		if (
			edit.status === 'prediction' &&
			!(typeof source === 'string' && source.startsWith('model:'))
		)
			return answer(
				{ detail: `annotation ${edit.id} is a person's: only a model's shape is a proposal` },
				400,
			);
	}
	if (b.base_version === null || b.base_version === undefined)
		return answer(
			{
				detail: `this save did not state the version it was built from; the work is at v${version}. Re-read it and send that version as base_version.`,
			},
			409,
		);
	if (b.base_version !== version)
		return answer(
			{ detail: `annotations changed on the server (loaded v${b.base_version}, now v${version})` },
			409,
		);
	// New shapes first: a shape drawn and changed before the save arrives as its insert and its changes, by its new id.
	const current = [...(draft?.rows ?? []), ...b.inserts.map((r) => ({ ...r, reviewer: sub }))];
	const ids = new Set(current.map((r) => r.id));
	const named = [
		...b.edits.map((e) => e.id),
		...b.geometry.map((g) => g.id),
		...b.spans.map((s) => s.id),
		...b.temporal.map((t) => t.id),
		...b.deletes,
	].filter((id) => !ids.has(id));
	if (named.length > 0)
		return answer(
			{
				detail: `annotation(s) ${JSON.stringify([...new Set(named)].sort())} are not this task's rows of its unit`,
			},
			404,
		);
	const gone = new Set(b.deletes);
	const rows = current
		.filter((r) => !gone.has(r.id))
		.map((r) => {
			const edit = b.edits.find((e) => e.id === r.id);
			const geo = b.geometry.find((g) => g.id === r.id);
			const span = b.spans.find((s) => s.id === r.id);
			const time = b.temporal.find((t) => t.id === r.id);
			const touched = edit || geo || span || time;
			return { ...r, ...edit, ...geo, ...span, ...time, ...(touched ? { reviewer: sub } : {}) };
		});
	const links =
		b.links ?? (draft?.links ?? []).filter((l) => !gone.has(l.from_shape) && !gone.has(l.to_shape));
	const next = version + 1;
	work.write(taskId, {
		rows,
		links,
		revision: next,
		history: [
			...(draft?.history ?? []),
			{
				version: next,
				timestamp: new Date().toISOString(),
				author: sub.replace(/^user:/, ''),
				rows,
				links,
			},
		].slice(-50),
	});
	return answer({ saved: b.inserts.length + b.edits.length + gone.size, version: next });
}

/** The stand-in endpoint's models, as the annotator lists them (`annotator.model_endpoint.stand_in`). */
const VERSION = 'stand-in.1';
const MODELS = [
	{
		name: 'stand-in-detector',
		version: VERSION,
		title: 'Stand-in detector',
		reads: ['image'],
		prompts: ['text', 'box'],
		returns: ['bbox'],
		compatible: null,
	},
	{
		name: 'stand-in-segmenter',
		version: VERSION,
		title: 'Stand-in segmenter',
		reads: ['image'],
		prompts: ['box', 'points'],
		returns: ['polygon'],
		compatible: null,
	},
	{
		name: 'stand-in-slow',
		version: VERSION,
		title: 'Stand-in that never answers',
		reads: ['image'],
		prompts: ['text', 'box'],
		returns: ['bbox'],
		compatible: null,
	},
	{
		name: 'stand-in-failing',
		version: VERSION,
		title: 'Stand-in that fails',
		reads: ['image'],
		prompts: ['text', 'box'],
		returns: ['bbox'],
		compatible: null,
	},
];

/** What the annotator says when the deployment names no model endpoint (`model_endpoint.client.NO_ENDPOINT`). */
const NO_ENDPOINT = 'assist is unavailable: this deployment names no model endpoint';

interface AssistBody {
	model: string;
	prompt: string | null;
	region: { x: number; y: number; width: number; height: number } | null;
	points: { x: number; y: number; positive: boolean }[];
	task_id: string;
}
const ASK_FIELDS = new Set(['model', 'prompt', 'region', 'points', 'task_id']);

/** The stand-in's answer, proposal for proposal: the detector's box where the person drew one (else a fixed box), the
 *  segmenter's diamond over its positive clicks' box padded by half a 120-pixel patch (or over the drawn box), its score
 *  rising with each click; each labelled with the task's first class (`pages`' `figure`). */
function proposals(body: AssistBody): Record<string, unknown>[] {
	const label = 'figure';
	if (body.model === 'stand-in-segmenter') {
		let box = body.region ?? { x: 0, y: 0, width: 0, height: 0 };
		if (body.points.length > 0) {
			const anchors = body.points.filter((p) => p.positive);
			const at = anchors.length > 0 ? anchors : body.points;
			const xs = at.map((p) => p.x);
			const ys = at.map((p) => p.y);
			box = {
				x: Math.min(...xs) - 60,
				y: Math.min(...ys) - 60,
				width: Math.max(...xs) - Math.min(...xs) + 120,
				height: Math.max(...ys) - Math.min(...ys) + 120,
			};
		}
		const { x, y, width: w, height: h } = box;
		return [
			{
				shape_type: 'polygon',
				x,
				y,
				width: w,
				height: h,
				polygon: [x + w / 2, y, x + w, y + h / 2, x + w / 2, y + h, x, y + h / 2],
				label,
				text: '',
				score: Math.min(0.95, 0.75 + 0.05 * body.points.length),
			},
		];
	}
	const r = body.region ?? { x: 100, y: 100, width: 200, height: 80 };
	return [{ shape_type: 'bbox', ...r, polygon: [], label, text: '', score: 0.7 }];
}

const problem = (type: string, title: string, status: number, detail: string) => ({
	type: `about:blank#${type}`,
	title,
	status,
	detail,
});

/** Answer one call to `/api/assist/…`, or null when the path is not that door. Its gate is the service's: the
 *  annotating role on the project the request names (`assist.require_assist`). ``endpoint`` false is a deployment that
 *  names no model endpoint (the spec's `no-endpoint` fault on `/api/assist/models`). The slow model is the annotator's
 *  timeout and the failing one its 502, answered at once: what the canvas meets, without the wait. */
export async function assistDoor(
	req: Request,
	url: URL,
	perms: (projectId: string) => string[],
	record: (status: number) => void,
	endpoint: boolean,
): Promise<Response | null> {
	if (!url.pathname.startsWith('/api/assist/')) return null;
	const answer = (body: unknown, status = 200): Response => {
		record(status);
		return new Response(JSON.stringify(body), {
			status,
			headers: { 'content-type': status < 400 ? 'application/json' : 'application/problem+json' },
		});
	};
	const project = url.searchParams.get('project') ?? '';
	if (!perms(project).includes('can_annotate'))
		return answer({ detail: 'you lack can_annotate on this project' }, 403);
	if (url.pathname === '/api/assist/models' && req.method === 'GET')
		return answer(
			endpoint
				? { available: true, reason: '', models: MODELS }
				: { available: false, reason: NO_ENDPOINT, models: [] },
		);
	if (req.method !== 'POST') return null;
	const body = (await req.json()) as AssistBody;
	const extra = Object.keys(body).filter((k) => !ASK_FIELDS.has(k));
	if (extra.length > 0 || typeof body.model !== 'string' || typeof body.task_id !== 'string')
		return answer(
			problem(
				'validation',
				'Unprocessable Content',
				422,
				`the ask is not one: ${extra.join(', ')}`,
			),
			422,
		);
	if (!endpoint)
		return answer(problem('no-model-endpoint', 'Service Unavailable', 503, NO_ENDPOINT), 503);
	if (body.model === 'stand-in-slow')
		return answer(
			problem(
				'model-endpoint-timeout',
				'Gateway Timeout',
				504,
				'the model endpoint did not answer within 30 s',
			),
			504,
		);
	if (body.model === 'stand-in-failing')
		return answer(
			problem(
				'model-endpoint-failed',
				'Bad Gateway',
				502,
				'the model endpoint failed (500): stand-in-failing fails every ask',
			),
			502,
		);
	if (!MODELS.some((m) => m.name === body.model))
		return answer(
			problem('not-found', 'Not Found', 404, 'the model endpoint serves no such model'),
			404,
		);
	return answer({ model: body.model, version: VERSION, proposals: proposals(body), dropped: [] });
}
