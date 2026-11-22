/**
 * The annotator's project plane, as the browser calls it: projects, their members, events, adjudications and items
 * (`/projects…`), and one task's events and import (`/tasks…`), each same-origin at the annotator's public base on the
 * DPoP seam (`client.ts`). Every door asks the project's role for its action in the workbench's own store, and a
 * refusal comes back as the door's own words, which the pages show as they are.
 *
 * WHICH PROJECT A CALL IS FOR. A project door names the project in its path (`/projects/<id>/…`): that is the object
 * its role is checked on. A task door names only the task: the annotator reads the task's project from the task's own
 * record, so a caller cannot choose the project its permission is evaluated against, and no `?project=` is sent there.
 * The annotation, tag, version and assist doors are the ones that take `?project=` (D3b's canvas).
 *
 * The project doors take no `Idempotency-Key`, so no write here is keyed: a write that got no answer is reported
 * NOT CONFIRMED (`writeJSON`), and the page re-reads the project before anything is sent again.
 */
import * as v from 'valibot';
import { query, command } from '#lib/reads.svelte.ts';
import { authorizedFetch, readJSON, writeJSON, type ApiResult } from './client.ts';
import { serviceUrl } from './services.ts';
import type { CorpusRow, RowKey } from '#lib/projects/corpus-rows.ts';

/** The rows of one dataset behind a page of tasks, and which of their columns form the key. */
export interface CorpusRows {
	rows: CorpusRow[];
	keyFields: string[];
}
import {
	DraftImportSchema,
	DraftSchema,
	CandidatesSchema,
	MemberListSchema,
	ProjectDetailSchema,
	ProjectListSchema,
	ProjectSchema,
	SendReceiptSchema,
	TaskDetailSchema,
	TaskListingSchema,
	type Candidates,
	type Draft,
	type DraftImport,
	type MemberList,
	type Ontology,
	type Project,
	type ProjectDetail,
	type ProjectList,
	type SendItem,
	type SamplingStrategy,
	type SendReceipt,
	type TaskDetail,
	type TaskListing,
} from '#lib/projects/types.ts';

const UPSTREAM = 'the annotator';
const enc = encodeURIComponent;
const at = (path: string): string => serviceUrl('annotator', path);

// ── reads ────────────────────────────────────────────────────────────────────────────────────

/** Every project of one organisation, with its state and progress counts, or with `can` only those the reader holds
 *  that relation on, as the workbench's store answers it. Gated on membership of the organisation: a refusal is a
 *  403, never an empty list. */
export const listProjects = query(
	'unchecked',
	({ tenant, can }: { tenant: string; can?: 'can_send_items' }): Promise<ApiResult<ProjectList>> =>
		readJSON(
			at(`/projects?${new URLSearchParams({ tenant, ...(can ? { can } : {}) })}`),
			ProjectListSchema,
			UPSTREAM,
		),
);

/** One project, the transitions out of its state, and what the reader may do on it. */
export const fetchProject = query(
	'unchecked',
	({ projectId }: { projectId: string }): Promise<ApiResult<ProjectDetail>> =>
		readJSON(at(`/projects/${enc(projectId)}`), ProjectDetailSchema, UPSTREAM),
);

/** Who holds which role directly on the project; a manager's read. */
export const fetchMembers = query(
	'unchecked',
	({ projectId }: { projectId: string }): Promise<ApiResult<MemberList>> =>
		readJSON(at(`/projects/${enc(projectId)}/members`), MemberListSchema, UPSTREAM),
);

/** The people a manager may grant a role on this project to by name; anyone else is granted by their principal. */
export const fetchCandidates = query(
	'unchecked',
	({ projectId }: { projectId: string }): Promise<ApiResult<Candidates>> =>
		readJSON(at(`/projects/${enc(projectId)}/members/candidates`), CandidatesSchema, UPSTREAM),
);

