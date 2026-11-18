// The annotator's project plane, as the e2e annotator mock serves it: one world of projects, tasks and grants per
// person, seeded on their first call, so no spec sees another's writes.
//
// A person's ROLE is the first word of their name, lowercased, as `person('Manager')` mints `user:manager-…`: owner,
// manager, reviewer, annotator or viewer holds that role on every seeded project of the organisation `acme`, and any
// other name (`person('Stranger')`) belongs to no organisation and holds no role. Each door asks the role the
// annotator's does (`service_kit/governed/auth/model.fga`'s ladder: owner > manager > reviewer > annotator > viewer)
// and refuses as it does: 403 naming the relation, before it says whether the project exists or what state it is in.

/** The ladder, highest first, and the permissions each rung holds (`model.fga`). */
const ROLES = ['owner', 'manager', 'reviewer', 'annotator', 'viewer'] as const;
type Role = (typeof ROLES)[number];
const PERMS: Record<Role, string[]> = {
	owner: [
		'can_administer',
		'can_manage',
		'can_publish',
		'can_review',
		'can_annotate',
		'can_claim',
		'can_send_items',
		'can_view',
	],
	manager: [
		'can_manage',
		'can_publish',
		'can_review',
		'can_annotate',
		'can_claim',
		'can_send_items',
		'can_view',
	],
	reviewer: ['can_review', 'can_annotate', 'can_claim', 'can_send_items', 'can_view'],
	annotator: ['can_annotate', 'can_claim', 'can_send_items', 'can_view'],
	viewer: ['can_view'],
};
/** What a project's read answers the reader holds: the service's `READER_PERMISSIONS`. */
const READER = [
	'can_administer',
	'can_manage',
	'can_publish',
	'can_review',
	'can_annotate',
	'can_claim',
	'can_send_items',
];

const PROJECT_EDGES: Record<string, Record<string, [string, string]>> = {
	draft: { open: ['labeling', 'can_manage'] },
	labeling: { freeze: ['frozen', 'can_manage'] },
	frozen: {
		open: ['labeling', 'can_manage'],
		publish: ['publishing', 'can_publish'],
		archive: ['archived', 'can_manage'],
	},
	publish_failed: { publish: ['publishing', 'can_publish'] },
	published: { archive: ['archived', 'can_manage'] },
};
const TASK_EDGES: Record<string, Record<string, [string, string]>> = {
	unassigned: { claim: ['claimed', 'can_claim'], assign: ['claimed', 'can_manage'] },
	changes_requested: { claim: ['claimed', 'can_claim'], assign: ['claimed', 'can_manage'] },
	claimed: {
		submit: ['in_review', 'can_annotate'],
		release: ['unassigned', 'can_annotate'],
		skip: ['skipped', 'can_annotate'],
		save_draft: ['claimed', 'can_annotate'],
	},
	in_review: {
		accept: ['accepted', 'can_review'],
		fix_and_accept: ['accepted', 'can_review'],
		request_changes: ['changes_requested', 'can_review'],
	},
	accepted: { reopen: ['changes_requested', 'can_manage'] },
	skipped: { requeue: ['unassigned', 'can_manage'] },
};
const TERMINAL = new Set(['accepted', 'skipped']);

export interface Task {
	task_id: string;
	project_id: string;
	state: string;
	assignee: string | null;
	lease_expires_at: string | null;
	source: { kind: string; keys: string[]; where: string | null; dataset_version: number | null };
	media: { kind: string; image_url: null; media_url: null };
	review_required: boolean;
	replica_of: string | null;
	submitted_by: string | null;
	submitted_at?: string | null;
	reviewed_by: string | null;
	reviewed_at?: string | null;
	review_action: string | null;
	sent_at?: string | null;
	holders?: string[];
	prediction: { shape_type: string; label: string; source: string }[];
	review_notes: { by: string; at: string; action: string; message: string }[];
	/** The ontology captured onto the task at send, which the canvas reads its rules from. */
	ontology?: Record<string, unknown>;
}

/** One annotation row of a task's work, as the canvas sends and reads it. */
export type Row = Record<string, unknown> & { id: string };

/** A task's draft, its work in progress: the rows the canvas reads and saves, the links between them, its revision,
 *  and its kept saves (`annotator.state.store`: the draft and its bounded history). */
export interface Draft {
	rows: Row[];
	links: { name: string; from_shape: string; to_shape: string }[];
	revision: number;
	history: {
		version: number;
		timestamp: string;
		author: string;
		rows: Row[];
		links?: { name: string; from_shape: string; to_shape: string }[];
	}[];
}

/** A draft's rows as the service's `Shape`s, as `GET /tasks/{id}/draft` answers them. */
function shapeOf(row: Row): Record<string, unknown> {
	let attributes: Record<string, string> = {};
	try {
		const parsed: unknown = JSON.parse(String(row.metadata ?? '{}'));
		if (parsed && typeof parsed === 'object' && !Array.isArray(parsed))
			attributes = Object.fromEntries(Object.entries(parsed).map(([k, v]) => [k, String(v)]));
	} catch {
		attributes = {};
	}
	const offset = (v: unknown): number | null => (typeof v === 'number' && v >= 0 ? v : null);
	return {
		shape_id: row.id,
		shape_type: row.shape_type,
		x: row.x ?? null,
		y: row.y ?? null,
		width: row.width ?? null,
		height: row.height ?? null,
		polygon: row.polygon ?? [],
		t_start: row.t_start ?? null,
		t_end: row.t_end ?? null,
		parent_id: row.parent_id || null,
		char_start: offset(row.char_start),
		char_end: offset(row.char_end),
		label: row.label ?? null,
		text: row.text ?? null,
		group: row.group || null,
		source: row.source ?? null,
		status: row.status ?? 'accepted',
		confidence: row.confidence ?? null,
		attributes,
	};
}

/** Two boxes on the first page of the canvas's item: a figure top left and a caption bottom right (the picture is
 *  160 × 160), the work of every task of `pages` on that page that has no work of its own below. */
const PAGE_ROWS: Row[] = [
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
];

/** A box row of a consensus replica's work. */
const box = (
	id: string,
	x: number,
	y: number,
	width: number,
	height: number,
	label: string,
): Row => ({
	id,
	shape_type: 'bbox',
	x,
	y,
	width,
	height,
	polygon: [],
	label,
	status: 'accepted',
	source: 'human',
});

/** Each replica of the `pages` consensus item has its own work: the first its figure and caption, the second the figure
 *  where the first has it, the caption called a figure, and a box the first has not. */
