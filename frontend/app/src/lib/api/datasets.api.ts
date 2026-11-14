/**
 * The datasets the lakehouse shows the reader, and what one dataset offers: the viewer's
 * `GET /api/datasets` and `GET /api/datasets/{id}/descriptor`, each read as the person (the viewer
 * exchanges their token for one addressed to the lakehouse, whose refusal is final).
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

/** The parts of a descriptor this app reads; the rest is the viewer's and is left as it is. */
const DescriptorSchema = v.object({
	id: v.string(),
	tables: v.record(v.string(), v.looseObject({ row_count: v.number() })),
	declared: v.looseObject({
		identity: v.looseObject({ doc_key: v.string(), key_fields: v.array(v.string()) }),
		document: v.nullish(
			v.looseObject({
				table: v.string(),
				thumbnail: v.nullish(v.string()),
				mime: v.nullish(v.string()),
			}),
		),
		atlas: v.optional(v.array(v.looseObject({ name: v.string() })), []),
		search: v.nullish(v.looseObject({})),
	}),
});

/** A failed read as the page shows it: SvelteKit's `error`, with the service's own status and detail
 *  (0, unreachable, reads as 503). */
export function orFail<T>(result: ApiResult<T>): T {
	if (result.ok) return result.data;
	error(result.status === 0 ? 503 : result.status, result.detail);
}

export const fetchDatasets = query(async () =>
	orFail(await readJSON(serviceUrl('viewer', '/api/datasets'), DatasetsSchema, 'the viewer')),
);

export const fetchDescriptor = query(v.string(), async (id) =>
	orFail(
		await readJSON(
			serviceUrl('viewer', `/api/datasets/${encodeURIComponent(id)}/descriptor`),
			DescriptorSchema,
			'the viewer',
		),
	),
);
