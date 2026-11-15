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
	MemberListSchema,
	ProjectDetailSchema,
	ProjectListSchema,
	ProjectSchema,
	SendReceiptSchema,
	TaskDetailSchema,
	TaskListingSchema,
	type Draft,
	type DraftImport,
	type MemberList,
	type Ontology,
	type Project,
	type ProjectDetail,
	type ProjectList,
	type SendItem,
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

/** The most task details one page of the listing carries (the annotator's own cap). */
const TASK_PAGE = 200;
/** A project holds at most this many pages the queue reads whole: past it, the listing says how many it shows. */
const MAX_TASK_PAGES = 50;

/** The work queue: states and counts, and with `details` every task's document and its own legal events, read a
 *  page at a time on the listing's cursor, so a project larger than one page is shown whole. */
export const listTasks = query(
	'unchecked',
	async ({
		projectId,
		details,
	}: {
		projectId: string;
		details?: boolean;
	}): Promise<ApiResult<TaskListing>> => {
		const base = `/projects/${enc(projectId)}/tasks`;
		if (!details) return readJSON(at(base), TaskListingSchema, UPSTREAM);
		const pages: TaskListing[] = [];
		let cursor: string | null = null;
		do {
			const query = new URLSearchParams({ include: 'details', limit: String(TASK_PAGE) });
			if (cursor !== null) query.set('cursor', cursor);
			const result = await readJSON(at(`${base}?${query}`), TaskListingSchema, UPSTREAM);
			if (!result.ok) return result;
			pages.push(result.data);
			cursor = result.data.next_cursor ?? null;
		} while (cursor !== null && pages.length < MAX_TASK_PAGES);
		const [first] = pages;
		if (first === undefined)
			return { ok: false, status: 502, detail: `${UPSTREAM} answered no page` };
		return {
			ok: true,
			data: {
				...first,
				details: pages.flatMap((p) => p.details ?? []),
				missing: pages.flatMap((p) => p.missing ?? []),
				next_cursor: cursor,
			},
		};
	},
);

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

/** The task's draft as the publish will read it, or null when none is written yet (the canvas then writes the first
 *  with no `base_revision`; on a failed read it writes nothing). Read fresh on every call, since a save is guarded by
 *  the revision this answers. */
export function fetchDraft(taskId: string): Promise<ApiResult<Draft | null>> {
	return readJSON(at(`/tasks/${enc(taskId)}/draft`), v.nullable(DraftSchema), UPSTREAM);
}

/** The canvas's save of one unit's shapes and links into the task's draft, guarded by `base_revision` (a stale one is
 *  a 409, never an overwrite). Not a `command`: autosave writes every few idle seconds, and the activity log keeps the
 *  person's deliberate writes. */
export function saveDraft(
	taskId: string,
	body: {
		shapes: Record<string, unknown>[];
		links: { name: string; from_shape: string; to_shape: string }[];
		base_revision?: number;
	},
): Promise<ApiResult<Draft>> {
	return send('PUT', `/tasks/${enc(taskId)}/draft`, body, DraftSchema);
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

/** Send items into the project; each becomes a claimable task (times the project's consensus). */
export const sendItems = command(
	(input: { projectId: string; items: SendItem[] }) =>
		`Send ${input.items.length} item${input.items.length === 1 ? '' : 's'} to ${input.projectId}`,
	'unchecked',
	({
		projectId,
		items,
	}: {
		projectId: string;
		items: SendItem[];
	}): Promise<ApiResult<SendReceipt>> =>
		send('POST', `/projects/${enc(projectId)}/items`, { items }, SendReceiptSchema),
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
		if (result.ok) void listTasks({ projectId, details: true }).refresh();
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

/** Fire one task transition. `projectId` names the listing it changes, for its refresh; it is not sent. */
export const fireTaskEvent = command(
	(input: { taskId: string; event: string }) => `${input.event} item ${input.taskId}`,
	'unchecked',
	async ({
		taskId,
		projectId,
		...body
	}: {
		taskId: string;
		event: string;
		projectId?: string;
		assignee?: string;
		message?: string;
		shape_ids?: string[];
		lease_seconds?: number;
	}): Promise<ApiResult<TaskDetail>> => {
		const result = await send('POST', `/tasks/${enc(taskId)}/events`, body, TaskDetailSchema);
		if (result.ok && projectId) void listTasks({ projectId, details: true }).refresh();
		return result;
	},
);

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