/** One page of a project's items as the annotator filters, sorts and pages them (`GET /projects/{id}/tasks`), with
 *  the project's index and publish precondition from the same snapshot: `filter` is the door's own query
 *  (`#lib/projects/item-query.ts` builds it from the page's address), `around` the item the page is read around (a
 *  canvas walking a pass), and `contributors` adds each person's part over the whole project. The browser never reads
 *  more than one page: what it shows past it, the annotator counts. */
export const pageTasks = query(
	'unchecked',
	({
		projectId,
		filter = '',
		limit = 25,
		offset = 0,
		around,
		details = true,
		contributors = false,
		work = false,
		judge = false,
		sampling = false,
	}: {
		projectId: string;
		filter?: string;
		limit?: number;
		offset?: number;
		around?: string;
		details?: boolean;
		contributors?: boolean;
		/** With the details, each item's work as a bulk label reads it: its revision and whole-item tags. */
		work?: boolean;
		/** With the details, what the project's judge said of each item's submission, and the judge's state. */
		judge?: boolean;
		/** With the details, what sampling knows of each item, and the project's sampling state. */
		sampling?: boolean;
	}): Promise<ApiResult<TaskListing>> => {
		const params = new URLSearchParams(filter);
		if (details) params.append('include', 'details');
		if (contributors) params.append('include', 'contributors');
		if (work) params.append('include', 'work');
		if (judge) params.append('include', 'judge');
		if (sampling) params.append('include', 'sampling');
		params.set('limit', String(limit));
		params.set('offset', String(offset));
		if (around !== undefined) params.set('around', around);
		return readJSON(at(`/projects/${enc(projectId)}/tasks?${params}`), TaskListingSchema, UPSTREAM);
	},
);

/** One item of a project as the listing answers it now, with its work (its revision and whole-item tags), read fresh:
 *  the page of one item around it. Null when the annotator answers no such item. */
export async function readItem(projectId: string, taskId: string): Promise<TaskDetail | null> {
	const params = new URLSearchParams({ around: taskId, limit: '1' });
	params.append('include', 'details');
	params.append('include', 'work');
	const result = await readJSON(
		at(`/projects/${enc(projectId)}/tasks?${params}`),
		TaskListingSchema,
		UPSTREAM,
	);
	return result.ok ? (result.data.details?.find((t) => t.task_id === taskId) ?? null) : null;
}

/** The first item of a project's walk as the annotator filters and orders it now (``filter``, the door's own query),
 *  read fresh; null when the walk is empty. */
export async function firstOfWalk(
	projectId: string,
	filter: string,
): Promise<ApiResult<TaskDetail | null>> {
	const params = new URLSearchParams(filter);
	params.append('include', 'details');
	params.set('limit', '1');
	params.set('offset', '0');
	const result = await readJSON(
		at(`/projects/${enc(projectId)}/tasks?${params}`),
		TaskListingSchema,
		UPSTREAM,
	);
	return result.ok ? { ok: true, data: result.data.details?.[0] ?? null } : result;
}

/** The most items a bulk action over "every item matching the filter" takes: past it, a person narrows the filter. */
export const BULK_CAP = 1000;

/** Every item matching ``filter``, read a page of 200 at a time, at most `BULK_CAP`: what a bulk action over the whole
 *  filter acts on. ``capped`` says when the filter matched more. */
export async function allMatching(
	projectId: string,
	filter: string,
	{
		work = false,
		judge = false,
		sampling = false,
	}: { work?: boolean; judge?: boolean; sampling?: boolean } = {},
): Promise<ApiResult<{ tasks: TaskDetail[]; capped: boolean }>> {
	const tasks: TaskDetail[] = [];
	let matched = 0;
	do {
		const params = new URLSearchParams(filter);
		params.set('include', 'details');
		if (work) params.append('include', 'work');
		if (judge) params.append('include', 'judge');
		if (sampling) params.append('include', 'sampling');
		params.set('limit', '200');
		params.set('offset', String(tasks.length));
		const result = await readJSON(
			at(`/projects/${enc(projectId)}/tasks?${params}`),
			TaskListingSchema,
			UPSTREAM,
		);
		if (!result.ok) return result;
		matched = result.data.matched;
		const page = result.data.details ?? [];
		tasks.push(...page);
		if (page.length === 0) break;
	} while (tasks.length < Math.min(matched, BULK_CAP));
	return { ok: true, data: { tasks: tasks.slice(0, BULK_CAP), capped: matched > BULK_CAP } };
}

