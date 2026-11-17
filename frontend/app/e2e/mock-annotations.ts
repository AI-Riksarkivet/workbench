// The annotator's annotations door as the e2e mock serves it: one unit's rows per project, dataset and key, read as
// Arrow IPC with the version in `X-Annotations-Version`, saved as one review delta that names the version it was built
// from (a moved version is a 409), with its history.
//
// It refuses what the service's door refuses (`annotator.annotations.access`, `annotator.annotations.unit`), in its
// words: a key whose parts are not the dataset's identity (422), a project the caller may not view (404), a task that is
// not that project's (404), a unit that is not the task's (404), and a save the task takes no edit for (409 for its
// state, 403 for the caller).
import { mayReadWork, type Task } from './mock-projects';
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

/** One annotation row, as the canvas sends and reads it. */
type Row = Record<string, unknown> & { id: string };

interface Unit {
	version: number;
	rows: Row[];
	/** The rows at each version, newest last. */
	history: { version: number; timestamp: string; rows: Row[] }[];
}

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
];

function ipc(rows: Row[]): Uint8Array {
	const columns = Object.fromEntries(
		COLUMNS.map(([name, type]) => [
			name,
			vectorFromArray(
				rows.map((r) => r[name] ?? null),
				type,
			),
		]),
	);
	return tableToIPC(new Table(columns), 'stream');
}

/** Two boxes on the first page of the canvas's item: a figure top left and a caption bottom right (the picture is
 *  160 × 160). */
const SEEDED: Record<string, Row[]> = {
	'pages|lake~acme$letters|letter-001/0': [
		{
			id: 'fig-1',
			shape_type: 'bbox',
			x: 10,
			y: 10,
			width: 50,
			height: 40,
			polygon: [],
			label: 'figure',
			status: 'accepted',
			source: 'human',
			// Keys the attribute editor does not show, nested and not: an edit of one attribute keeps them.
			metadata: '{"damaged":"false","provenance":{"source":"ocr","page":3}}',
		},
		{
			id: 'cap-1',
			shape_type: 'bbox',
			x: 90,
			y: 100,
			width: 60,
			height: 40,
			polygon: [],
			label: 'caption',
			text: 'The harbour at dawn',
			status: 'prediction',
			source: 'model:seed',
			uncertainty: 0.4,
			// Metadata that is not an object: an attribute edit is refused rather than writing over it.
			metadata: '"scribbled note"',
		},
	],
};

/** The first replica of a consensus item on the `pages` project, its own annotator's rows of one unit; the second's
 *  work is its task's draft (`mock-projects.ts`). */
const BY_TASK: Record<string, Row[]> = {
	'pagesg-r1': [
		{
			id: 'r1-fig',
			shape_type: 'bbox',
			x: 10,
			y: 10,
			width: 50,
			height: 40,
			polygon: [],
			label: 'figure',
			status: 'accepted',
			source: 'human',
		},
		{
			id: 'r1-cap',
			shape_type: 'bbox',
			x: 90,
			y: 100,
			width: 60,
			height: 40,
			polygon: [],
			label: 'caption',
			status: 'accepted',
			source: 'human',
		},
	],
};

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

/** Each person's units, so no spec sees another's saves. */
const worlds = new Map<string, Map<string, Unit>>();

/** A unit's rows are its task's (`annotator.annotations.unit.Unit.of_task`): `id` is `project|task|dataset|key`. */
function unitOf(sub: string, id: string): Unit {
	let units = worlds.get(sub);
	if (!units) {
		units = new Map();
		worlds.set(sub, units);
	}
	let unit = units.get(id);
	if (!unit) {
		const [project, task, dataset, key] = id.split('|');
		const rows = structuredClone(
			BY_TASK[task ?? ''] ?? SEEDED[`${project}|${dataset}|${key}`] ?? [],
		);
		unit = {
			version: rows.length > 0 ? 1 : 0,
			rows,
			history: rows.length > 0 ? [{ version: 1, timestamp: new Date().toISOString(), rows }] : [],
		};
		units.set(id, unit);
	}
	return unit;
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
	base_version: number | null;
}

/** Answer one call to `/api/annotations/…`, or null when the path is not that door. `perms` is what the caller holds
 *  on a project; `record` writes the call's status into the ledger; `fault` is the spec's switch for this path;
 *  `lakehouseWrite` writes a save to the lakehouse as the workbench's own client, as the annotator does. */
