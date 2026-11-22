// The annotator's project plane, as the e2e annotator mock serves it: one world of projects, tasks and grants per
// person, seeded on their first call, so no spec sees another's writes.
//
// A person's ROLE is the first word of their name, lowercased, as `person('Manager')` mints `user:manager-…`: owner,
// manager, reviewer, annotator or viewer holds that role on every seeded project of the organisation `acme`, and any
// other name (`person('Stranger')`) belongs to no organisation and holds no role. Each door asks the role the
// annotator's does (`workbench/governed/auth/model.fga`'s ladder: owner > manager > reviewer > annotator > viewer)
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
	/** The task's own trail of transitions, each with when and by whom (`Task.transitions`). */
	transitions?: Transition[];
	/** The ontology captured onto the task at send, which the canvas reads its rules from. */
	ontology?: Record<string, unknown>;
	/** Why the item was sent, where its send named a sampling proposal (`Task.chosen`). */
	chosen?: Chosen;
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
		model_version: row.model_version ?? null,
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
		source: 'model:seed-detector',
		model_version: 'seed.1',
		confidence: 0.82,
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

/** The work of an item a judge reads (`vetting`, `appeals`): a figure alone (`-a`, `-mine`), a figure and a caption
 *  (`-b`, `-d`), nothing (`-c`); none for any other item. */
function vettedRows(id: string): Row[] {
	if (!/^(vetting|appeals)-/.test(id) || id.endsWith('-c')) return [];
	const figure = box(`${id}-fig`, 10, 10, 50, 40, 'figure');
	return /-[bd]$/.test(id) ? [figure, box(`${id}-cap`, 90, 100, 60, 40, 'caption')] : [figure];
}

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
	judge_model: string;
	sampling: Strategy;
	sampling_model: string;
}

/** A project's sampling strategy, and what sampling keeps of an item (`annotator.sampling`). */
type Strategy = 'off' | 'least_sure' | 'least_alike' | 'mix';
interface Sampling {
	task_id: string;
	kind: 'least_sure' | 'proposed_nothing';
	score: number | null;
	model: string;
	version: string;
	asked_by: string;
	scored_at: string;
}
interface Novelty {
	task_id: string;
	novelty: number;
	against: number;
	vectors: string;
	model: string | null;
	asked_by: string;
	measured_at: string;
}
interface Chosen {
	by: 'sampling' | 'person';
	kind: 'least_sure' | 'least_alike' | null;
	score: number | null;
	model: string | null;
}
/** A proposal sampling made for one sender and project (`annotator.sampling.Proposal`): the keys it chose and why. */
interface Proposal {
	sub: string;
	project_id: string;
	dataset: string;
	chosen: Map<string, Chosen>;
}

/** A judge's verdict on one submission, as the annotator keeps it (`annotator.judgement.Judgement`). */
interface Judgement {
	task_id: string;
	submitted_at: string;
	revision: number;
	model: string;
	version: string;
	judged_at: string;
	asked_by: string;
	flags: { shape_id: string | null; reason: string; suggested_label: string; score: number }[];
	settled_by: string | null;
	settled_at: string | null;
	settled_action: string | null;
	agreed: boolean | null;
}