const REPLICA_ROWS: Record<string, Row[]> = {
	'pagesg-r1': [box('r1-fig', 10, 10, 50, 40, 'figure'), box('r1-cap', 90, 100, 60, 40, 'caption')],
	'pagesg-r2': [
		box('r2-fig', 11, 10, 50, 40, 'figure'),
		box('r2-cap', 90, 100, 60, 40, 'figure'),
		box('r2-new', 100, 10, 40, 30, 'figure'),
	],
};

interface Project {
	project_id: string;
	tenant: string;
	slug: string;
	title: string;
	description: string;
	instructions: string;
	state: string;
	review_required: boolean;
	lease_seconds?: number;
	consensus_n: number;
	adjudications: Record<string, { task_id: string; by: string; at: string }>;
	ontology: Record<string, unknown>;
	counts: Record<string, number>;
	publish_error: string | null;
	publish_progress: string | null;
	pending_target_namespace: string | null;
}

interface World {
	role: Role | null;
	projects: Map<string, Project>;
	tasks: Map<string, Task>;
	grants: Map<string, { user: string; relation: string }[]>;
	drafts: Map<string, Draft>;
	/** The name each principal's own token carried when they last wrote (`annotator.people`). */
	people: Map<string, string>;
}

/** The names the seeded people wrote under, as the annotator would have kept them from their tokens. `user:gone` never
 *  wrote, so the pages show their principal. */
const SEEDED_NAMES: Record<string, string> = {
	'user:alpha': 'Alma Alpha',
	'user:beta': 'Bert Beta',
	'user:someone-else': 'Sam Else',
	'user:reviewer-x': 'Rex Reviewer',
	'user:other-annotator': 'Otto Other',
};

const worlds = new Map<string, World>();
/** A lease an hour from the seed, as a claim takes one. */
const later = (): string => new Date(Date.now() + 3600_000).toISOString();

function roleOf(sub: string): Role | null {
	const word = sub.replace(/^user:/, '').split('-')[0] as Role;
	return ROLES.includes(word) ? word : null;
}

function task(
	project: string,
	id: string,
	state: string,
	key: string,
	over: Partial<Task> = {},
): Task {
	return {
		task_id: id,
		project_id: project,
		state,
		assignee: null,
		lease_expires_at: null,
		source: { kind: 'chunks', keys: [key], where: 'lake~acme$letters', dataset_version: 3 },
		media: { kind: 'image', image_url: null, media_url: null },
		review_required: true,
		replica_of: null,
		submitted_by: null,
		reviewed_by: null,
		review_action: null,
		prediction: [],
		review_notes: [],
		...over,
	};
}

function project(id: string, over: Partial<Project>): Project {
	return {
		project_id: id,
		tenant: 'acme',
		slug: id,
		title: '',
		description: '',
		instructions: '',
		state: 'labeling',
		review_required: true,
		consensus_n: 1,
		adjudications: {},
		ontology: { kind: '', classes: [], relations: [] },
		counts: {},
		publish_error: null,
		publish_progress: null,
		pending_target_namespace: null,
		...over,
	};
}