const CorpusRowsSchema = v.object({
	rows: v.array(v.record(v.string(), v.unknown())),
	key_fields: v.array(v.string()),
});

/** The dataset rows behind a page of tasks, addressed by key (the viewer's `POST /api/atlas/chunks/by-key`), read as
 *  the person. They decorate the queue: a failure costs the extra columns, never the queue. `dataset` null asks the
 *  viewer's own default. */
export const corpusRows = query(
	'unchecked',
	async ({ dataset, keys }: { dataset: string | null; keys: RowKey[] }): Promise<CorpusRows> => {
		if (keys.length === 0) return { rows: [], keyFields: [] };
		const suffix = dataset === null ? '' : `?dataset=${enc(dataset)}`;
		const result = await readJSON(
			serviceUrl('viewer', `/api/atlas/chunks/by-key${suffix}`),
			CorpusRowsSchema,
			'the viewer',
			{
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ keys }),
			},
		);
		return result.ok
			? { rows: result.data.rows, keyFields: result.data.key_fields }
			: { rows: [], keyFields: [] };
	},
);

/** One task: its source, its captured ontology and the transitions out of its state. A task door names only the task:
 *  the annotator reads its project from the task's own record. */
export const fetchTask = query(
	'unchecked',
	({ taskId }: { taskId: string }): Promise<ApiResult<TaskDetail>> =>
		readJSON(at(`/tasks/${enc(taskId)}`), TaskDetailSchema, UPSTREAM),
);

/** The task's draft, its work in progress as the submit, the review and the publish read it, or null when nothing is
 *  saved yet. The canvas changes it through the annotations door (`annotations.api.ts`); the compare reads another
 *  replica's here. */
export function fetchDraft(taskId: string): Promise<ApiResult<Draft | null>> {
	return readJSON(at(`/tasks/${enc(taskId)}/draft`), v.nullable(DraftSchema), UPSTREAM);
}

// ── writes ───────────────────────────────────────────────────────────────────────────────────

/** The create body, as the service's `CreateProjectRequest` takes it; absent keys take its defaults. */
export interface CreateProject {
	tenant: string;
	slug: string;
	title?: string;
	description?: string;
	instructions?: string;
	review_required?: boolean;
	lease_seconds?: number;
	consensus_n?: number;
	ontology?: Ontology;
}

const send = <T>(
	method: string,
	path: string,
	body: unknown,
	schema: v.GenericSchema<unknown, T>,
): Promise<ApiResult<T>> => writeJSON(at(path), body, schema, UPSTREAM, { method });

/** A write's answer: its JSON parsed against `schema`, or the door's own refusal. */
async function answered<T>(
	res: Response,
	schema: v.GenericSchema<unknown, T>,
): Promise<ApiResult<T>> {
	const body: unknown = await res.json().catch(() => null);
	if (!res.ok) {
		const detail =
			body !== null && typeof body === 'object' && 'detail' in body
				? String((body as { detail: unknown }).detail)
				: `${UPSTREAM} answered ${res.status}`;
		return { ok: false, status: res.status, detail };
	}
	const read = v.safeParse(schema, body);
	return read.success
		? { ok: true, data: read.output }
		: { ok: false, status: 502, detail: `${UPSTREAM} contract drift: ${v.summarize(read.issues)}` };
}

/** A write with no JSON body (a DELETE, or an upload of raw bytes), waited for as `writeJSON` waits. */
async function sendRaw<T>(
	method: string,
	path: string,
	schema: v.GenericSchema<unknown, T>,
	init: RequestInit = {},
): Promise<ApiResult<T>> {
	let res: Response;
	try {
		res = await authorizedFetch(at(path), {
			...init,
			method,
			signal: AbortSignal.timeout(120_000),
		});
	} catch (err) {
		return {
			ok: false,
			status: 0,
			detail: `NOT CONFIRMED: ${UPSTREAM} gave no answer (${String(err)}), so the write may have landed`,
		};
	}
	return answered(res, schema);
}

