/**
 * The sampling doors (ADR 0034): what orders a project's queue and proposes what to send.
 *
 * - `POST /projects/{id}/sampling/reads` reads the numbers a strategy orders by for up to 50 items, as the person: a
 *   score column of the dataset, its embeddings (each item's novelty against the labelled ones, measured again), and
 *   says which items a model is still to be asked about. It asks no model.
 * - `POST /api/sampling/<unit key>` asks the project's sampling model about one item and keeps its doubt; an item it
 *   already scored answers the kept score. Addressed as the judge and assist doors are: the unit's key in the path, its
 *   project, task and dataset in the query.
 * - `POST /projects/{id}/sampling/proposals` ranks the keys a send would carry and answers the ones the strategy
 *   chooses, from the dataset's score column and embeddings alone (no model is asked at send); a send that names the
 *   proposal is recorded against it (`sendItems`).
 *
 * None of these changes an item, so none is recorded in the person's activity log.
 */
import { readJSON, writeJSON, type ApiResult } from './client.ts';
import { serviceUrl } from './services.ts';
import {
	ProposalSchema,
	SamplingReadsSchema,
	SamplingSchema,
	TaskListingSchema,
	type Proposal,
	type Sampling,
	type SamplingReads,
	type TaskDetail,
} from '#lib/projects/types.ts';

const UPSTREAM = 'the annotator';
const enc = encodeURIComponent;
const at = (path: string): string => serviceUrl('annotator', path);
const signalled = (signal?: AbortSignal) => (signal ? { signal } : {});

/** The most items one read takes, and the most items an Ask round reads. */
export const READ_BATCH = 50;

/** Up to `READ_BATCH` of the items ``filter`` matches that the project's sampling model has not scored
 *  (`scored=false`) or that are not measured against the labelled items (`measured=false`), in item order from
 *  ``offset``: what one lane of a round reads. Item order, so items a round could not number are passed by offset. */
export async function itemsLacking(
	projectId: string,
	filter: string,
	lane: 'scored' | 'measured',
	offset: number,
	signal?: AbortSignal,
): Promise<ApiResult<TaskDetail[]>> {
	const params = new URLSearchParams(filter);
	params.set(lane, 'false');
	params.set('sort', 'item');
	params.set('order', 'asc');
	params.append('include', 'details');
	params.set('limit', String(READ_BATCH));
	params.set('offset', String(offset));
	const result = await readJSON(
		at(`/projects/${enc(projectId)}/tasks?${params}`),
		TaskListingSchema,
		UPSTREAM,
		signalled(signal),
	);
	return result.ok ? { ok: true, data: result.data.details ?? [] } : result;
}

/** Read the numbers the project's strategy orders by for ``taskIds`` (at most `READ_BATCH`). */
export function readSamples(
	projectId: string,
	taskIds: readonly string[],
	signal?: AbortSignal,
): Promise<ApiResult<SamplingReads>> {
	return writeJSON(
		at(`/projects/${enc(projectId)}/sampling/reads`),
		{ task_ids: taskIds.slice(0, READ_BATCH) },
		SamplingReadsSchema,
		UPSTREAM,
		signalled(signal),
	);
}

/** Ask the project's sampling model about ``task``, or read the score it kept; ``signal`` gives the ask up. */
export function requestSampling(
	task: Pick<TaskDetail, 'task_id' | 'project_id' | 'source'>,
	signal?: AbortSignal,
): Promise<ApiResult<Sampling>> {
	const key = task.source.keys[0] ?? '';
	const path = key.split('/').map(enc).join('/');
	const query = new URLSearchParams({ project: task.project_id, task: task.task_id });
	if (task.source.where) query.set('dataset', task.source.where);
	return writeJSON(
		at(`/api/sampling/${path}?${query}`),
		null,
		SamplingSchema,
		UPSTREAM,
		signalled(signal),
	);
}

/** The most keys one proposal ranks, and the most it may choose. */
export const PROPOSAL_CAP = 1000;

/** Ask sampling which ``count`` of ``keys`` (items of ``dataset``) the project's strategy would send. */
export function proposeItems(
	projectId: string,
	{ dataset, keys, count }: { dataset: string; keys: readonly string[]; count: number },
): Promise<ApiResult<Proposal>> {
	return writeJSON(
		at(`/projects/${enc(projectId)}/sampling/proposals`),
		{ dataset, keys: keys.slice(0, PROPOSAL_CAP), count },
		ProposalSchema,
		UPSTREAM,
	);
}