interface World {
	role: Role | null;
	projects: Map<string, Project>;
	tasks: Map<string, Task>;
	grants: Map<string, { user: string; relation: string }[]>;
	drafts: Map<string, Draft>;
	/** The name each principal's own token carried when they last wrote (`annotator.people`). */
	people: Map<string, string>;
	/** Each submission's verdict, by task and the instant it was submitted (`annotator.state.store`'s `judgements`). */
	judgements: Map<string, Judgement>;
	/** Each item's score by a sampling model, by task (`annotator.state.store`'s `samplings`). */
	samplings: Map<string, Sampling>;
	/** Each item's novelty, by task. */
	novelties: Map<string, Novelty>;
	/** The proposals sampling made, by id. */
	proposals: Map<string, Proposal>;
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

/** A reviewer's note longer than a grid cell or a table row shows whole. */
export const SENT_BACK_NOTE =
	'Check the class: these pages are ledgers, not letters, and the stamp in the lower corner needs a box of its own before this goes back for review.';

const worlds = new Map<string, World>();

/** The stand-in judge, as the stand-in endpoint serves it (`annotator.model_endpoint.stand_in`). */
const JUDGE_MODEL = 'stand-in-judge';
const JUDGE_VERSION = 'stand-in.1';
/** What the annotator says of a project that names no judge, and of a deployment that names no model endpoint. */
const NO_JUDGE = 'this project names no judge';
const NO_ENDPOINT = 'assist is unavailable: this deployment names no model endpoint';
/** Why the stand-in judge disagrees with a label of the class it doubts (`stand_in.doubt`). */
const doubt = (label: string, instead: string): string =>
	`the stand-in judge doubts every '${label}': it would say '${instead}'`;
/** A submission's key among the verdicts: its task and the instant it was submitted. */
const verdictKey = (t: Pick<Task, 'task_id' | 'submitted_at'>): string =>
	`${t.task_id}@${t.submitted_at ?? ''}`;
/** The model the sampling projects name: the stand-in tagger, which proposes one tag of the task's first class at a
 *  fixed score (`stand_in.TAGGER_SCORE`, 0.6) on every item it is shown, so each item it scores has the same doubt. */
const SAMPLER = 'stand-in-tagger';
const TAGGER_DOUBT = 1 - 0.6;
/** What each dataset declares for sampling (`Declared.sampling.scores`, `Declared.embeddings`): a score column a
 *  pipeline wrote, by a named model, with each key's confidence; and the vectors each key has, by a named model. A
 *  dataset absent here declares neither. */
interface Declared {
	scores?: { model: string; column: string; confidence: Record<string, number> };
	embeddings?: { vectors: string; model: string; of: Record<string, number[]> };
}
const SAMPLING_DECLARED: Record<string, Declared> = {
	'lake~acme$letters': {
		scores: {
			model: SAMPLER,
			column: 'pages.confidence',
			confidence: {
				'letter-001/0': 0.79,
				'letter-001/1': 0.17,
				'letter-002/0': 0.53,
				'map-003/0': 0.38,
			},
		},
		embeddings: {
			vectors: 'pages.embedding',
			model: 'stand-in-embedder',
			of: {
				'letter-001/0': [1, 0],
				'letter-001/1': [0.8, 0.6],
				'letter-002/0': [0, 1],
				'map-003/0': [-1, 0],
				'letter-004/0': [0.6, -0.8],
			},
		},
	},
};
/** The doubt a dataset's score column states of each of ``keys`` by ``model`` (`sampling_reads.confidences`), or null
 *  when it declares no score column by that model. */
function columnDoubts(dataset: string, model: string, keys: string[]): Map<string, number> | null {
	const column = SAMPLING_DECLARED[dataset]?.scores;
	if (!column || !model || column.model !== model) return null;
	return new Map(
		keys.flatMap((key) => {
			const confidence = column.confidence[key];
			return confidence === undefined
				? []
				: [[key, 1 - Math.min(1, Math.max(0, confidence))] as const];
		}),
	);
}
/** The cosine distance between two vectors. */
function distance(a: number[], b: number[]): number {
	const dot = a.reduce((n, x, i) => n + x * (b[i] ?? 0), 0);
	const norm = (v: number[]): number => Math.hypot(...v) || 1;
	return 1 - dot / (norm(a) * norm(b));
}
/** What the annotator says when sampling cannot order a list (`annotator.sampling`). */
const SAMPLING_OFF = 'sampling is off for this project';
const NO_SAMPLING_MODEL = 'this project names no model to score its items';
const NO_SAMPLING_ENDPOINT = 'this deployment names no model endpoint';
const NOTHING_MEASURED = 'no item is measured against the labelled items yet';
const noEmbeddings = (dataset: string): string =>
	`${dataset} declares no embeddings, so diversity is unavailable for its items`;
const SCORED_STRATEGIES: readonly Strategy[] = ['least_sure', 'mix'];
const DIVERSE_STRATEGIES: readonly Strategy[] = ['least_alike', 'mix'];
/** Why ``p``'s strategy orders none of the listed items, or null (`sampling.why_unavailable`). */
function whyUnsampled(
	p: Project,
	endpoint: boolean,
	scored: number,
	measured: number,
): string | null {
	if (p.sampling === 'off') return SAMPLING_OFF;
	const byDoubt = SCORED_STRATEGIES.includes(p.sampling);
	if (byDoubt && !p.sampling_model) return NO_SAMPLING_MODEL;
	const noScore = byDoubt && scored === 0 && !endpoint ? NO_SAMPLING_ENDPOINT : null;
	const noMeasure =
		DIVERSE_STRATEGIES.includes(p.sampling) && measured === 0 ? NOTHING_MEASURED : null;
	if (p.sampling === 'mix') return noScore && noMeasure ? `${noScore}, and ${noMeasure}` : null;
	return noScore ?? noMeasure;
}
/** The unit keys of a project's labelled items of ``dataset``: submitted, in review or accepted (`labelled_units`). */
const labelledKeys = (world: World, projectId: string, dataset: string): string[] =>
	[...world.tasks.values()]
		.filter(
			(t) =>
				t.project_id === projectId &&
				t.source.where === dataset &&
				['in_review', 'accepted'].includes(t.state) &&
				!!t.submitted_at,
		)
		.map((t) => t.source.keys[0] ?? '')
		.filter(Boolean);

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
		judge_model: '',
		sampling: 'off',
		sampling_model: '',
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
	// never drawn on (`judging-e`), which a browse of the labeled work leaves out. The caption is a model's proposal its
	// annotator kept, but on `judging-b`, where it is still a proposal (one a reviewer's assist left), which keeps the
	// item from being accepted until it is kept or removed.
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
	// A grid longer than its smallest page (25): 30 items nobody holds, and one Alma Alpha sent back to the reader with a
	// note longer than a cell shows.
	projects.set('batch', project('batch', { title: 'Batch', ontology: sortingOntology }));
	for (let i = 0; i < 30; i++) {
		const id = `batch-${String(i).padStart(2, '0')}`;
		tasks.set(
			id,
			task('batch', id, 'unassigned', sortingKeys[i % 4] ?? '', { ontology: sortingOntology }),
		);
	}
	tasks.set(
		'batch-sent-back',
		task('batch', 'batch-sent-back', 'changes_requested', 'letter-002/0', {
			assignee: sub,
			holders: [sub],
			submitted_by: sub,
			reviewed_by: 'user:alpha',
			review_action: 'request_changes',
			review_notes: [
				{
					by: 'user:alpha',
					at: '2026-11-20T09:00:00Z',
					action: 'request_changes',
					message: SENT_BACK_NOTE,
				},
			],
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
	// Projects a judge reads (ADR 0026): `vetting` names the stand-in judge, which doubts every label of the last class
	// (`caption`, suggesting `figure`) and an item nobody labelled; four items in review, each another annotator's, one
	// already judged, and one the reader holds to submit. `appeals` names the stand-in that fails every ask.
	const vettingOntology = {
		kind: 'object-detection',
		modality: 'image',
		classes: [
			{ name: 'figure', tools: [], required: false, transcribe: false, attributes: [] },
			{ name: 'caption', tools: [], required: false, transcribe: false, attributes: [] },
		],
		relations: [],
		allow_empty: true,
	};
	for (const [pid, title, judge] of [
		['vetting', 'Vetting', 'stand-in-judge'],
		['appeals', 'Appeals', 'stand-in-failing'],
	] as const) {
		projects.set(pid, project(pid, { title, ontology: vettingOntology, judge_model: judge }));
		for (const [suffix, key, at] of [
			['a', 'letter-001/0', '2026-11-17T09:00:00Z'],
			['b', 'letter-001/1', '2026-11-17T10:00:00Z'],
			['c', 'letter-002/0', '2026-11-17T11:00:00Z'],
			['d', 'map-003/0', '2026-11-17T12:00:00Z'],
		] as const)
			tasks.set(
				`${pid}-${suffix}`,
				task(pid, `${pid}-${suffix}`, 'in_review', key, {
					submitted_by: 'user:alpha',
					submitted_at: at,
					holders: ['user:alpha'],
					ontology: vettingOntology,
				}),
			);
	}
	tasks.set(
		'vetting-mine',
		task('vetting', 'vetting-mine', 'claimed', 'letter-001/0', {
			assignee: sub,
			lease_expires_at: later(),
			holders: [sub],
			ontology: vettingOntology,
		}),
	);
	// Projects sampling orders (ADR 0034). `triage` samples the least sure by the stand-in tagger: four of its five open
	// items are scored by the letters dataset's score column (doubts 0.21, 0.83, 0.47, 0.62), the fifth names a page the
	// column does not score, which the tagger is asked about (doubt 0.4); its `triage-01` was sent on a proposal.
	// `blended` samples the least alike in the letters dataset's embeddings, against its one labelled item; `fresh` has
	// none labelled; `unscored` mixes the two over the maps dataset, which declares neither a score column nor
	// embeddings.
	const triageKeys = ['letter-001/0', 'letter-001/1', 'letter-002/0', 'map-003/0', 'letter-004/0'];
	projects.set(
		'triage',
		project('triage', {
			title: 'Triage',
			ontology: sortingOntology,
			sampling: 'least_sure',
			sampling_model: SAMPLER,
		}),
	);
	triageKeys.forEach((key, i) => {
		const id = `triage-0${i}`;
		tasks.set(
			id,
			task('triage', id, 'unassigned', key, {
				ontology: sortingOntology,
				...(id === 'triage-01'
					? { chosen: { by: 'sampling', kind: 'least_sure', score: 0.83, model: SAMPLER } }
					: {}),
			}),
		);
	});
	for (const [pid, title] of [
		['blended', 'Blended'],
		['fresh', 'Fresh'],
	] as const) {
		projects.set(pid, project(pid, { title, ontology: sortingOntology, sampling: 'least_alike' }));
		['letter-001/1', 'letter-002/0', 'map-003/0'].forEach((key, i) =>
			tasks.set(
				`${pid}-0${i + 1}`,
				task(pid, `${pid}-0${i + 1}`, 'unassigned', key, { ontology: sortingOntology }),
			),
		);
	}
	tasks.set(
		'blended-00',
		task('blended', 'blended-00', 'accepted', 'letter-001/0', {
			submitted_by: 'user:alpha',
			submitted_at: '2026-11-20T09:00:00Z',
			reviewed_by: 'user:reviewer-x',
			holders: ['user:alpha'],
			ontology: sortingOntology,
		}),
	);
	projects.set(
		'unscored',
		project('unscored', {
			title: 'Unscored',
			ontology: sortingOntology,
			sampling: 'mix',
			sampling_model: SAMPLER,
		}),
	);
	['map-003/0', 'letter-001/0'].forEach((key, i) =>
		tasks.set(
			`unscored-0${i}`,
			task('unscored', `unscored-0${i}`, 'unassigned', key, {
				source: { kind: 'chunks', keys: [key], where: 'lake~acme$maps', dataset_version: 1 },
				ontology: sortingOntology,
			}),
		),
	);
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
		const kept = t.project_id === 'judging' && t.task_id !== 'judging-b';
		const rows =
			REPLICA_ROWS[t.task_id] ??
			(pages
				? kept
					? PAGE_ROWS.map((r) => (r.status === 'prediction' ? { ...r, status: 'accepted' } : r))
					: PAGE_ROWS
				: []);
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
						model_version: 'vlm.3',
						confidence: 0.91,
					},
				],
			}[t.task_id] ?? [];
		const vetted = vettedRows(t.task_id);
		if (rows.length === 0 && tagged.length === 0 && vetted.length === 0) continue;
		const seeded = structuredClone([...rows, ...tagged, ...vetted]);
		drafts.set(t.task_id, {
			rows: seeded,
			links: [],
			revision: 1,
			history: [{ version: 1, timestamp: seededAt, author: 'seed', rows: seeded }],
		});
	}
	// `vetting-d` was judged when it was submitted: the judge doubted its caption.
	const judgements = new Map<string, Judgement>();
	const d = tasks.get('vetting-d');
	if (d?.submitted_at)
		judgements.set(verdictKey(d), {
			task_id: d.task_id,
			submitted_at: d.submitted_at,
			revision: 1,
			model: JUDGE_MODEL,
			version: JUDGE_VERSION,
			judged_at: d.submitted_at,
			asked_by: 'user:alpha',
			flags: [
				{
					shape_id: 'vetting-d-cap',
					reason: doubt('caption', 'figure'),
					suggested_label: 'figure',
					score: 0.8,
				},
			],
			settled_by: null,
			settled_at: null,
			settled_action: null,
			agreed: null,
		});
	return {
		role,
		projects,
		tasks,
		grants,
		drafts,
		people: new Map(Object.entries(SEEDED_NAMES)),
		judgements,
		samplings: new Map(),
		novelties: new Map(),
		proposals: new Map(),
	};
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