/** Create a project (born `draft`) in an organisation; its creator owns it. */
export const createProject = command(
	(req: CreateProject) => `Create project ${req.title || req.slug}`,
	'unchecked',
	async (req: CreateProject): Promise<ApiResult<Project>> => {
		const result = await send('POST', '/projects', req, ProjectSchema);
		if (result.ok) {
			void listProjects({ tenant: req.tenant }).refresh();
			void listProjects({ tenant: req.tenant, can: 'can_send_items' }).refresh();
		}
		return result;
	},
);

/** Fire a project transition (open, freeze, publish, archive). `targetNamespace` rides only when named. */
export const fireProjectEvent = command(
	(input: { projectId: string; event: string }) => `${input.event} project ${input.projectId}`,
	'unchecked',
	({
		projectId,
		event,
		targetNamespace,
	}: {
		projectId: string;
		event: string;
		targetNamespace?: string;
	}): Promise<ApiResult<Project>> =>
		send(
			'POST',
			`/projects/${enc(projectId)}/events`,
			{ event, ...(targetNamespace ? { target_namespace: targetNamespace } : {}) },
			ProjectSchema,
		),
);

/** Replace the project's whole ontology; refused once the project is past labeling. */
export const updateProjectOntology = command(
	(input: { projectId: string }) => `Edit the labeling task of ${input.projectId}`,
	'unchecked',
	async ({
		projectId,
		ontology,
	}: {
		projectId: string;
		ontology: Ontology;
	}): Promise<ApiResult<Project>> => {
		const result = await send(
			'PATCH',
			`/projects/${enc(projectId)}/ontology`,
			{ ontology },
			ProjectSchema,
		);
		if (result.ok) void fetchProject({ projectId }).refresh();
		return result;
	},
);

/** Send items into the project; each becomes a claimable task (times the project's consensus). A send that names a
 *  sampling `proposal` has the annotator record which of its items sampling chose and which a person added. */
export const sendItems = command(
	(input: { projectId: string; items: SendItem[] }) =>
		`Send ${input.items.length} item${input.items.length === 1 ? '' : 's'} to ${input.projectId}`,
	'unchecked',
	({
		projectId,
		items,
		proposal,
	}: {
		projectId: string;
		items: SendItem[];
		proposal?: string;
	}): Promise<ApiResult<SendReceipt>> =>
		send(
			'POST',
			`/projects/${enc(projectId)}/items`,
			{ items, ...(proposal ? { proposal } : {}) },
			SendReceiptSchema,
		),
);

/** Name one accepted replica of a group canonical. */
export const adjudicate = command(
	(input: { groupId: string }) => `Pick the canonical replica of ${input.groupId}`,
	'unchecked',
	({
		projectId,
		groupId,
		taskId,
	}: {
		projectId: string;
		groupId: string;
		taskId: string;
	}): Promise<ApiResult<Project>> =>
		send(
			'PUT',
			`/projects/${enc(projectId)}/adjudications/${enc(groupId)}`,
			{ task_id: taskId },
			ProjectSchema,
		),
);

/** Withdraw a group's pick. */
export const clearAdjudication = command(
	(input: { groupId: string }) => `Clear the pick of ${input.groupId}`,
	'unchecked',
	({ projectId, groupId }: { projectId: string; groupId: string }): Promise<ApiResult<Project>> =>
		sendRaw('DELETE', `/projects/${enc(projectId)}/adjudications/${enc(groupId)}`, ProjectSchema),
);

const DropReceiptSchema = v.object({ task_id: v.string(), removed: v.boolean() });