function seed(sub: string): World {
	const role = roleOf(sub);
	const projects = new Map<string, Project>();
	const tasks = new Map<string, Task>();
	const letters = project('letters', {
		title: 'Letters',
		description: 'Which pages are letters.',
		instructions: 'Tag each page that is a letter.',
		ontology: {
			kind: 'image-classification',
			classes: [
				{ name: 'letter', tools: ['tag'], required: false },
				{ name: 'map', tools: ['tag'], required: false },
			],
			relations: [],
		},
	});
	projects.set(letters.project_id, letters);
	for (const t of [
		task('letters', 'letters-t1', 'unassigned', 'letter-001/0'),
		task('letters', 'letters-t2', 'claimed', 'letter-001/1', {
			assignee: sub,
			lease_expires_at: later(),
		}),
		task('letters', 'letters-t3', 'in_review', 'letter-002/0', {
			submitted_by: 'user:someone-else',
			prediction: [{ shape_type: 'tag', label: 'letter', source: 'bulk' }],
		}),
		task('letters', 'letters-t4', 'accepted', 'map-003/0', {
			submitted_by: 'user:someone-else',
			reviewed_by: 'user:reviewer-x',
		}),
	])
		tasks.set(t.task_id, t);
	// Consensus: two annotators per item. g1's replicas are both accepted (adjudicable); g2's are in review, one of
	// them submitted by the reader, which a bulk accept is refused for. And an item whose lease has lapsed.
	const panel = project('panel', { title: 'Panel', consensus_n: 2 });
	projects.set(panel.project_id, panel);
	for (const t of [
		task('panel', 'panel-g1-r1', 'accepted', 'letter-001/0', {
			replica_of: 'panel-g1',
			submitted_by: 'user:alpha',
		}),
		task('panel', 'panel-g1-r2', 'accepted', 'letter-001/0', {
			replica_of: 'panel-g1',
			submitted_by: 'user:beta',
		}),
		task('panel', 'panel-g2-r1', 'in_review', 'letter-002/0', {
			replica_of: 'panel-g2',
			submitted_by: 'user:alpha',
		}),
		task('panel', 'panel-g2-r2', 'in_review', 'letter-002/0', {
			replica_of: 'panel-g2',
			submitted_by: sub,
		}),
		task('panel', 'panel-x', 'claimed', 'map-003/0', {
			assignee: 'user:gone',
			lease_expires_at: new Date(Date.now() - 600_000).toISOString(),
		}),
	])
		tasks.set(t.task_id, t);
	// A publish that failed, to retry.
	const minutes = project('minutes', {
		title: 'Minutes',
		state: 'publish_failed',
		publish_error: 'the lakehouse refused the table: the namespace is protected',
		publish_progress: 'creating the table',
	});
	projects.set(minutes.project_id, minutes);
	tasks.set('minutes-t1', task('minutes', 'minutes-t1', 'accepted', 'letter-002/0'));
	const charters = project('charters', { title: 'Charters', state: 'frozen' });
	projects.set(charters.project_id, charters);
	tasks.set(
		'charters-t1',
		task('charters', 'charters-t1', 'accepted', 'letter-002/0', {
			submitted_by: 'user:someone-else',
		}),
	);
	// The canvas's project: image pages to draw on, with the reader's two claimed items, an ontology whose classes may
	// be drawn with any tool, two item-level choices, an attribute, a transcribing class and one relation.
	const pagesOntology = {
		kind: 'object-detection',
		modality: 'image',
		classes: [
			{
				name: 'figure',
				tools: [],
				required: false,
				transcribe: false,
				attributes: [{ name: 'damaged', type: 'bool', choices: [], required: false }],
			},
			{
				name: 'caption',
				tools: [],
				required: false,
				transcribe: true,
				attributes: [{ name: 'legible', type: 'bool', choices: [], required: false }],
			},
			{ name: 'letter', tools: ['tag'], required: false, transcribe: false, attributes: [] },
			{ name: 'map', tools: ['tag'], required: false, transcribe: false, attributes: [] },
		],
		relations: [
			{ name: 'caption_of', from_classes: [], to_classes: [], directed: true, required: false },
		],
		allow_empty: true,
	};
	const pages = project('pages', { title: 'Pages', ontology: pagesOntology });
	projects.set(pages.project_id, pages);
	for (const t of [
		task('pages', 'pages-t1', 'claimed', 'letter-001/0', {
			assignee: sub,
			lease_expires_at: later(),
			ontology: pagesOntology,
		}),
		task('pages', 'pages-t2', 'claimed', 'map-003/0', {
			assignee: sub,
			lease_expires_at: later(),
			ontology: pagesOntology,
		}),
		// Another annotator's claim: the reader opens it read-only.
		task('pages', 'pages-held', 'claimed', 'letter-002/0', {
			assignee: 'user:someone-else',
			lease_expires_at: later(),
			ontology: pagesOntology,
		}),
		// A key the dataset's identity (doc_id/chunk_id) cannot name: the door refuses it in words.
		task('pages', 'pages-bad', 'accepted', 'letter-001/0/0', { ontology: pagesOntology }),
		// An item of two units: the canvas opens the first.
		task('pages', 'pages-multi', 'accepted', 'letter-001/0', {
			source: {
				kind: 'chunks',
				keys: ['letter-001/0', 'letter-001/1'],
				where: 'lake~acme$letters',
				dataset_version: 3,
			},
			ontology: pagesOntology,
		}),
	])
		tasks.set(t.task_id, t);
	// A consensus item of the pages project: two replicas of one unit, each its annotator's own rows; the first in
	// review, the second accepted, for a reviewer to compare and take from.
	for (const [id, state, by] of [
		['pagesg-r1', 'in_review', 'user:alpha'],
		['pagesg-r2', 'accepted', 'user:beta'],
	] as const)
		tasks.set(
			id,
			task('pages', id, state, 'letter-002/0', {
				replica_of: 'pagesg',
				submitted_by: by,
				ontology: pagesOntology,
			}),
		);
	// A pages item someone else submitted, for a reviewer's verdict on the canvas.
	tasks.set(
		'pages-review',
		task('pages', 'pages-review', 'in_review', 'map-003/0', {
			submitted_by: 'user:someone-else',
			ontology: pagesOntology,
		}),
	);
	// The canvas over a recording and a video: the demo's own, whose answers the viewer recorded (`seeded-answers.json`).
	const soundOntology = {
		kind: 'audio-classification',
		modality: 'audio',
		classes: [
			{ name: 'question', tools: ['segment'], required: true, transcribe: true, attributes: [] },
			{ name: 'answer', tools: ['segment'], required: false, transcribe: true, attributes: [] },
			{ name: 'noise', tools: ['segment'], required: false, transcribe: false, attributes: [] },
		],
		relations: [],
		allow_empty: false,
	};
	const filmOntology = {
		kind: 'object-tracking',
		modality: 'video',
		classes: [
			{ name: 'boat', tools: ['bbox'], required: false, transcribe: false, attributes: [] },
			{ name: 'cart', tools: ['bbox'], required: false, transcribe: false, attributes: [] },
		],
		relations: [],
		allow_empty: false,
	};
	for (const [id, title, ontology, key, where, kind] of [
		[
			'sounds',
			'Interview turns',
			soundOntology,
			'c41f0e2a9b7d3e15/0/1',
			'lake~demo$recordings',
			'audio',
		],
		['films', 'Moving objects', filmOntology, 'f1a07c3e9d2b4e68/0/0', 'lake~demo$films', 'video'],
	] as const) {
		projects.set(id, project(id, { title, ontology }));
		tasks.set(
			`${id}-t1`,
			task(id, `${id}-t1`, 'claimed', key, {
				assignee: sub,
				lease_expires_at: later(),
				ontology,
				source: { kind: 'chunks', keys: [key], where, dataset_version: 1 },
				media: { kind, image_url: null, media_url: null },
			}),
		);
	}
	// A consensus project where the reader holds one replica and another annotator the other: the reader may not
	// see the other's work, so no replica is offered to compare.
	projects.set(
		'pairs',
		project('pairs', { title: 'Pairs', consensus_n: 2, ontology: pagesOntology }),
	);
	for (const [id, holder] of [
		['pairs-g1-r1', sub],
		['pairs-g1-r2', 'user:other-annotator'],
	] as const)
		tasks.set(
			id,
			task('pairs', id, 'claimed', 'letter-001/0', {
				replica_of: 'pairs-g1',
				assignee: holder,
				lease_expires_at: later(),
				ontology: pagesOntology,
			}),
		);
	// A text to mark entities in: the item is its row's text, the classes closed and drawn as text only.
	const notesOntology = {
		kind: 'token-classification',
		modality: 'text',
		classes: [
			{ name: 'person', tools: ['text'], required: false, transcribe: false, attributes: [] },
			{ name: 'place', tools: ['text'], required: false, transcribe: false, attributes: [] },
		],
		relations: [],
		allow_empty: false,
	};
	projects.set('notes', project('notes', { title: 'Entities', ontology: notesOntology }));
	tasks.set(
		'notes-t1',
		task('notes', 'notes-t1', 'claimed', 'letter-001/0', {
			assignee: sub,
			lease_expires_at: later(),
			ontology: notesOntology,
			media: { kind: 'text', image_url: null, media_url: null },
		}),
	);
	// A project a reviewer judges one item at a time: three items in review, each another annotator's, and one already
	// accepted, for browsing. Every item holds the two boxes of `PAGE_ROWS`, a figure and a caption, but one claimed and
	// never drawn on (`judging-e`), which a browse of the labeled work leaves out.
	projects.set('judging', project('judging', { title: 'Judging', ontology: pagesOntology }));
	for (const [id, state, key, by] of [
		['judging-a', 'in_review', 'letter-001/0', 'user:alpha'],
		['judging-b', 'in_review', 'letter-002/0', 'user:beta'],
		['judging-c', 'in_review', 'map-003/0', 'user:someone-else'],
		['judging-d', 'accepted', 'letter-001/1', 'user:alpha'],
	] as const)
		tasks.set(
			id,
			task('judging', id, state, key, {
				submitted_by: by,
				submitted_at: '2026-11-17T10:00:00Z',
				holders: [by],
				...(state === 'accepted'
					? {
							reviewed_by: 'user:reviewer-x',
							reviewed_at: '2026-11-17T11:00:00Z',
							review_action: 'accepted',
						}
					: {}),
				ontology: pagesOntology,
			}),
		);
	tasks.set(
		'judging-e',
		task('judging', 'judging-e', 'claimed', 'letter-002/0', {
			assignee: 'user:alpha',
			lease_expires_at: later(),
			holders: ['user:alpha'],
			ontology: pagesOntology,
		}),
	);
	// A classification project for the bulk-labeling grid: two classes a grid labels (tags) and one drawn as a box, which
	// is labelled on the canvas; 24 items nobody holds, on the four pages the mock dataset has, one the reader holds, one
	// another annotator holds, and two in review that another annotator submitted, one of them labelled `map`.
	const sortingOntology = {
		kind: 'image-classification',
		modality: 'image',
		classes: [
			{ name: 'letter', tools: ['tag'], required: false, transcribe: false, attributes: [] },
			{ name: 'map', tools: ['tag'], required: false, transcribe: false, attributes: [] },
			{ name: 'stamp', tools: ['bbox'], required: false, transcribe: false, attributes: [] },
		],
		relations: [],
		allow_empty: false,
	};
	projects.set('sorting', project('sorting', { title: 'Sorting', ontology: sortingOntology }));
	const sortingKeys = ['letter-001/0', 'letter-001/1', 'letter-002/0', 'map-003/0'];
	for (let i = 0; i < 24; i++) {
		const id = `sorting-${String(i).padStart(2, '0')}`;
		tasks.set(
			id,
			task('sorting', id, 'unassigned', sortingKeys[i % 4] ?? '', { ontology: sortingOntology }),
		);
	}
	tasks.set(
		'sorting-mine',
		task('sorting', 'sorting-mine', 'claimed', 'map-003/0', {
			assignee: sub,
			lease_expires_at: later(),
			ontology: sortingOntology,
		}),
	);
	tasks.set(
		'sorting-theirs',
		task('sorting', 'sorting-theirs', 'claimed', 'letter-001/0', {
			assignee: 'user:alpha',
			lease_expires_at: later(),
			ontology: sortingOntology,
		}),
	);
	for (const id of ['sorting-review-a', 'sorting-review-b'])
		tasks.set(
			id,
			task('sorting', id, 'in_review', 'letter-002/0', {
				submitted_by: 'user:alpha',
				holders: ['user:alpha'],
				ontology: sortingOntology,
			}),
		);
	// A project past what one bulk selection holds (`BULK_CAP`, 1,000): 1,050 items nobody has claimed, the first 50 sent
	// on the first of the month and the rest on the tenth, for the date range.
	projects.set('heap', project('heap', { title: 'Heap' }));
	for (let i = 0; i < 1050; i++) {
		const id = `heap-${String(i).padStart(4, '0')}`;
		tasks.set(
			id,
			task('heap', id, 'unassigned', 'letter-001/0', {
				sent_at: i < 50 ? '2026-11-01T09:00:00Z' : '2026-11-10T09:00:00Z',
			}),
		);
	}
	const grants = new Map<string, { user: string; relation: string }[]>();
	for (const id of projects.keys())
		grants.set(id, role ? [{ user: sub.replace(/^user:/, ''), relation: role }] : []);
	// Alma Alpha annotates the judging project, so a manager of it is offered her by name elsewhere.
	grants.get('judging')?.push({ user: 'alpha', relation: 'annotator' });
	const drafts = new Map<string, Draft>();
	const seededAt = new Date().toISOString();
	for (const t of tasks.values()) {
		const pages =
			(t.project_id === 'pages' && t.source.keys[0] === 'letter-001/0') ||
			(t.project_id === 'judging' && t.state !== 'claimed');
		const rows = REPLICA_ROWS[t.task_id] ?? (pages ? PAGE_ROWS : []);
		// Whole-item tags of the sorting project: a reviewer's item labelled `map`, an item whose `letter` carries a
		// person's words (what a new label would discard), and one a model proposed `map` for.
		const tagged: Row[] =
			{
				'sorting-review-b': [
					{ id: 'seed-map', shape_type: 'tag', label: 'map', status: 'accepted', source: 'human' },
				],
				'sorting-20': [
					{
						id: 'seed-said',
						shape_type: 'tag',
						label: 'letter',
						text: 'signed by the harbour master',
						status: 'accepted',
						source: 'human',
					},
				],
				'sorting-21': [
					{
						id: 'seed-proposed',
						shape_type: 'tag',
						label: 'map',
						status: 'prediction',
						source: 'model:vlm',
						confidence: 0.91,
					},
				],
			}[t.task_id] ?? [];
		if (rows.length === 0 && tagged.length === 0) continue;
		const seeded = structuredClone([...rows, ...tagged]);
		drafts.set(t.task_id, {
			rows: seeded,
			links: [],
			revision: 1,
			history: [{ version: 1, timestamp: seededAt, author: 'seed', rows: seeded }],
		});
	}
	return { role, projects, tasks, grants, drafts, people: new Map(Object.entries(SEEDED_NAMES)) };
}

