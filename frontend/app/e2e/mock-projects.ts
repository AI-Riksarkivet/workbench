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
	reviewed_by: string | null;
	review_action: string | null;
	prediction: { shape_type: string; label: string; source: string }[];
	review_notes: { by: string; at: string; action: string; message: string }[];
	/** The ontology captured onto the task at send, which the canvas reads its rules from. */
	ontology?: Record<string, unknown>;
}

/** A task's draft: the shapes and links the publish reads, guarded by its revision. */
interface Draft {
	shapes: Record<string, unknown>[];
	links: { name: string; from_shape: string; to_shape: string }[];
	revision: number;
}

interface Project {
	project_id: string;
	tenant: string;
	slug: string;
	title: string;
	description: string;
	instructions: string;
	state: string;
	review_required: boolean;
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
}

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
	const grants = new Map<string, { user: string; relation: string }[]>();
	for (const id of projects.keys())
		grants.set(id, role ? [{ user: sub.replace(/^user:/, ''), relation: role }] : []);
	return { role, projects, tasks, grants, drafts: new Map() };
}

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

const refuse = (sub: string, relation: string, obj: string): Response =>
	json({ detail: `${sub.replace(/^user:/, '')} lacks ${relation} on ${obj}` }, 403);

/** Answer one call to the project plane, or null when the path is not one of its doors. `record(status)` writes the
 *  call into the ledger; `fault` is the spec's switch for this person and path. */
export async function projectsDoor(
	req: Request,
	url: URL,
	sub: string,
	record: (status: number) => void,
	fault: 'fail' | 'unbuilt' | 'slow' | undefined,
): Promise<Response | null> {
	const path = url.pathname;
	if (!path.startsWith('/projects') && !path.startsWith('/tasks/')) return null;
	const world = worldOf(sub);
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
			const states = Object.fromEntries(all.map((t) => [t.task_id, t.state]));
			const terminal = all.filter((t) => TERMINAL.has(t.state)).length;
			const frozen = p !== undefined && ['publishing', 'published', 'archived'].includes(p.state);
			return answer({
				tasks: states,
				counts: counts(world, id),
				total: all.length,
				terminal,
				may_publish: all.length > 0 && terminal === all.length,
				...(url.searchParams.get('include') === 'details'
					? {
							details: all.map((t) => ({ ...t, legal_events: legalTask(t.state, frozen) })),
							missing: [],
							next_cursor: null,
						}
					: {}),
			});
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
		const members = rest === '/members';
		if (members) {
			const grants = world.grants.get(id) ?? [];
			const list = () => ({
				members: [...grants].sort((a, b) => a.user.localeCompare(b.user)),
				grantable: [...ROLES],
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
						detail: `${user} holds the only remaining ${relation} on this project — grant another owner or manager first`,
					},
					409,
				);
			world.grants.set(id, left);
			return answer({ members: left, grantable: [...ROLES] });
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
	if (taskRead && (req.method === 'GET' || (taskRead[2] && req.method === 'PUT'))) {
		const [, id = '', draft] = taskRead;
		const t = world.tasks.get(id);
		const perms = t ? held(world, sub, t.project_id) : [];
		if (!t || !perms.includes('can_view'))
			return answer({ detail: `No task ${id} that you can see` }, 404);
		const frozen = ['publishing', 'published', 'archived'].includes(
			world.projects.get(t.project_id)?.state ?? '',
		);
		if (!draft) return answer({ ...t, legal_events: legalTask(t.state, frozen) });
		const current = world.drafts.get(id);
		const doc = (d: Draft) => ({
			task_id: id,
			project_id: t.project_id,
			author: sub,
			...d,
			origin: 'human',
		});
		// No draft yet is an answer, null, as the service gives it.
		if (req.method === 'GET') return answer(current ? doc(current) : null);
		if (!perms.includes('can_annotate')) {
			record(403);
			return refuse(sub, 'can_annotate', `task:${id}`);
		}
		if (t.state !== 'claimed' || t.assignee !== sub)
			return answer({ detail: `the task is held by ${t.assignee}` }, 409);
		const b = (await body()) as Partial<Draft> & { base_revision?: number };
		if ((b.base_revision ?? null) !== (current?.revision ?? null))
			return answer({ detail: 'the draft moved: re-read it before saving' }, 409);
		const next: Draft = {
			shapes: b.shapes ?? [],
			links: b.links ?? [],
			revision: (current?.revision ?? 0) + 1,
		};
		world.drafts.set(id, next);
		return answer(doc(next));
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
			return answer({ detail: `the task is held by ${t.assignee}` }, 403);
		t.state = edge[0];
		if (event === 'claim') {
			t.assignee = sub;
			t.lease_expires_at = later();
		}
		if (event === 'assign') {
			t.assignee = assignee ?? null;
			t.lease_expires_at = null;
		}
		if (event === 'release') t.assignee = null;
		if (event === 'submit') t.submitted_by = sub;
		if (['accept', 'fix_and_accept', 'request_changes'].includes(event)) {
			t.reviewed_by = sub;
			t.review_action = event === 'accept' ? 'accepted' : event;
			if (message)
				t.review_notes.push({ by: sub, at: new Date().toISOString(), action: event, message });
		}
		return answer({ ...t, legal_events: legalTask(t.state, false) });
	}
	return null;
}

/** The person's organisations, as `GET /me` answers them. */
export const orgsOf = (sub: string): { org: string; role: 'admin' | 'member' }[] =>
	roleOf(sub) === null ? [] : [{ org: 'acme', role: roleOf(sub) === 'owner' ? 'admin' : 'member' }];
