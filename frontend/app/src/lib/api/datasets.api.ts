/**
 * The datasets the lakehouse shows the reader: the viewer's `GET /api/datasets`, read as the person (the
 * viewer exchanges their token for one addressed to the lakehouse, whose refusal is final). One dataset's
 * descriptor is read through the explorer's client (`explore.api.ts`'s `datasetView`).
 */
import * as v from 'valibot';
import { error } from '@sveltejs/kit';
import { query } from '#lib/reads.svelte.ts';
import { readJSON, type ApiResult } from './client.ts';
import { serviceUrl } from './services.ts';

const TableFacts = v.object({ row_count: v.number(), version: v.number(), n_columns: v.number() });

const DatasetSummary = v.object({
	id: v.string(),
	tables: v.record(v.string(), TableFacts),
	capabilities: v.array(v.string()),
});
export type DatasetSummary = v.InferOutput<typeof DatasetSummary>;

const DatasetsSchema = v.object({ datasets: v.array(DatasetSummary) });

/** A failed read as the page shows it: SvelteKit's `error`, with the service's own status and detail
 *  (0, unreachable, reads as 503). */
export function orFail<T>(result: ApiResult<T>): T {
	if (result.ok) return result.data;
	error(result.status === 0 ? 503 : result.status, result.detail);
}

export const fetchDatasets = query(async () =>
	orFail(await readJSON(serviceUrl('viewer', '/api/datasets'), DatasetsSchema, 'the viewer')),
);