/** Whether `sub` may read a task's work: any task they may view, but a consensus replica only when it is theirs or
 *  they may review it (`annotator.api.v1.endpoints.tasks.require_work_readable`). */
export const mayReadWork = (t: Task, sub: string, perms: string[]): boolean =>
	t.replica_of === null ||
	t.assignee === sub ||
	t.submitted_by === sub ||
	perms.includes('can_review');

/** The draft of one task of `sub`'s world, which the annotations door reads and changes: none before the first save. */
export const draftOf = (sub: string, taskId: string): Draft | undefined =>
	worldOf(sub).drafts.get(taskId);

/** Write the draft of one task of `sub`'s world. */
export const writeDraft = (sub: string, taskId: string, draft: Draft): void => {
	worldOf(sub).drafts.set(taskId, draft);
};

/** One task of `sub`'s world, as the annotations door reads it to judge a unit. */
export const taskOf = (sub: string, taskId: string): Task | undefined =>
	worldOf(sub).tasks.get(taskId);

/** The permissions `sub` holds on `projectId` in their world: what the annotations door asks. */
export const permissionsOn = (sub: string, projectId: string): string[] =>
	held(worldOf(sub), sub, projectId);

const worldOf = (sub: string): World => {
	let world = worlds.get(sub);
	if (!world) {
		world = seed(sub);
		worlds.set(sub, world);
	}
	return world;
};