/** One row of a task's trail, as the store appends it (`store.fire_task`) and the wire names it (`from`, `to`). */
interface Transition {
	at: string;
	by: string;
	event: string;
	from: string;
	to: string;
}

/** Append ``event`` to ``t``'s trail, as fired by ``by`` now. */
function recordTransition(t: Task, by: string, event: string, to: string): void {
	const at = new Date().toISOString();
	t.transitions = [...(t.transitions ?? []), { at, by, event, from: t.state, to }];
}

/** What a spec changes in an item's work away from the grid: a person's `letter` tag added, or every proposal removed. */
export type WorkElsewhere = 'tag-letter' | 'remove-proposals';

/** A spec's stand-in for a save to ``taskId``'s work made away from the grid (another tab, the canvas), as one revision
 *  of its own; the grid's listing is not read again. */
export function changeWorkElsewhere(sub: string, taskId: string, change: WorkElsewhere): boolean {
	const world = worldOf(sub);
	const draft = world.drafts.get(taskId) ?? { rows: [], links: [], revision: 0, history: [] };
	const rows: Row[] =
		change === 'tag-letter'
			? [
					...draft.rows,
					{
						id: crypto.randomUUID(),
						shape_type: 'tag',
						label: 'letter',
						status: 'accepted',
						source: 'human',
					},
				]
			: draft.rows.filter((r) => r.status !== 'prediction');
	const revision = draft.revision + 1;
	const saved = { version: revision, timestamp: new Date().toISOString(), author: sub, rows };
	world.drafts.set(taskId, {
		rows,
		links: draft.links,
		revision,
		history: [...draft.history, saved].slice(-50),
	});
	return true;
}

/** Where a spec moves an item away from the grid: sent back to `sub` after they claimed and submitted it elsewhere, or
 *  open again after they claimed it and let it go. */
