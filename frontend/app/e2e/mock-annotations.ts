// The annotator's annotations door as the e2e mock serves it: one unit's rows per project, dataset and key, read as
// Arrow IPC with the version in `X-Annotations-Version`, saved as one review delta that names the version it was built
// from (a moved version is a 409), with its history.
//
// It refuses what the service's door refuses (`annotator.annotations.access`, `annotator.annotations.unit`), in its
// words: a key whose parts are not the dataset's identity (422), a project the caller may not view (404), a task that is
// not that project's (404), a unit that is not the task's (404), and a save the task takes no edit for (409 for its
// state, 403 for the caller).
import type { Task } from './mock-projects';
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
	'pages|acme$letters|letter-001/0': [
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

/** Each mock dataset's identity, as its descriptor declares it (`mock-services.ts`): a doc key and an integer chunk. */
const IDENTITY: Record<string, string[]> = {
	acme$letters: ['doc_id', 'chunk_id'],
	acme$maps: ['doc_id', 'chunk_id'],
};

/** The service's refusal of a key its dataset's identity cannot name (`annotator.annotations.unit.parse_unit`). */
function keyRefusal(dataset: string, key: string): string | null {
	const fields = IDENTITY[dataset] ?? [];
	const parts = key.split('/');
	if (parts.length !== fields.length)
		return `unit key '${key}' has ${parts.length} part(s), and this dataset names a unit by ${fields.length}: ${fields.join('/')}`;
	const [, chunk = ''] = parts;
	if (!/^-?\d{1,19}$/.test(chunk))
		return `unit key '${key}': chunk_id is an integer, not '${chunk}'`;
	return null;
}

/** Each person's units, so no spec sees another's saves. */
const worlds = new Map<string, Map<string, Unit>>();

function unitOf(sub: string, id: string): Unit {
	let units = worlds.get(sub);
	if (!units) {
		units = new Map();
		worlds.set(sub, units);
	}
	let unit = units.get(id);
	if (!unit) {
		const rows = structuredClone(SEEDED[id] ?? []);
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
	const unit = unitOf(sub, `${project}|${dataset}|${key}`);

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
			return { ...r, ...edit, ...geo, ...span, reviewer: sub };
		});
	rows.push(...b.inserts.map((r) => ({ ...r, reviewer: sub })));
	unit.version += 1;
	unit.rows = rows;
	unit.history.push({ version: unit.version, timestamp: new Date().toISOString(), rows });
	return answer({ version: unit.version, inserted: b.inserts.length, deleted: gone.size });
}