const json = (body: unknown, status = 200): Response =>
	new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

/** The permissions `sub` holds on a project of their world: their role's, or an owner's of a project they created. */
function held(world: World, sub: string, projectId: string): string[] {
	const user = sub.replace(/^user:/, '');
	const grant = (world.grants.get(projectId) ?? []).find((g) => g.user === user);
	const role = (grant?.relation as Role | undefined) ?? null;
	return role ? PERMS[role] : [];
}

function counts(world: World, projectId: string): Record<string, number> {
	const out: Record<string, number> = {};
	for (const t of world.tasks.values())
		if (t.project_id === projectId) out[t.state] = (out[t.state] ?? 0) + 1;
	return out;
}

const withCounts = (world: World, p: Project): Project => ({
	...p,
	counts: counts(world, p.project_id),
});

const legalProject = (state: string) => [
	...Object.entries(PROJECT_EDGES[state] ?? {}).map(([event, [to, permission]]) => ({
		event,
		to,
		permission,
	})),
	...(state === 'draft' || state === 'labeling'
		? [{ event: 'send', to: state, permission: 'can_send_items' }]
		: []),
];
const legalTask = (state: string, frozen: boolean) =>
	frozen
		? []
		: Object.entries(TASK_EDGES[state] ?? {}).map(([event, [to, permission]]) => ({
				event,
				to,
				permission,
			}));

/** What `sub`, holding `perms`, may fire on `t` now, as the service answers it (`machines.caller_events`): the edges out
 *  of its state their role allows and no identity rule refuses (no verdict on their own submission, no holder's event
 *  on another's claim, a release of another's claim only by a manager). */
const callerEvents = (t: Task, sub: string, perms: readonly string[], frozen: boolean) =>
	legalTask(t.state, frozen).filter(({ event, permission }) => {
		if (!perms.includes(permission)) return false;
		if (['accept', 'fix_and_accept', 'request_changes'].includes(event) && t.submitted_by === sub)
			return false;
		const other = t.assignee !== null && t.assignee !== sub;
		if (['save_draft', 'submit', 'skip'].includes(event) && other) return false;
		return !(event === 'release' && other && !perms.includes('can_manage'));
	});

/** A failed check, as the service words it (`annotator.api.security.lacking`): who and what are machine fields. */
const OBJECT_WORDS: Record<string, string> = {
	project: 'project',
	task: 'item',
	org: 'organisation',
	corpus: 'dataset',
};
const refuse = (sub: string, relation: string, obj: string): Response => {
	const kind = obj.split(':')[0] ?? '';
	return json(
		{
			detail: `you lack ${relation} on this ${OBJECT_WORDS[kind] ?? kind}`,
			principal: sub.replace(/^user:/, ''),
			relation,
			object: obj,
		},
		403,
	);
};

/** Answer one call to the project plane, or null when the path is not one of its doors. `record(status)` writes the
 *  call into the ledger; `fault` is the spec's switch for this person and path. */