export type Elsewhere = 'changes_requested' | 'unassigned';

/** A spec's stand-in for ``taskId`` moved on away from the grid, its trail written as the store writes it: from open,
 *  claimed, submitted and sent back to `sub` by another reviewer (`changes_requested`); from sent back, claimed and let
 *  go (`unassigned`). The grid's listing is not read again. */
export function moveElsewhere(sub: string, taskId: string, to: Elsewhere): boolean {
	const t = worldOf(sub).tasks.get(taskId);
	const from = to === 'changes_requested' ? 'unassigned' : 'changes_requested';
	if (!t || t.state !== from) return false;
	const step = (by: string, event: string, next: string): void => {
		recordTransition(t, by, event, next);
		t.state = next;
	};
	step(sub, 'claim', 'claimed');
	if (to === 'changes_requested') {
		step(sub, 'submit', 'in_review');
		step('user:alpha', 'request_changes', 'changes_requested');
		t.submitted_by = sub;
		t.review_action = 'request_changes';
	} else step(sub, 'release', 'unassigned');
	t.assignee = null;
	t.lease_expires_at = null;
	return true;
}

/** A spec's stand-in for a claim of ``taskId`` that `sub` let go and took again away from the grid (another tab, the
 *  canvas, a lapsed lease claimed again): its release and a new claim on the trail, a new lease, the work untouched. */
export function reclaimElsewhere(sub: string, taskId: string): boolean {
	const t = worldOf(sub).tasks.get(taskId);
	if (!t || t.state !== 'claimed' || t.assignee !== sub) return false;
	recordTransition(t, sub, 'release', 'unassigned');
	t.state = 'unassigned';
	recordTransition(t, sub, 'claim', 'claimed');
	t.state = 'claimed';
	t.lease_expires_at = later();
	return true;
}

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

/** What a spec plants on one path for one person: a failure, a dataset not built, a slow answer, a session that ended
 *  (`unauthorized` on every call, `unauthorized-write` on a write alone), on `/api/assist/models` a deployment that
 *  names no model endpoint (`no-endpoint`), or on one unit's `/api/assist/<unit>` an endpoint busy for it (`busy`) or
 *  a model that answers it a shape its classes may not take, beside what it found (`mixed`) or in its place
 *  (`polygon`), on one unit's `/api/annotations/<unit>` another save landing on top of a save (`moved-after`), or on
 *  one unit's `/api/judge/<unit>` an item that left review while its judge was asked (`left-review`). */
const FAULTS = [
	'fail',
	'unbuilt',
	'slow',
	'unauthorized',
	'unauthorized-write',
	'no-endpoint',
	'busy',
	'mixed',
	'polygon',
	'moved-after',
	'left-review',
] as const;
export type Fault = (typeof FAULTS)[number];

// Widened to look a string up: `includes` on the literal tuple takes only its own members.
export const isFault = (kind: string | null): kind is Fault =>
	kind !== null && (FAULTS as readonly string[]).includes(kind);

/** Who made a call to the annotator and how the mock answers it: `record(status)` writes the call into the ledger,
 *  `fault` is the spec's switch for this person and path, and `endpoint` false is a deployment that names no model
 *  endpoint (the spec's `no-endpoint` on `/api/assist/models`). */
export interface CallOf<F extends string> {
	sub: string;
	record: (status: number) => void;
	fault: F | undefined;
	endpoint: boolean;
}

/** Answer one call to the project plane, or null when the path is not one of its doors; a listing's judge says what
 *  `endpoint` false means. */
