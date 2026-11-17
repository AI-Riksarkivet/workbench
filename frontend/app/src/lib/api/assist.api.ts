/**
 * The task's assist models, as the labeling canvas asks them: the annotator's assist doors, same-origin on the DPoP seam
 * (`client.ts`), with the project on every call. `GET /api/assist/producers` says which models exist, whether each is a
 * real backend or the annotator's own mock, what each returns and whether that fits the task; `POST /api/assist/<unit
 * key>` runs one over the unit and answers its proposals, which the annotator has already held to the task's captured
 * ontology (it reads the task by id, so the browser never sends the rules it is judged by) and reports what it dropped.
 * The model servers themselves are the annotator's to reach; the browser never names one.
 */
import * as v from 'valibot';
import { readJSON, writeJSON, type ApiResult } from './client.ts';
import { serviceUrl } from './services.ts';

const UPSTREAM = 'the annotator';

const ProducerSchema = v.object({
	name: v.string(),
	configured: v.boolean(),
	returns: v.array(v.string()),
	inputs: v.optional(v.array(v.string()), []),
	compatible: v.nullable(v.boolean()),
	interactive: v.optional(v.boolean(), true),
});
export type Producer = v.InferOutput<typeof ProducerSchema>;

const ListingSchema = v.object({
	producers: v.array(ProducerSchema),
	default_configured: v.boolean(),
});
export type ProducerListing = v.InferOutput<typeof ListingSchema>;

const ShapeSchema = v.object({
	shape_type: v.string(),
	x: v.number(),
	y: v.number(),
	width: v.number(),
	height: v.number(),
	polygon: v.optional(v.array(v.number()), []),
	label: v.optional(v.string(), ''),
	text: v.optional(v.string(), ''),
	confidence: v.optional(v.number(), 0),
	uncertainty: v.optional(v.nullable(v.number()), null),
});

const ResultSchema = v.object({
	shapes: v.array(ShapeSchema),
	source: v.string(),
	dropped: v.optional(v.array(v.string()), []),
});
export type AssistAnswer = v.InferOutput<typeof ResultSchema>;

/** The unit an assist runs over: its project and task, its dataset and its key path. */
export interface AssistUnit {
	project: string;
	task: string;
	dataset: string;
	key: string;
}

/** One click of an interactive segmenter's session: `positive: false` marks background ("not this"). */
export interface AssistPoint {
	x: number;
	y: number;
	positive: boolean;
}

/** What to ask a model: its producer, a text prompt, a drawn region, and a segmenter's clicks so far. */
export interface AssistAsk {
	producer: string;
	prompt: string;
	region: { x: number; y: number; width: number; height: number } | null;
	points: AssistPoint[];
}

/** The models the task can ask, and what each would return for it. */
export function fetchProducers(unit: AssistUnit): Promise<ApiResult<ProducerListing>> {
	const query = new URLSearchParams({ project: unit.project, task_id: unit.task });
	return readJSON(
		serviceUrl('annotator', `/api/assist/producers?${query}`),
		ListingSchema,
		UPSTREAM,
	);
}

/** Ask a model for proposals on the unit. */
export function requestAssist(unit: AssistUnit, ask: AssistAsk): Promise<ApiResult<AssistAnswer>> {
	const path = unit.key.split('/').map(encodeURIComponent).join('/');
	const query = new URLSearchParams({ project: unit.project, dataset: unit.dataset });
	return writeJSON(
		serviceUrl('annotator', `/api/assist/${path}?${query}`),
		{
			producer: ask.producer,
			prompt: ask.prompt,
			region: ask.region,
			points: ask.points,
			task_id: unit.task,
		},
		ResultSchema,
		UPSTREAM,
	);
}