export async function projectsDoor(
	req: Request,
	url: URL,
	sub: string,
	record: (status: number) => void,
	fault: 'fail' | 'unbuilt' | 'slow' | undefined,
	name?: string,
): Promise<Response | null> {
	const path = url.pathname;
	if (!path.startsWith('/projects') && !path.startsWith('/tasks/')) return null;
	const world = worldOf(sub);
	// A write keeps the name the caller's own token carries (`annotator.people.record_person`).
	if (req.method !== 'GET' && name) world.people.set(sub, name);
	const namesOf = (principals: Iterable<string | null | undefined>): Record<string, string> =>
		Object.fromEntries(
			[...new Set(principals)]
				.filter((p): p is string => !!p && world.people.has(p))
				.map((p) => [p, world.people.get(p) ?? '']),
		);
	const principalsOf = (t: Task): (string | null | undefined)[] => [
		t.assignee,
		t.submitted_by,
		t.reviewed_by,
		...(t.holders ?? []),
		...t.review_notes.map((n) => n.by),
	];
	const answer = (body: unknown, status = 200): Response => {
		record(status);
		return json(body, status);
	};
	if (fault === 'slow') await Bun.sleep(2500);
	if (fault === 'fail')
		return answer({ detail: 'the annotator could not reach its state store' }, 503);
	const body = async (): Promise<Record<string, unknown>> =>
		(await req.json().catch(() => ({}))) as Record<string, unknown>;

	if (path === '/projects' && req.method === 'GET') {
		const tenant = url.searchParams.get('tenant');
		if (world.role === null || tenant !== 'acme') {
			record(403);
			return refuse(sub, 'can_list_projects', `org:${tenant}`);
		}
		const can = url.searchParams.get('can');
		const projects = [...world.projects.values()]
			.filter((p) => can === null || held(world, sub, p.project_id).includes(can))
			.map((p) => withCounts(world, p));
		return answer({ projects, total: projects.length });
	}
	if (path === '/projects' && req.method === 'POST') {
		const b = await body();
		if (world.role === null || b.tenant !== 'acme') {
			record(403);
			return refuse(sub, 'can_create_project', `org:${String(b.tenant)}`);
		}
		const id = `${String(b.slug)}-${crypto.randomUUID().slice(0, 6)}`;
		const created = project(id, {
			slug: String(b.slug),
			title: String(b.title ?? ''),
			description: String(b.description ?? ''),
			instructions: String(b.instructions ?? ''),
			state: 'draft',
			consensus_n: Number(b.consensus_n ?? 1),
			review_required: b.review_required !== false,
			ontology: (b.ontology as Record<string, unknown>) ?? { kind: '', classes: [], relations: [] },
		});
		world.projects.set(id, created);
		world.grants.set(id, [{ user: sub.replace(/^user:/, ''), relation: 'owner' }]);
		return answer(created, 201);
	}

	const projectPath = /^\/projects\/([\w-]+)(\/.*)?$/.exec(path);
	if (projectPath) {
		const [, id = '', rest = ''] = projectPath;
		const obj = `project:${id}`;
		const perms = held(world, sub, id);
		const may = (relation: string): boolean => perms.includes(relation);
		const p = world.projects.get(id);
		// Every door below refuses a person without the role before it says whether the project exists.
		// The one refusal someone who may not view the project gets, whatever it is (`unseen_project`).
		const unseen = (): Response | null => {
			if (may('can_view')) return null;
			record(404);
			return json({ detail: `No project ${id} that you can see` }, 404);
		};
		const gate = (relation: string): Response | null => {
			if (may(relation)) return null;
			record(403);
			return refuse(sub, relation, obj);
		};
		if (rest === '' && req.method === 'GET') {
			const refused = unseen();
			if (refused) return refused;
			if (!p) return answer({ detail: `annotation project ${id} does not exist` }, 404);
			return answer({
				project: withCounts(world, p),
				legal_events: legalProject(p.state),
				permissions: READER.filter(may).sort(),
			});
		}
		if (rest === '/tasks' && req.method === 'GET') {
			const refused = unseen();
			if (refused) return refused;
			const all = [...world.tasks.values()].filter((t) => t.project_id === id);
			const terminal = all.filter((t) => TERMINAL.has(t.state)).length;
			const frozen = p !== undefined && ['publishing', 'published', 'archived'].includes(p.state);
			const q = url.searchParams;
			const include = q.getAll('include');
			// The annotator's filters (`annotator.state.task_query.TaskFilter`), each over what the mock's tasks hold.
			const labelsOf = (t: Task): string[] => [
				...(world.drafts.get(t.task_id)?.rows ?? []).map((r) => String(r.label ?? '')),
				...t.prediction.map((s) => s.label),
			];
			const holder = q.get('holder');
			// A date range on one of an item's instants (`date`), as the service reads it: an item with no such instant
			// falls outside every range.
			const instantOf = (t: Task): string | null | undefined =>
				({
					sent: t.sent_at,
					submitted: t.submitted_at,
					reviewed: t.reviewed_at,
					updated: t.reviewed_at ?? t.submitted_at ?? t.sent_at,
				})[q.get('date') ?? 'updated'];
			const inRange = (t: Task): boolean => {
				const since = q.get('since');
				const until = q.get('until');
				if (since === null && until === null) return true;
				const at = instantOf(t);
				if (!at) return false;
				const ms = Date.parse(at);
				return (
					(since === null || ms >= Date.parse(since)) && (until === null || ms < Date.parse(until))
				);
			};
			const matches = (t: Task, withStates: boolean): boolean =>
				(!withStates || q.getAll('state').length === 0 || q.getAll('state').includes(t.state)) &&
				(q.get('assignee') === null || t.assignee === q.get('assignee')) &&
				(q.get('reviewer') === null || t.reviewed_by === q.get('reviewer')) &&
				(q.get('submitter') === null || t.submitted_by === q.get('submitter')) &&
				(q.get('not_submitter') === null || t.submitted_by !== q.get('not_submitter')) &&
				(holder === null ||
					t.assignee === holder ||
					t.submitted_by === holder ||
					(t.holders ?? []).includes(holder)) &&
				(q.get('label') === null || labelsOf(t).includes(q.get('label') ?? '')) &&
				(q.get('dataset') === null || t.source.where === q.get('dataset')) &&
				(q.get('group') === null || t.replica_of === q.get('group')) &&
				(q.get('replicas') !== 'true' || t.replica_of !== null) &&
				(q.get('open_to') === null ||
					['unassigned', 'changes_requested'].includes(t.state) ||
					(t.state === 'claimed' && t.assignee === q.get('open_to'))) &&
				(q.get('has_work') === null ||
					(q.get('has_work') === 'true') ===
						(world.drafts.get(t.task_id)?.rows.length ?? 0) +
							(world.drafts.get(t.task_id)?.links.length ?? 0) >
							0) &&
				inRange(t);
			// An item's work as a bulk label reads it (`task_query.ItemWork`): its revision and whole-item tags, at most 64, or
			// null where the reader may not read it (`tasks.reads_work`).
			const workOf = (t: Task) => {
				if (!mayReadWork(t, sub, perms)) return null;
				const draft = world.drafts.get(t.task_id);
				const tags = (draft?.rows ?? [])
					.filter((r) => r.shape_type === 'tag')
					.map((r) => ({
						id: r.id,
						label: typeof r.label === 'string' ? r.label : null,
						detailed:
							(typeof r.text === 'string' && r.text !== '') ||
							(typeof r.metadata === 'string' && !['', '{}'].includes(r.metadata)) ||
							(draft?.links ?? []).some((l) => l.from_shape === r.id || l.to_shape === r.id),
						prediction: r.status === 'prediction',
					}));
				return {
					revision: draft?.revision ?? 0,
					tags: tags.slice(0, 64),
					tags_cut: tags.length > 64,
				};
			};
			const ORDER = [
				'unassigned',
				'claimed',
				'changes_requested',
				'in_review',
				'accepted',
				'skipped',
			];
			const sort = q.get('sort') ?? 'item';
			const keyOf = (t: Task): string | number | null =>
				sort === 'state'
					? ORDER.indexOf(t.state)
					: sort === 'assignee'
						? t.assignee
						: sort === 'submitted'
							? (t.submitted_at ?? null)
							: sort === 'reviewed'
								? (t.reviewed_at ?? null)
								: sort === 'sent'
									? (t.sent_at ?? null)
									: t.task_id;
			const desc = q.get('order') === 'desc';
			const matched = all
				.filter((t) => matches(t, true))
				.sort((a, b) => {
					const ka = keyOf(a);
					const kb = keyOf(b);
					if (sort === 'item')
						return desc ? b.task_id.localeCompare(a.task_id) : a.task_id.localeCompare(b.task_id);
					if (ka !== kb) {
						if (ka === null) return 1;
						if (kb === null) return -1;
						return (ka < kb ? -1 : 1) * (desc ? -1 : 1);
					}
					return a.task_id.localeCompare(b.task_id);
				});
			const limit = Number(q.get('limit') ?? 100);
			let offset = Number(q.get('offset') ?? 0);
			const around = q.get('around');
			const position = around === null ? -1 : matched.findIndex((t) => t.task_id === around);
			if (position >= 0) offset = Math.max(0, position - Math.floor(limit / 2));
			const pageOf = matched.slice(offset, offset + limit);
			const stateCounts: Record<string, number> = Object.fromEntries(ORDER.map((st) => [st, 0]));
			for (const t of all)
				if (matches(t, false)) stateCounts[t.state] = (stateCounts[t.state] ?? 0) + 1;
			const contributors = new Map<
				string,
				{
					principal: string;
					submitted: number;
					accepted: number;
					changes_requested: number;
					open: number;
					reviewed: number;
				}
			>();
			const of = (who: string) => {
				const row = contributors.get(who) ?? {
					principal: who,
					submitted: 0,
					accepted: 0,
					changes_requested: 0,
					open: 0,
					reviewed: 0,
				};
				contributors.set(who, row);
				return row;
			};
			for (const t of all) {
				if (t.submitted_by) {
					const row = of(t.submitted_by);
					row.submitted += 1;
					if (t.state === 'accepted') row.accepted += 1;
					else if (t.review_action === 'request_changes' || t.state === 'changes_requested')
						row.changes_requested += 1;
				}
				if (
					t.assignee &&
					!t.submitted_by &&
					['unassigned', 'claimed', 'changes_requested'].includes(t.state)
				)
					of(t.assignee).open += 1;
				if (t.reviewed_by) of(t.reviewed_by).reviewed += 1;
			}
			const people = [
				...(include.includes('details') ? pageOf.flatMap(principalsOf) : []),
				...(include.includes('contributors') ? [...contributors.keys()] : []),
			];
			return answer({
				counts: counts(world, id),
				total: all.length,
				terminal,
				may_publish: all.length > 0 && terminal === all.length,
				...(include.includes('details')
					? {
							details: pageOf.map((t) => ({
								...t,
								legal_events: callerEvents(t, sub, perms, frozen),
								...(include.includes('work') ? { work: workOf(t) } : {}),
							})),
							missing: [],
							matched: matched.length,
							state_counts: stateCounts,
							offset,
							position: position >= 0 ? position : null,
							datasets: [
								...new Set(all.map((t) => t.source.where).filter((w): w is string => !!w)),
							].sort(),
						}
					: {}),
				...(include.includes('contributors')
					? {
							contributors: [...contributors.values()].sort(
								(a, b) =>
									b.submitted - a.submitted ||
									b.open - a.open ||
									b.reviewed - a.reviewed ||
									a.principal.localeCompare(b.principal),
							),
						}
					: {}),
				...(people.length > 0 ? { people: namesOf(people) } : {}),
			});
		}
		if (rest === '' && req.method === 'PATCH') {
			const refused = gate('can_manage');
			if (refused) return refused;
			if (!p) return answer({ detail: `annotation project ${id} does not exist` }, 404);
			const b = await body();
			if (['publishing', 'published', 'archived'].includes(p.state))
				return answer(
					{ detail: `project ${id} is ${p.state}: its settings are fixed once a publish starts` },
					409,
				);
			if (
				b.consensus_n !== undefined &&
				b.consensus_n !== p.consensus_n &&
				[...world.tasks.values()].some((t) => t.project_id === id)
			)
				return answer(
					{ detail: 'this project holds items: the consensus is fixed once items are sent' },
					409,
				);
			for (const key of [
				'title',
				'description',
				'instructions',
				'review_required',
				'lease_seconds',
				'consensus_n',
			] as const)
				if (b[key] !== undefined) (p as unknown as Record<string, unknown>)[key] = b[key];
			return answer(withCounts(world, p));
		}
		if (rest === '/events' && req.method === 'POST') {
			const hidden = unseen();
			if (hidden) return hidden;
			const { event } = (await body()) as { event: string };
			const edge = p ? PROJECT_EDGES[p.state]?.[event] : undefined;
			if (!p || !edge) {
				return answer(
					{
						detail: p
							? `illegal project transition: ${p.state} --${event}-->`
							: `annotation project ${id} does not exist`,
					},
					409,
				);
			}
			const refused = gate(edge[1]);
			if (refused) return refused;
			if (
				event === 'publish' &&
				[...world.tasks.values()].some((t) => t.project_id === id && !TERMINAL.has(t.state))
			)
				return answer(
					{
						detail: 'illegal project transition: frozen --publish (tasks are not all terminal)-->',
					},
					409,
				);
			p.state = event === 'publish' ? 'published' : edge[0];
			if (event === 'publish') p.publish_error = p.publish_progress = null;
			return answer(withCounts(world, p));
		}
		if (rest === '/ontology' && req.method === 'PATCH') {
			const refused = gate('can_manage');
			if (refused) return refused;
			if (!p) return answer({ detail: `annotation project ${id} does not exist` }, 404);
			if (p.state !== 'draft' && p.state !== 'labeling')
				return answer(
					{
						detail: `project ${id} is ${p.state} — the ontology is editable only in draft or labeling`,
					},
					409,
				);
			p.ontology = ((await body()).ontology as Record<string, unknown>) ?? p.ontology;
			// Every item of the project nobody has started takes it: unassigned, never held, no draft.
			for (const t of world.tasks.values())
				if (
					t.project_id === id &&
					t.state === 'unassigned' &&
					!t.assignee &&
					!world.drafts.has(t.task_id)
				)
					t.ontology = p.ontology;
			return answer(withCounts(world, p));
		}
		if (rest === '/items' && req.method === 'POST') {
			const refused = unseen() ?? gate('can_send_items');
			if (refused) return refused;
			if (!p) return answer({ detail: `annotation project ${id} does not exist` }, 409);
			if (p.state !== 'draft' && p.state !== 'labeling')
				return answer(
					{
						detail: `project ${id} is ${p.state} — items may only be sent while it is draft or labeling`,
					},
					409,
				);
			const { items } = (await body()) as {
				items: {
					source: Task['source'];
					media: { kind: string };
					prediction?: { shape_type: string; label: string }[];
				}[];
			};
			const created: string[] = [];
			for (const item of items) {
				const tid = `${id}-${crypto.randomUUID().slice(0, 8)}`;
				world.tasks.set(
					tid,
					task(id, tid, 'unassigned', item.source.keys[0] ?? '', {
						source: {
							...item.source,
							where: item.source.where ?? null,
							dataset_version: item.source.dataset_version ?? null,
						},
						media: { kind: item.media.kind, image_url: null, media_url: null },
						prediction: (item.prediction ?? []).map((s) => ({ ...s, source: 'bulk' })),
					}),
				);
				created.push(tid);
			}
			return answer({ sent: items.length, created: created.length, task_ids: created }, 201);
		}
		// The members' names keyed by the principal each grant names, as the service answers them.
		const named = (users: Iterable<string>): Record<string, string> =>
			Object.fromEntries(
				[...users].flatMap((user) => {
					const name = world.people.get(`user:${user}`);
					return name ? [[user, name]] : [];
				}),
			);
		if (rest === '/members/candidates' && req.method === 'GET') {
			// Who holds a role on a project the reader manages, known by name (`members.member_candidates`).
			const refused = gate('can_manage');
			if (refused) return refused;
			const managed = [...world.projects.keys()].filter((pid) =>
				held(world, sub, pid).includes('can_manage'),
			);
			const users = new Set(
				managed.flatMap((pid) => (world.grants.get(pid) ?? []).map((g) => g.user)),
			);
			const people = Object.entries(named(users))
				.map(([principal, name]) => ({ principal, name }))
				.sort((a, b) => a.name.localeCompare(b.name));
			return answer({ people });
		}
		const members = rest === '/members';
		if (members) {
			const grants = world.grants.get(id) ?? [];
			const list = () => ({
				members: [...grants].sort((a, b) => a.user.localeCompare(b.user)),
				grantable: [...ROLES],
				people: named(grants.map((g) => g.user)),
			});
			if (req.method === 'GET') return gate('can_manage') ?? answer(list());
			const { user, relation } =
				req.method === 'PUT'
					? ((await body()) as { user: string; relation: string })
					: {
							user: url.searchParams.get('user') ?? '',
							relation: url.searchParams.get('relation') ?? '',
						};
			const refused = gate(
				relation === 'owner' || relation === 'manager' ? 'can_administer' : 'can_manage',
			);
			if (refused) return refused;
			if (req.method === 'PUT') {
				if (!grants.some((g) => g.user === user && g.relation === relation))
					grants.push({ user, relation });
				world.grants.set(id, grants);
				return answer(list());
			}
			const admin = (g: { relation: string }) => g.relation === 'owner' || g.relation === 'manager';
			const left = grants.filter((g) => !(g.user === user && g.relation === relation));
			if (admin({ relation }) && !left.some(admin))
				return answer(
					{
						detail: `${world.people.get(`user:${user}`) ?? 'this person'} holds the only remaining ${relation} on this project — grant another owner or manager first`,
					},
					409,
				);
			world.grants.set(id, left);
			return answer({ ...list(), members: left });
		}
		const adjudication = /^\/adjudications\/([\w-]+)$/.exec(rest);
		if (adjudication && p) {
			const refused = gate('can_manage');
			if (refused) return refused;
			const group = adjudication[1] ?? '';
			if (req.method === 'PUT')
				p.adjudications[group] = {
					task_id: String((await body()).task_id),
					by: sub,
					at: new Date().toISOString(),
				};
			else delete p.adjudications[group];
			return answer(withCounts(world, p));
		}
		const drop = /^\/tasks\/([\w-]+)$/.exec(rest);
		if (drop && req.method === 'DELETE') {
			const refused = gate('can_manage');
			if (refused) return refused;
			const removed = world.tasks.delete(drop[1] ?? '');
			return answer({ task_id: drop[1], removed });
		}
	}

	const taskRead = /^\/tasks\/([\w-]+)(\/draft)?$/.exec(path);
	if (taskRead && req.method === 'GET') {
		const [, id = '', draft] = taskRead;
		const t = world.tasks.get(id);
		const perms = t ? held(world, sub, t.project_id) : [];
		if (!t || !perms.includes('can_view'))
			return answer({ detail: `No task ${id} that you can see` }, 404);
		const frozen = ['publishing', 'published', 'archived'].includes(
			world.projects.get(t.project_id)?.state ?? '',
		);
		if (!draft)
			return answer({
				...t,
				legal_events: callerEvents(t, sub, perms, frozen),
				people: namesOf(principalsOf(t)),
			});
		// A consensus replica's work is its annotator's and its reviewers' (`tasks.require_work_readable`).
		if (!mayReadWork(t, sub, perms))
			return answer(
				{
					detail:
						"you may not read this replica's work: only its annotator, or someone who may review it",
				},
				403,
			);
		const current = world.drafts.get(id);
		// No draft yet is an answer, null, as the service gives it.
		return answer(
			current
				? {
						task_id: id,
						project_id: t.project_id,
						author: current.history.at(-1)?.author ?? sub,
						shapes: current.rows.map(shapeOf),
						links: current.links,
						revision: current.revision,
						origin: 'human',
					}
				: null,
		);
	}

	const taskPath = /^\/tasks\/([\w-]+)\/(events|import)$/.exec(path);
	if (taskPath && req.method === 'POST') {
		const [, id = '', door] = taskPath;
		const t = world.tasks.get(id);
		const perms = t ? held(world, sub, t.project_id) : [];
		// An absent task and one the caller may not view answer alike (`unseen_task`).
		if (!t || !perms.includes('can_view'))
			return answer({ detail: `No task ${id} that you can see` }, 404);
		const obj = `task:${id}`;
		if (door === 'import') {
			if (!perms.includes('can_annotate')) {
				record(403);
				return refuse(sub, 'can_annotate', obj);
			}
			return answer({
				imported: 1,
				links: 0,
				draft: {
					task_id: id,
					project_id: t.project_id,
					author: sub,
					shapes: [],
					links: [],
					revision: 1,
					origin: 'import',
				},
			});
		}
		const { event, assignee, message } = (await body()) as {
			event: string;
			assignee?: string;
			message?: string;
		};
		const edge = TASK_EDGES[t.state]?.[event];
		if (!edge) {
			return answer({ detail: `illegal task transition: ${t.state} --${event}-->` }, 409);
		}
		if (!perms.includes(edge[1])) {
			record(403);
			return refuse(sub, edge[1], obj);
		}
		if (['accept', 'fix_and_accept', 'request_changes'].includes(event) && t.submitted_by === sub)
			return answer({ detail: 'a reviewer may not review their own submission' }, 403);
		if (['submit', 'skip'].includes(event) && t.assignee !== sub)
			return answer({ detail: 'another person holds this item', holder: t.assignee }, 403);
		t.state = edge[0];
		if (event === 'claim') {
			t.assignee = sub;
			t.lease_expires_at = later();
			t.holders = [...new Set([...(t.holders ?? []), sub])];
		}
		if (event === 'assign') {
			t.assignee = assignee ?? null;
			t.lease_expires_at = null;
		}
		if (event === 'release') t.assignee = null;
		if (event === 'submit') {
			t.submitted_by = sub;
			t.submitted_at = new Date().toISOString();
			t.assignee = null;
		}
		if (['accept', 'fix_and_accept', 'request_changes'].includes(event)) {
			t.reviewed_by = sub;
			t.reviewed_at = new Date().toISOString();
			t.review_action = event === 'accept' ? 'accepted' : event;
			if (message)
				t.review_notes.push({ by: sub, at: new Date().toISOString(), action: event, message });
		}
		return answer({
			...t,
			legal_events: callerEvents(t, sub, perms, false),
			people: namesOf(principalsOf(t)),
		});
	}
	return null;
}

/** Register the name a person's own token carries, as `GET /me` does (`annotator.people.register_person`). */
export const registerName = (sub: string, name: string | undefined): void => {
	if (name) worldOf(sub).people.set(sub, name);
};

/** The person's organisations, as `GET /me` answers them. */
export const orgsOf = (sub: string): { org: string; role: 'admin' | 'member' }[] =>
	roleOf(sub) === null ? [] : [{ org: 'acme', role: roleOf(sub) === 'owner' ? 'admin' : 'member' }];