export async function annotationsDoor(
	req: Request,
	url: URL,
	sub: string,
	perms: (projectId: string) => string[],
	taskOf: (taskId: string) => Task | undefined,
	record: (status: number) => void,
	fault: string | undefined,
	lakehouseWrite: () => Promise<boolean>,
): Promise<Response | null> {
	const match = /^\/api\/annotations\/(.+?)(\/versions)?$/.exec(url.pathname);
	if (!match) return null;
	const answer = (body: unknown, status = 200): Response => {
		record(status);
		return json(body, status);
	};
	if (fault === 'fail')
		return answer({ detail: 'the annotator could not open the annotations table' }, 503);
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
	if (task.source.where !== dataset || !task.source.keys.includes(key))
		return answer({ detail: `unit ${key} of dataset ${dataset} is not task ${taskId}'s` }, 404);
	if (req.method === 'GET' && !mayReadWork(task, sub, held))
		return answer(
			{
				detail: `${sub.replace(/^user:/, '')} may not read replica ${taskId}'s work: only its annotator, or someone who may review it`,
			},
			403,
		);
	if (req.method !== 'GET') {
		const edits: Record<string, string> = { claimed: 'can_annotate', in_review: 'can_review' };
		const relation = edits[task.state];
		if (!relation)
			return answer(
				{
					detail: `task ${taskId} is ${task.state} — its shapes take edits only while it is claimed or in review`,
				},
				409,
			);
		const who = sub.replace(/^user:/, '');
		if (!held.includes(relation))
			return answer({ detail: `${who} lacks ${relation} on task:${taskId}` }, 403);
		if (task.state === 'claimed' && task.assignee !== sub)
			return answer({ detail: `the task is held by ${task.assignee}` }, 403);
		if (task.state === 'in_review' && task.submitted_by === sub)
			return answer({ detail: 'a reviewer may not edit their own submission' }, 403);
	}
	const refused = keyRefusal(dataset, key);
	if (refused) return answer({ detail: refused }, 422);
	const unit = unitOf(sub, `${project}|${taskId}|${dataset}|${key}`);

	if (match[2] && req.method === 'GET') {
		return answer(
			unit.history
				.toReversed()
				.map((h) => ({ version: h.version, timestamp: h.timestamp, count: h.rows.length })),
		);
	}
	if (req.method === 'GET') {
		const at = url.searchParams.get('version');
		const rows =
			at === null ? unit.rows : (unit.history.find((h) => h.version === Number(at))?.rows ?? []);
		record(200);
		return new Response(new Blob([new Uint8Array(ipc(rows))]), {
			headers: {
				'content-type': 'application/vnd.apache.arrow.stream',
				'x-annotations-version': String(unit.version),
				'cache-control': 'no-store',
			},
		});
	}
	if (req.method !== 'POST') return null;
	const b = (await req.json()) as SaveBody;
	if (b.base_version !== unit.version)
		return answer(
			{ detail: `the annotations moved: version ${unit.version}, not ${b.base_version}` },
			409,
		);
	if (!(await lakehouseWrite()))
		return answer({ detail: 'the lakehouse refused the workbench’s write' }, 503);
	const gone = new Set(b.deletes);
	const rows = unit.rows
		.filter((r) => !gone.has(r.id))
		.map((r) => {
			const edit = b.edits.find((e) => e.id === r.id);
			const geo = b.geometry.find((g) => g.id === r.id);
			const span = b.spans.find((s) => s.id === r.id);
			const time = b.temporal.find((t) => t.id === r.id);
			return { ...r, ...edit, ...geo, ...span, ...time, reviewer: sub };
		});
	rows.push(...b.inserts.map((r) => ({ ...r, reviewer: sub })));
	unit.version += 1;
	unit.rows = rows;
	unit.history.push({ version: unit.version, timestamp: new Date().toISOString(), rows });
	return answer({ version: unit.version, inserted: b.inserts.length, deleted: gone.size });
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
		return answer({ detail: `the caller lacks can_annotate on project:${project}` }, 403);
	if (url.pathname === '/api/assist/producers' && req.method === 'GET')
		return answer({ producers: PRODUCERS, default_configured: false, urls_redacted: true });
	if (req.method !== 'POST') return null;
	const body = (await req.json()) as AssistBody;
	return answer({ shapes: proposals(body), source: `model:${body.producer}`, dropped: [] });
}
