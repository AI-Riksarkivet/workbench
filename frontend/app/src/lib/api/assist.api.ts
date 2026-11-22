/**
 * The task's assist models, as the labeling canvas asks them: the annotator's assist doors, same-origin on the DPoP seam
 * (`client.ts`), with the project on every call. `GET /api/assist/models` says whether the deployment names a model
 * endpoint and, when it does, which models it serves, which prompts each takes, what it returns and whether that fits
 * the task; `POST /api/assist/<unit key>` asks one of them over the unit and answers its proposals, with the model, its
 * version and each proposal's score, which the annotator has already held to the task's captured ontology (it reads the
 * task by id, so the browser never sends the rules it is judged by) and says what it dropped.
 *
 * The model endpoint is the annotator's to reach: the browser never names one, and no answer here carries its address.
 * An endpoint that is slow or fails is a refusal with the annotator's `detail`, never an empty answer.
 */
import * as v from 'valibot';
import { query } from '#lib/reads.svelte.ts';
import { readJSON, writeJSON, type ApiResult } from './client.ts';
import { serviceUrl } from './services.ts';

const UPSTREAM = 'the annotator';

const ModelSchema = v.object({
	name: v.string(),
	version: v.string(),
	title: v.string(),
	reads: v.array(v.string()),
	/** The prompts it takes: `text` (words to find), `box` (a region drawn on the picture), `points` (clicks). */
	prompts: v.array(v.string()),
	/** The shape types of what it proposes; empty for a model that proposes nothing, which is offered only as a
	 *  project's judge. */
	returns: v.array(v.string()),
	/** The shape types of the labels it judges; empty for a model that is no judge. */
	assesses: v.array(v.string()),
	/** Whether what it returns fits the task's classes; null when there is nothing to judge it against. */
	compatible: v.nullable(v.boolean()),
});
export type AssistModel = v.InferOutput<typeof ModelSchema>;

const ListingSchema = v.object({
	available: v.boolean(),
	/** Why assist is unavailable, in words a person reads; empty when it is available. */
	reason: v.string(),
	models: v.array(ModelSchema),
});
export type AssistListing = v.InferOutput<typeof ListingSchema>;

const ProposalSchema = v.object({
	shape_type: v.string(),
	x: v.number(),
	y: v.number(),
	width: v.number(),
	height: v.number(),
	polygon: v.array(v.number()),
	label: v.string(),
	text: v.string(),
	score: v.number(),
});

const AnswerSchema = v.object({
	model: v.string(),
	version: v.string(),
	proposals: v.array(ProposalSchema),
	dropped: v.array(v.string()),
});
export type AssistAnswer = v.InferOutput<typeof AnswerSchema>;
export type AssistProposal = v.InferOutput<typeof ProposalSchema>;

/** The unit an assist runs over: its project and task, its dataset and its key path. */
export interface AssistUnit {
	project: string;
	task: string;
	dataset: string;
	key: string;
}

/** One click of a session: `positive: false` marks background ("not this"). */
export interface AssistPoint {
	x: number;
	y: number;
	positive: boolean;
}

/** What to ask a model: its name, the words to find, a drawn region, and the session's clicks so far. */
export interface AssistAsk {
	model: string;
	prompt: string;
	region: { x: number; y: number; width: number; height: number } | null;
	points: AssistPoint[];
}

/** Whether a model takes ``prompt``, as the endpoint describes it. */
export const takes = (model: AssistModel, prompt: 'text' | 'box' | 'points'): boolean =>
	model.prompts.includes(prompt);

/** Whether ``model`` proposes anything: a model that returns nothing is a judge only, and never offered for an ask. */
export const proposes = (model: Pick<AssistModel, 'returns'>): boolean => model.returns.length > 0;

/** Whether ``model`` can be a project's judge: it judges labels of some shape. */
export const judges = (model: Pick<AssistModel, 'assesses'>): boolean => model.assesses.length > 0;

/** The models the project can ask, each judged against the classes of its task ``task`` where one is named, or why
 *  assist is unavailable. */
export function fetchModels(project: string, task?: string): Promise<ApiResult<AssistListing>> {
	const query = new URLSearchParams(task === undefined ? { project } : { project, task_id: task });
	return readJSON(serviceUrl('annotator', `/api/assist/models?${query}`), ListingSchema, UPSTREAM);
}

/** The models the project can ask, as a held read (`fetchModels` with no task): what a page that only lists them reads. */
export const listModels = query(
	'unchecked',
	({ project }: { project: string }): Promise<ApiResult<AssistListing>> => fetchModels(project),
);

/** Ask a model for proposals on the unit; ``signal`` gives the ask up (the person stopped it, or left the page). */
export function requestAssist(
	unit: AssistUnit,
	ask: AssistAsk,
	signal?: AbortSignal,
): Promise<ApiResult<AssistAnswer>> {
	const path = unit.key.split('/').map(encodeURIComponent).join('/');
	const query = new URLSearchParams({ project: unit.project, dataset: unit.dataset });
	return writeJSON(
		serviceUrl('annotator', `/api/assist/${path}?${query}`),
		{
			model: ask.model,
			prompt: ask.prompt === '' ? null : ask.prompt,
			region: ask.region,
			points: ask.points,
			task_id: unit.task,
		},
		AnswerSchema,
		UPSTREAM,
		signal ? { signal } : {},
	);
}
