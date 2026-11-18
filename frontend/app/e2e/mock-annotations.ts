// The annotator's annotations door as the e2e mock serves it: a task's work is its draft (`mock-projects.ts`), read as
// Arrow IPC rows with the draft's revision in `X-Annotations-Version`, changed by one review delta that names the
// revision it was built from (a moved one is a 409), with its kept saves as its history.
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
	const match = /^\/api\/annotations\/(.+?)(\/versions)?$/.exec(url.pathname);
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
	const b = (await req.json()) as SaveBody;
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
			},
		].slice(-50),
	});
	return answer({ saved: b.inserts.length + b.edits.length + gone.size, version: next });
}

/** The models the annotator's mock answers for (`annotator.api.v1.endpoints.assist`): the detector and the segmenter,
 *  interactive, no server configured; a batch family listed as such. */
const PRODUCERS = [
	{
		name: 'grounding-dino',
		configured: false,
		returns: ['bbox'],
		inputs: [],
		compatible: null,
		interactive: true,
	},
	{
		name: 'htr',
		configured: false,
		returns: ['text'],
		inputs: [],
		compatible: null,
		interactive: false,
	},
	{
		name: 'sam',
		configured: false,
		returns: ['polygon'],
		inputs: [],
		compatible: null,
		interactive: true,
	},
];

interface AssistBody {
	producer: string;
	prompt: string | null;
	region: { x: number; y: number; width: number; height: number } | null;
	points: { x: number; y: number; positive: boolean }[];
	task_id: string | null;
}

/** The annotator's in-repo stand-in for a model (`assist._mock`), shape for shape: a detector's box at the drawn region
 *  (or its default), labelled with the prompt; a segmenter's diamond over its positive clicks' box, padded by half a
 *  120-pixel patch, its scores rising with each click. */
function proposals(body: AssistBody): Record<string, unknown>[] {
	const label = (body.prompt ?? '').trim();
	if (body.producer.startsWith('sam')) {
		const anchors = body.points.filter((p) => p.positive);
		const at = anchors.length > 0 ? anchors : body.points;
		const pad = 60;
		const xs = at.map((p) => p.x);
		const ys = at.map((p) => p.y);
		const [x, y] = [Math.min(...xs) - pad, Math.min(...ys) - pad];
		const [w, h] = [
			Math.max(...xs) - Math.min(...xs) + 2 * pad,
			Math.max(...ys) - Math.min(...ys) + 2 * pad,
		];
		const n = body.points.length;
		return [
			{
				shape_type: 'polygon',
				x,
				y,
				width: w,
				height: h,
				polygon: [x + w / 2, y, x + w, y + h / 2, x + w / 2, y + h, x, y + h / 2],
				label: label || 'object',
				confidence: Math.min(0.95, 0.7 + 0.05 * n),
				uncertainty: Math.max(0.05, 0.3 - 0.04 * n),
			},
		];
	}
	const r = body.region ?? { x: 100, y: 100, width: 200, height: 80 };
	return [
		{
			shape_type: 'bbox',
			...r,
			polygon: [],
			label: label || 'region',
			confidence: 0.7,
			uncertainty: 0.45,
		},
	];
}

/** Answer one call to `/api/assist/…`, or null when the path is not that door. Its gate is the service's: the
 *  annotating role on the project the request names (`assist.require_assist`). */
export async function assistDoor(
	req: Request,
	url: URL,
	perms: (projectId: string) => string[],
	record: (status: number) => void,
): Promise<Response | null> {
	if (!url.pathname.startsWith('/api/assist/')) return null;
	const answer = (body: unknown, status = 200): Response => {
		record(status);
		return json(body, status);
	};
	const project = url.searchParams.get('project') ?? '';
	if (!perms(project).includes('can_annotate'))
		return answer({ detail: 'you lack can_annotate on this project' }, 403);
	if (url.pathname === '/api/assist/producers' && req.method === 'GET')
		return answer({ producers: PRODUCERS, default_configured: false, urls_redacted: true });
	if (req.method !== 'POST') return null;
	const body = (await req.json()) as AssistBody;
	return answer({ shapes: proposals(body), source: `model:${body.producer}`, dropped: [] });
}