export async function projectsDoor(
	req: Request,
	url: URL,
	{ sub, record, fault, name, endpoint }: CallOf<'fail' | 'unbuilt' | 'slow'> & { name?: string },
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
			// What sampling keeps of an item (`task_query.samplings_of`): its score by the project's current sampling model,
			// and its novelty; `scored` and `measured` filter by whether it has each.
			const strategy: Strategy = p?.sampling ?? 'off';
			const scoreOf = (t: Task): Sampling | undefined => {
				const kept = world.samplings.get(t.task_id);
				return kept && kept.model === p?.sampling_model ? kept : undefined;
			};
			const noveltyOf = (t: Task): Novelty | undefined => world.novelties.get(t.task_id);
			const has = (name: 'scored' | 'measured', t: Task): boolean => {
				const asked = q.get(name);
				if (asked === null) return true;
				return (name === 'scored' ? !!scoreOf(t) : !!noveltyOf(t)) === (asked === 'true');
			};
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
				has('scored', t) &&
				has('measured', t) &&
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
					proposals: (draft?.rows ?? []).filter((r) => r.status === 'prediction').length,
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
			// Where an item sits in the judge's order (`task_query._JUDGE_RANK`): flagged, unread, clear.
			const verdictOf = (t: Task): Judgement | undefined => world.judgements.get(verdictKey(t));
			const rankOf = (t: Task): number => {
				const verdict = verdictOf(t);
				return verdict === undefined ? 1 : verdict.flags.length > 0 ? 0 : 2;
			};
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
					if (sort === 'judge') {
						const rank = (rankOf(a) - rankOf(b)) * (desc ? -1 : 1);
						if (rank !== 0) return rank;
						const [sa, sb] = [a.submitted_at ?? null, b.submitted_at ?? null];
						if (sa !== sb) return sa === null ? 1 : sb === null ? -1 : sa < sb ? -1 : 1;
						return a.task_id.localeCompare(b.task_id);
					}
					if (ka !== kb) {
						if (ka === null) return 1;
						if (kb === null) return -1;
						return (ka < kb ? -1 : 1) * (desc ? -1 : 1);
					}
					return a.task_id.localeCompare(b.task_id);
				});
			// The strategy's order (`task_query._SAMPLING_ORDER`): a rank that groups the items, which `order=desc` flips, and
			// an order within the group. Least sure: a doubt, then scored with none (the model proposed nothing), then
			// unscored, by doubt; least alike: measured, then not, by novelty; mix: an item's place is the earlier of its
			// odd place in the doubt order and its even place in the novelty order, over the matched items (`_MIX_PLACE`),
			// and an item with neither number is in the last group. Ties by task id. Which number placed it: `_PLACED_BY`.
			const placed = new Map<string, 'least_sure' | 'least_alike'>();
			if (sort === 'sampling' && strategy !== 'off') {
				const doubt = (t: Task): number | null => scoreOf(t)?.score ?? null;
				const novelty = (t: Task): number | null => noveltyOf(t)?.novelty ?? null;
				const rankedBy = (value: (t: Task) => number | null): Map<string, number> =>
					new Map(
						matched
							.filter((t) => value(t) !== null)
							.sort(
								(a, b) => (value(b) ?? 0) - (value(a) ?? 0) || a.task_id.localeCompare(b.task_id),
							)
							.map((t, i) => [t.task_id, i + 1]),
					);
				const [byDoubt, byNovelty] = [rankedBy(doubt), rankedBy(novelty)];
				const mixPlace = (t: Task): number | null => {
					const places = [
						byDoubt.has(t.task_id) ? 2 * (byDoubt.get(t.task_id) ?? 0) - 1 : null,
						byNovelty.has(t.task_id) ? 2 * (byNovelty.get(t.task_id) ?? 0) : null,
					].filter((n): n is number => n !== null);
					return places.length > 0 ? Math.min(...places) : null;
				};
				const groupOf = (t: Task): number =>
					strategy === 'least_sure'
						? doubt(t) !== null
							? 0
							: scoreOf(t)
								? 1
								: 2
						: strategy === 'least_alike'
							? novelty(t) !== null
								? 0
								: 1
							: mixPlace(t) !== null
								? 0
								: 1;
				const within = (t: Task): number =>
					strategy === 'least_sure'
						? -(doubt(t) ?? Number.NEGATIVE_INFINITY)
						: strategy === 'least_alike'
							? -(novelty(t) ?? Number.NEGATIVE_INFINITY)
							: (mixPlace(t) ?? Number.POSITIVE_INFINITY);
				matched.sort(
					(a, b) =>
						(groupOf(a) - groupOf(b)) * (desc ? -1 : 1) ||
						(within(a) === within(b) ? 0 : within(a) < within(b) ? -1 : 1) ||
						a.task_id.localeCompare(b.task_id),
				);
				for (const t of matched) {
					const place = strategy === 'mix' ? mixPlace(t) : null;
					const by =
						strategy === 'least_sure'
							? doubt(t) !== null
								? 'least_sure'
								: null
							: strategy === 'least_alike'
								? novelty(t) !== null
									? 'least_alike'
									: null
								: place === null
									? null
									: place % 2 === 1
										? 'least_sure'
										: 'least_alike';
					if (by !== null) placed.set(t.task_id, by);
				}
			}
			const samplingOf = (t: Task) => {
				const [score, novelty] = [scoreOf(t), noveltyOf(t)];
				return score || novelty
					? {
							score: score ?? null,
							novelty: novelty ?? null,
							placed_by: placed.get(t.task_id) ?? null,
						}
					: null;
			};
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
								...(include.includes('judge')
									? { judgement: mayReadWork(t, sub, perms) ? (verdictOf(t) ?? null) : null }
									: {}),
								...(include.includes('sampling') ? { sampling: samplingOf(t) } : {}),
							})),
							missing: [],
							matched: matched.length,
							state_counts: stateCounts,
							offset,
							position: position >= 0 ? position : null,
							datasets: [
								...new Set(all.map((t) => t.source.where).filter((w): w is string => !!w)),
							].sort(),
							...(include.includes('judge')
								? {
										judge: {
											model: p?.judge_model ?? '',
											available: !!p?.judge_model && endpoint,
											reason: !p?.judge_model ? NO_JUDGE : endpoint ? '' : NO_ENDPOINT,
											flagged: matched.filter((t) => rankOf(t) === 0).length,
											clear: matched.filter((t) => rankOf(t) === 2).length,
											unread: matched.filter((t) => rankOf(t) === 1).length,
										},
									}
								: {}),
							...(include.includes('sampling') && p
								? (() => {
										const scored = matched.filter((t) => scoreOf(t)).length;
										const measured = matched.filter((t) => noveltyOf(t)).length;
										const why = whyUnsampled(p, endpoint, scored, measured);
										return {
											sampling: {
												strategy,
												model: p.sampling_model,
												available: why === null,
												reason: why ?? '',
												asks:
													endpoint && !!p.sampling_model && SCORED_STRATEGIES.includes(strategy),
												scored,
												unscored: matched.length - scored,
												measured,
												unmeasured: matched.length - measured,
											},
										};
									})()
								: {}),
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
				'judge_model',
				'sampling',
				'sampling_model',
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
		// A refusal of a sampling door as the annotator words it (`problem+json`).
		const problem = (type: string, status: number, detail: string): Response => {
			record(status);
			return new Response(JSON.stringify({ type: `about:blank#${type}`, status, detail }), {
				status,
				headers: { 'content-type': 'application/problem+json' },
			});
		};
		if (rest === '/sampling/reads' && req.method === 'POST') {
			// `endpoints.sampling.read_samples`: per dataset, the score column's doubt of each item the project's model has
			// not scored, each item's distance from the labelled ones (measured again), the items a model is still to be
			// asked about, and what could not be read. It asks no model; it refuses only a project whose sampling is off.
			const refused = unseen() ?? gate('can_annotate');
			if (refused) return refused;
			if (!p) return answer({ detail: `annotation project ${id} does not exist` }, 404);
			if (p.sampling === 'off') return problem('no-sampling', 409, SAMPLING_OFF);
			const ids = ((await body()).task_ids ?? []) as string[];
			if (!Array.isArray(ids) || ids.length < 1 || ids.length > 50)
				return answer({ detail: 'task_ids takes 1 to 50 items' }, 422);
			const read = ids
				.map((tid) => world.tasks.get(tid))
				.filter(
					(t): t is Task =>
						t !== undefined && t.project_id === id && !!t.source.where && !!t.source.keys[0],
				);
			const byDoubt = SCORED_STRATEGIES.includes(p.sampling);
			const byDiversity = DIVERSE_STRATEGIES.includes(p.sampling);
			const keyOf = (t: Task): string => t.source.keys[0] ?? '';
			const unscored = new Set(
				read
					.filter((t) => {
						const kept = world.samplings.get(t.task_id);
						return byDoubt && !(kept && kept.model === p.sampling_model);
					})
					.map((t) => t.task_id),
			);
			const out = {
				scored: [] as Sampling[],
				measured: [] as Novelty[],
				ask: [] as string[],
				notes: [] as string[],
			};
			const groups = [...Map.groupBy(read, (t) => t.source.where ?? '')].sort(([a], [b]) =>
				a.localeCompare(b),
			);
			for (const [dataset, held] of groups) {
				const fromColumn = new Set<string>();
				const doubts = byDoubt
					? columnDoubts(
							dataset,
							p.sampling_model,
							held.filter((t) => unscored.has(t.task_id)).map(keyOf),
						)
					: null;
				for (const t of held)
					if (unscored.has(t.task_id) && doubts?.has(keyOf(t))) {
						const score: Sampling = {
							task_id: t.task_id,
							kind: 'least_sure',
							score: doubts.get(keyOf(t)) ?? null,
							model: p.sampling_model,
							version: `${SAMPLING_DECLARED[dataset]?.scores?.column} at version 3`,
							asked_by: sub,
							scored_at: new Date().toISOString(),
						};
						world.samplings.set(t.task_id, score);
						out.scored.push(score);
						fromColumn.add(t.task_id);
					}
				if (byDiversity) {
					const vectors = SAMPLING_DECLARED[dataset]?.embeddings;
					const labelled = labelledKeys(world, id, dataset).filter((key) => vectors?.of[key]);
					if (!vectors) out.notes.push(noEmbeddings(dataset));
					else if (labelled.length === 0)
						out.notes.push(
							`no item of ${dataset} is labelled yet, so there is nothing to measure its items against`,
						);
					else
						for (const t of held) {
							const mine = vectors.of[keyOf(t)];
							const others = labelled.filter((key) => key !== keyOf(t));
							if (!mine || others.length === 0) continue;
							const novelty: Novelty = {
								task_id: t.task_id,
								novelty: Math.min(...others.map((key) => distance(mine, vectors.of[key] ?? []))),
								against: others.length,
								vectors: vectors.vectors,
								model: vectors.model,
								asked_by: sub,
								measured_at: new Date().toISOString(),
							};
							world.novelties.set(t.task_id, novelty);
							out.measured.push(novelty);
						}
				}
				if (byDoubt) {
					const left = held.filter((t) => unscored.has(t.task_id) && !fromColumn.has(t.task_id));
					const said: string[] = [];
					if (left.length > 0 && !p.sampling_model) said.push(NO_SAMPLING_MODEL);
					else if (left.length > 0 && !endpoint)
						said.push(
							`${NO_SAMPLING_ENDPOINT}, so ${left.length} item(s) of ${dataset} with no score by ${p.sampling_model} in their dataset stay unscored`,
						);
					else if (left.length > 0) {
						const pictured = left.filter((t) => t.media.kind === 'image');
						out.ask.push(...pictured.map((t) => t.task_id));
						if (pictured.length < left.length)
							said.push(
								`a sampling model is shown a picture, so ${left.length - pictured.length} item(s) of ${dataset} that are none stay unscored`,
							);
					}
					out.notes.push(...said.filter((note) => !out.notes.includes(note)));
				}
			}
			return answer(out);
		}
		if (rest === '/sampling/proposals' && req.method === 'POST') {
			// `endpoints.sampling.propose_items`: least sure from the dataset's score column by the project's model alone
			// (no model is asked at send), diversity from its embeddings (k-center greedy against the labelled and the
			// chosen), taken in turn; a project that ranks by doubt on a dataset with no such column ranks by diversity.
			const refused = unseen() ?? gate('can_send_items');
			if (refused) return refused;
			if (!p) return answer({ detail: `annotation project ${id} does not exist` }, 404);
			if (p.sampling === 'off') return problem('no-sampling', 409, SAMPLING_OFF);
			const b = (await body()) as { dataset?: string; keys?: string[]; count?: number };
			const keys = [...new Set(b.keys ?? [])];
			const count = Number(b.count ?? 0);
			if (keys.length < 1 || keys.length > 1000 || !(count >= 1 && count <= 1000))
				return answer({ detail: 'keys takes 1 to 1000, count 1 to 1000' }, 422);
			const dataset = String(b.dataset ?? '');
			const notes: string[] = [];
			const byDoubt = SCORED_STRATEGIES.includes(p.sampling);
			const doubts = byDoubt ? columnDoubts(dataset, p.sampling_model, keys) : null;
			if (byDoubt && doubts === null)
				notes.push(
					`${p.sampling_model ? `${dataset} carries no score by ${p.sampling_model}` : NO_SAMPLING_MODEL}: least sure orders these items once they are in the project, and they are proposed by how unlike the labelled items they are`,
				);
			const declared = SAMPLING_DECLARED[dataset]?.embeddings;
			if (!declared) notes.push(noEmbeddings(dataset));
			const vectors = new Map(
				keys.flatMap((key) => (declared?.of[key] ? [[key, declared.of[key]] as const] : [])),
			);
			const labelled = labelledKeys(world, id, dataset).flatMap((key) =>
				declared?.of[key] ? [declared.of[key]] : [],
			);
			const lanes: ('least_sure' | 'least_alike')[] = doubts !== null ? ['least_sure'] : [];
			if (DIVERSE_STRATEGIES.includes(p.sampling) || doubts === null) lanes.push('least_alike');
			const chosen: {
				key: string;
				kind: 'least_sure' | 'least_alike';
				score: number;
				model: string | null;
			}[] = [];
			const taken = new Set<string>();
			const byDoubtOrder = [...(doubts ?? new Map<string, number>())].sort(
				(x, y) => y[1] - x[1] || x[0].localeCompare(y[0]),
			);
			const nearest = (key: string): number => {
				const from = [
					...labelled,
					...[...taken].flatMap((k) => (vectors.get(k) ? [vectors.get(k) ?? []] : [])),
				];
				const mine = vectors.get(key) ?? [];
				return from.length === 0 ? 1 : Math.min(...from.map((v) => distance(mine, v)));
			};
			const next = (lane: 'least_sure' | 'least_alike') => {
				if (lane === 'least_sure') {
					const hit = byDoubtOrder.find(([key]) => !taken.has(key));
					return hit ? { key: hit[0], kind: lane, score: hit[1], model: p.sampling_model } : null;
				}
				const open = [...vectors.keys()].filter((key) => !taken.has(key));
				if (open.length === 0) return null;
				const far = open
					.map((key) => [key, nearest(key)] as const)
					.sort((x, y) => y[1] - x[1] || x[0].localeCompare(y[0]))[0];
				return far
					? { key: far[0], kind: lane, score: far[1], model: declared?.model ?? null }
					: null;
			};
			const open = [...lanes];
			while (open.length > 0 && chosen.length < count)
				for (const lane of open.slice()) {
					if (chosen.length >= count) break;
					const item = next(lane);
					if (item === null) open.splice(open.indexOf(lane), 1);
					else {
						taken.add(item.key);
						chosen.push(item);
					}
				}
			const proposalId = `proposal-${crypto.randomUUID().slice(0, 8)}`;
			world.proposals.set(proposalId, {
				sub,
				project_id: id,
				dataset,
				chosen: new Map(
					chosen.map((c) => [
						c.key,
						{ by: 'sampling', kind: c.kind, score: c.score, model: c.model },
					]),
				),
			});
			return answer({
				proposal_id: proposalId,
				project_id: id,
				dataset,
				proposed_by: sub,
				strategy: p.sampling,
				chosen,
				notes,
			});
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
			const { items, proposal: proposalId } = (await body()) as {
				items: {
					source: Task['source'];
					media: { kind: string };
					prediction?: { shape_type: string; label: string }[];
				}[];
				proposal?: string;
			};
			// Each item is stamped with who chose it (`project_events._chosen`): sampling, for an item the proposal the send
			// names chose, and the sender for every other (`sampling.NoSuchProposal` for a proposal not the sender's here).
			const proposal = proposalId === undefined ? undefined : world.proposals.get(proposalId);
			if (proposalId !== undefined && (proposal?.sub !== sub || proposal.project_id !== id))
				return problem(
					'no-such-proposal',
					409,
					`nothing was sent: ${proposalId} is not a proposal you were given for this project, or it is more than a day old. Ask for the proposal again`,
				);
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
						chosen: (item.source.where === proposal?.dataset
							? proposal?.chosen.get(item.source.keys[0] ?? '')
							: undefined) ?? {
							by: 'person',
							kind: null,
							score: null,
							model: null,
						},
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
		const { event, assignee, message, judge_agreed } = (await body()) as {
			event: string;
			assignee?: string;
			message?: string;
			judge_agreed?: boolean;
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
		// A proposal is a model's, and a submission or an acceptance is a person's: each is kept or removed first
		// (ADR 0019, `machines.SETTLES_PROPOSALS`).
		const unsettled = (world.drafts.get(id)?.rows ?? []).filter(
			(r) => r.status === 'prediction',
		).length;
		if (['submit', 'accept', 'fix_and_accept'].includes(event) && unsettled > 0) {
			const doing = event === 'submit' ? 'submitting' : 'accepting';
			return answer(
				{
					detail:
						unsettled === 1
							? `1 proposal on this item is neither kept nor removed: keep or remove it before ${doing}`
							: `${unsettled} proposals on this item are neither kept nor removed: keep or remove each before ${doing}`,
				},
				409,
			);
		}
		// A review decision settles the submission's verdict: whether the reviewer agreed with its flags, as they said or
		// as the decision says (`judgement.agreed_with`); saying it of an item with no flag is refused (`store`).
		const verdict = world.judgements.get(verdictKey(t));
		const decides = ['accept', 'fix_and_accept', 'request_changes'].includes(event);
		if (judge_agreed !== undefined && !(decides && verdict && verdict.flags.length > 0))
			return answer(
				{
					detail: decides
						? 'this submission carries no flag: there is nothing to agree with'
						: `${event} is no review decision: only one says whether a judge's flag was right`,
				},
				409,
			);
		if (decides && verdict)
			Object.assign(verdict, {
				settled_by: sub,
				settled_at: new Date().toISOString(),
				settled_action: event === 'accept' ? 'accepted' : event,
				agreed: verdict.flags.length > 0 ? (judge_agreed ?? event !== 'accept') : null,
			});
		recordTransition(t, sub, event, edge[0]);
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

/** Answer one call to the judge door (`POST /api/judge/<unit key>`, `annotator.api.v1.endpoints.judge`), or null when the
 *  path is not that door. It refuses in the service's order: the item unseen or not this unit's (404), a caller who
 *  neither submitted it nor may review it (403), an item not in review or a project naming no judge (409), no model
 *  endpoint (503), then the endpoint's own failure; an item already judged answers its kept verdict. The stand-in judge
 *  doubts every label of the task's last class and an item nobody labelled; `stand-in-failing` fails every ask, as does
 *  any judge on a unit a spec planted `fail` on, and a unit a spec planted `left-review` on is refused as an item that
 *  left review while it was asked. */
export type JudgeFault = 'fail' | 'left-review';
export async function judgeDoor(
	req: Request,
	url: URL,
	{ sub, record, fault, endpoint }: CallOf<JudgeFault>,
): Promise<Response | null> {
	if (!url.pathname.startsWith('/api/judge/') || req.method !== 'POST') return null;
	const answer = (body: unknown, status = 200): Response => {
		record(status);
		return new Response(JSON.stringify(body), {
			status,
			headers: { 'content-type': status < 400 ? 'application/json' : 'application/problem+json' },
		});
	};
	const world = worldOf(sub);
	const id = url.searchParams.get('task') ?? '';
	const t = world.tasks.get(id);
	const perms = t ? held(world, sub, t.project_id) : [];
	if (!t || t.project_id !== url.searchParams.get('project') || !perms.includes('can_view'))
		return answer({ detail: `No task ${id} that you can see` }, 404);
	const key = decodeURIComponent(url.pathname.slice('/api/judge/'.length));
	if (t.source.keys[0] !== key || t.source.where !== url.searchParams.get('dataset'))
		return answer({ detail: `unit ${key} is not this item's` }, 404);
	if (t.submitted_by !== sub && !perms.includes('can_review')) {
		record(403);
		return refuse(sub, 'can_review', `project:${t.project_id}`);
	}
	const problem = (type: string, status: number, detail: string): Response =>
		answer({ type: `about:blank#${type}`, status, detail }, status);
	if (t.state !== 'in_review' || !t.submitted_at)
		return problem('conflict', 409, `this item is ${t.state}: a judge reads an item in review`);
	const kept = world.judgements.get(verdictKey(t));
	if (kept) return answer(kept);
	const model = world.projects.get(t.project_id)?.judge_model ?? '';
	if (!model) return problem('no-judge', 409, NO_JUDGE);
	if (!endpoint) return problem('no-model-endpoint', 503, NO_ENDPOINT);
	if (fault === 'left-review')
		return problem(
			'conflict',
			409,
			'this item left review while its judge was asked: the verdict is not kept',
		);
	if (model === 'stand-in-failing' || fault === 'fail')
		return problem(
			'model-endpoint-failed',
			502,
			model === 'stand-in-failing'
				? 'the model endpoint failed (500): stand-in-failing fails every ask'
				: 'the model endpoint failed (500)',
		);
	const draft = world.drafts.get(id);
	const flags = standInFlags(
		(draft?.rows ?? []).filter((r) => r.status !== 'prediction'),
		classNames(t),
	);
	const verdict: Judgement = {
		task_id: id,
		submitted_at: t.submitted_at,
		revision: draft?.revision ?? 0,
		model,
		version: JUDGE_VERSION,
		judged_at: new Date().toISOString(),
		asked_by: sub,
		flags,
		settled_by: null,
		settled_at: null,
		settled_action: null,
		agreed: null,
	};
	world.judgements.set(verdictKey(t), verdict);
	return answer(verdict);
}

/** Answer one call to the sampling door (`POST /api/sampling/<unit key>`, `endpoints.sampling.sample`), or null when
 *  the path is not that door. It refuses in the service's order: the item unseen or not this unit's (404), a caller who
 *  may not annotate (403), a strategy that ranks by no score or names no model (409 `no-sampling`), then an item the
 *  model already scored answers its kept score, then no model endpoint (503), an item that is no picture (422), and the
 *  endpoint's own failure (a unit a spec planted `fail` on). The stand-in tagger proposes a tag of the task's first
 *  class at 0.6 on every item, so an item whose first class takes a tag has doubt 0.4, and any other none. */
export async function samplingDoor(
	req: Request,
	url: URL,
	{ sub, record, fault, endpoint }: CallOf<'fail'>,
): Promise<Response | null> {
	if (!url.pathname.startsWith('/api/sampling/') || req.method !== 'POST') return null;
	const answer = (body: unknown, status = 200): Response => {
		record(status);
		return new Response(JSON.stringify(body), {
			status,
			headers: { 'content-type': status < 400 ? 'application/json' : 'application/problem+json' },
		});
	};
	const problem = (type: string, status: number, detail: string): Response =>
		answer({ type: `about:blank#${type}`, status, detail }, status);
	const world = worldOf(sub);
	const id = url.searchParams.get('task') ?? '';
	const t = world.tasks.get(id);
	const perms = t ? held(world, sub, t.project_id) : [];
	if (!t || t.project_id !== url.searchParams.get('project') || !perms.includes('can_view'))
		return answer({ detail: `No task ${id} that you can see` }, 404);
	const key = decodeURIComponent(url.pathname.slice('/api/sampling/'.length));
	if (t.source.keys[0] !== key || t.source.where !== url.searchParams.get('dataset'))
		return answer({ detail: `unit ${key} is not this item's` }, 404);
	if (!perms.includes('can_annotate')) {
		record(403);
		return refuse(sub, 'can_annotate', `project:${t.project_id}`);
	}
	const p = world.projects.get(t.project_id);
	if (!p || !SCORED_STRATEGIES.includes(p.sampling))
		return problem('no-sampling', 409, SAMPLING_OFF);
	if (!p.sampling_model) return problem('no-sampling', 409, NO_SAMPLING_MODEL);
	const kept = world.samplings.get(id);
	if (kept && kept.model === p.sampling_model) return answer(kept);
	if (!endpoint) return problem('no-model-endpoint', 503, NO_SAMPLING_ENDPOINT);
	if (t.media.kind !== 'image')
		return problem(
			'model-refused-ask',
			422,
			`a sampling model is shown a picture, and this item is ${t.media.kind}`,
		);
	if (fault === 'fail')
		return problem('model-endpoint-failed', 502, 'the model endpoint failed (500)');
	const first = (t.ontology?.classes as { tools?: string[] }[] | undefined)?.[0];
	const takesTag =
		first !== undefined &&
		((first.tools ?? []).length === 0 || (first.tools ?? []).includes('tag'));
	const score: Sampling = {
		task_id: id,
		kind: takesTag ? 'least_sure' : 'proposed_nothing',
		score: takesTag ? TAGGER_DOUBT : null,
		model: p.sampling_model,
		version: JUDGE_VERSION,
		asked_by: sub,
		scored_at: new Date().toISOString(),
	};
	world.samplings.set(id, score);
	return answer(score);
}

/** The names of a task's classes, as its captured ontology lists them. */
const classNames = (t: Task): string[] => {
	const classes = t.ontology?.classes;
	return Array.isArray(classes)
		? classes.flatMap((c: unknown) =>
				c && typeof c === 'object' && 'name' in c ? [String(c.name)] : [],
			)
		: [];
};

/** What the stand-in judge disagrees with (`stand_in._judge`): an item nobody labelled, and every label of the last of
 *  two or more classes, which it would have given the first. */
function standInFlags(labels: Row[], classes: string[]): Judgement['flags'] {
	if (labels.length === 0)
		return [
			{
				shape_id: null,
				reason: 'nothing is labelled on this item',
				suggested_label: '',
				score: 0.5,
			},
		];
	const [first, doubted] = [classes[0], classes.at(-1)];
	if (classes.length < 2 || first === undefined || doubted === undefined) return [];
	return labels
		.filter((r) => r.label === doubted)
		.map((r) => ({
			shape_id: r.id,
			reason: doubt(doubted, first),
			suggested_label: first,
			score: 0.8,
		}));
}

/** Register the name a person's own token carries, as `GET /me` does (`annotator.people.register_person`). */
export const registerName = (sub: string, name: string | undefined): void => {
	if (name) worldOf(sub).people.set(sub, name);
};

/** The person's organisations, as `GET /me` answers them. */
export const orgsOf = (sub: string): { org: string; role: 'admin' | 'member' }[] =>
	roleOf(sub) === null ? [] : [{ org: 'acme', role: roleOf(sub) === 'owner' ? 'admin' : 'member' }];
