/**
 * A document's time as the viewer answers it, for a canvas over its recording or video: its waveform
 * (`GET /api/datasets/{id}/documents/{doc}/peaks`, `viewer.services.peaks`), built into its zoom levels here, its parts
 * (`/api/doc-chunks`) and its speaker turns (`/api/diarization`). Each names its dataset itself rather than the
 * explorer's active one, so a canvas reads the task's own dataset whatever page was open before. Read as the person.
 */
import * as v from 'valibot';
import { PeaksSchema, waveformOf, type Waveform } from '#lib/media/time/waveform/peaks.ts';
import { query } from '#lib/reads.svelte.ts';
import { readJSON, type ApiResult } from './client.ts';
import { serviceUrl } from './services.ts';

const enc = encodeURIComponent;

/** The waveform of `docId`'s sound in `dataset`, or the viewer's reason there is none. */
export const documentPeaks = query(
	v.object({ dataset: v.string(), docId: v.string() }),
	async ({ dataset, docId }): Promise<ApiResult<Waveform>> => {
		const answer = await readJSON(
			serviceUrl('viewer', `/api/datasets/${enc(dataset)}/documents/${enc(docId)}/peaks`),
			PeaksSchema,
			'the viewer',
		);
		return answer.ok ? { ok: true, data: waveformOf(answer.data) } : answer;
	},
);

const RowsSchema = v.object({ chunks: v.array(v.record(v.string(), v.unknown())) });
const TurnsSchema = v.object({
	built: v.boolean(),
	turns: v.array(v.object({ speaker: v.string(), start: v.number(), end: v.number() })),
});

/** `docId`'s parts in `dataset`, in order: rows, read through the dataset's view for their times and text. */
export const documentParts = query(
	v.object({ dataset: v.string(), docId: v.string() }),
	async ({ dataset, docId }): Promise<ApiResult<Record<string, unknown>[]>> => {
		const answer = await readJSON(
			serviceUrl('viewer', `/api/doc-chunks/${enc(docId)}?dataset=${enc(dataset)}`),
			RowsSchema,
			'the viewer',
		);
		return answer.ok ? { ok: true, data: answer.data.chunks } : answer;
	},
);

/** `docId`'s speaker turns in `dataset`; none where it declares no diarization. */
export const documentTurns = query(
	v.object({ dataset: v.string(), docId: v.string() }),
	async ({
		dataset,
		docId,
	}): Promise<ApiResult<{ speaker: string; start: number; end: number }[]>> => {
		const answer = await readJSON(
			serviceUrl('viewer', `/api/diarization/${enc(docId)}?dataset=${enc(dataset)}`),
			TurnsSchema,
			'the viewer',
		);
		return answer.ok ? { ok: true, data: answer.data.turns } : answer;
	},
);