/** Remove one item from the project (an item that can never be completed would wedge its publish). */
export const dropTask = command(
	(input: { taskId: string }) => `Remove item ${input.taskId}`,
	'unchecked',
	async ({
		projectId,
		taskId,
	}: {
		projectId: string;
		taskId: string;
	}): Promise<ApiResult<{ task_id: string; removed: boolean }>> => {
		const result = await sendRaw(
			'DELETE',
			`/projects/${enc(projectId)}/tasks/${enc(taskId)}`,
			DropReceiptSchema,
		);
		return result;
	},
);

/** Grant one role to one person; idempotent. */
export const grantMember = command(
	(input: { user: string; relation: string }) => `Grant ${input.relation} to ${input.user}`,
	'unchecked',
	async ({
		projectId,
		user,
		relation,
	}: {
		projectId: string;
		user: string;
		relation: string;
	}): Promise<ApiResult<MemberList>> => {
		const result = await send(
			'PUT',
			`/projects/${enc(projectId)}/members`,
			{ user, relation },
			MemberListSchema,
		);
		if (result.ok) void fetchMembers({ projectId }).refresh();
		return result;
	},
);

/** Revoke one role from one person; the project's last owner or manager is refused. */
export const revokeMember = command(
	(input: { user: string; relation: string }) => `Revoke ${input.relation} from ${input.user}`,
	'unchecked',
	async ({
		projectId,
		user,
		relation,
	}: {
		projectId: string;
		user: string;
		relation: string;
	}): Promise<ApiResult<MemberList>> => {
		const result = await sendRaw(
			'DELETE',
			`/projects/${enc(projectId)}/members?user=${enc(user)}&relation=${enc(relation)}`,
			MemberListSchema,
		);
		if (result.ok) void fetchMembers({ projectId }).refresh();
		return result;
	},
);

/** Fire one task transition. The page that fired it reads its listing again (`onchanged`). */
export const fireTaskEvent = command(
	(input: { taskId: string; event: string }) => `${input.event} item ${input.taskId}`,
	'unchecked',
	({
		taskId,
		...body
	}: {
		taskId: string;
		event: string;
		assignee?: string;
		message?: string;
		shape_ids?: string[];
		lease_seconds?: number;
		/** A review decision on an item a judge flagged: whether the reviewer found the flag right. Sent only for a
		 *  flagged item, and only where it differs from what the decision says (the annotator refuses it otherwise). */
		judge_agreed?: boolean;
	}): Promise<ApiResult<TaskDetail>> =>
		send('POST', `/tasks/${enc(taskId)}/events`, body, TaskDetailSchema),
);

/** Change a project's title, description, instructions, review and claim settings, consensus, judge or sampling; only
 *  the named ones. */
export const updateProjectSettings = command(
	(input: { projectId: string }) => `Change the settings of ${input.projectId}`,
	'unchecked',
	async ({
		projectId,
		settings,
	}: {
		projectId: string;
		settings: ProjectSettings;
	}): Promise<ApiResult<Project>> => {
		const result = await send('PATCH', `/projects/${enc(projectId)}`, settings, ProjectSchema);
		if (result.ok) void fetchProject({ projectId }).refresh();
		return result;
	},
);

/** The settings a project's settings door takes; an absent key is left as it is. */
export interface ProjectSettings {
	title?: string;
	description?: string;
	instructions?: string;
	review_required?: boolean;
	lease_seconds?: number;
	consensus_n?: number;
	/** The model that judges the project's submissions; empty names none. */
	judge_model?: string;
	/** How the project's queue is ordered. */
	sampling?: SamplingStrategy;
	/** The model whose doubt the queue is ordered by; empty names none. */
	sampling_model?: string;
}

/** Import annotations made elsewhere into a claimed task's draft, as an Arrow IPC stream, appended. */
export const importAnnotations = command(
	(input: { taskId: string }) => `Import annotations into ${input.taskId}`,
	'unchecked',
	({ taskId, bytes }: { taskId: string; bytes: ArrayBuffer }): Promise<ApiResult<DraftImport>> =>
		sendRaw('POST', `/tasks/${enc(taskId)}/import`, DraftImportSchema, {
			headers: { 'content-type': 'application/vnd.apache.arrow.stream' },
			body: bytes,
		}),
);
