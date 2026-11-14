/**
 * Search in one dataset: the search service's `GET /api/search`, whose hits come back as one Arrow IPC
 * stream of rows (`service_kit.arrow_rows`), read as the person.
 */
import * as v from 'valibot';
import { query } from '#lib/reads.svelte.ts';
import { orFail } from './datasets.api.ts';
import { readBytes } from './client.ts';
import { rowsOf, type Row } from './arrow.ts';
import { serviceUrl } from './services.ts';

const SearchInput = v.object({
	dataset: v.pipe(v.string(), v.minLength(1)),
	q: v.pipe(v.string(), v.trim(), v.minLength(1), v.maxLength(2000)),
	n: v.optional(v.pipe(v.number(), v.integer(), v.minValue(1), v.maxValue(200)), 40),
});

export const searchRows = query(SearchInput, async ({ dataset, q, n }): Promise<Row[]> => {
	const params = new URLSearchParams({ dataset, q, n: String(n), mode: 'fts' });
	const read = orFail(await readBytes(serviceUrl('search', `/api/search?${params}`), 'search'));
	return rowsOf(read.bytes);
});
