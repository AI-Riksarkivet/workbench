/**
 * The datasets the deployment's sources show the reader, and any table's rows: the viewer's
 * `GET /api/datasets` (grouped by source, with what could not be listed or opened), `GET /api/datasets/{id}`
 * (one dataset's facts: its source, tables, whether it carries a workbench descriptor and whether it may
 * be annotated), and the plain table's `GET /api/datasets/{id}/tables/{table}/rows` and `.../rows/{rowid}`,
 * read from the table's schema alone. A lakehouse is read as the person, whose refusal is final; any
 * other source is read with no token. A dataset's descriptor is read through the explorer's client
 * (`explore.api.ts`'s `datasetView`).
 */
import * as v from 'valibot';
import { error } from '@sveltejs/kit';
import { query } from '#lib/reads.svelte.ts';
import { readJSON, type ApiResult } from './client.ts';
import { serviceUrl } from './services.ts';

const TableFacts = v.object({ row_count: v.number(), version: v.number(), n_columns: v.number() });

const SOURCE_KINDS = ['rask', 'namespace', 'tables'] as const;

const DatasetSummary = v.object({
	id: v.string(),
	source: v.string(),
	stamped: v.boolean(),
	tables: v.record(v.string(), TableFacts),
	capabilities: v.array(v.string()),
	problem: v.nullable(v.string()),
	unreadable: v.record(v.string(), v.string()),
	read_only: v.nullable(v.string()),
});
export type DatasetSummary = v.InferOutput<typeof DatasetSummary>;

const Unlisted = v.object({ id: v.string(), reason: v.string() });

const SourceListing = v.object({
	name: v.string(),
	kind: v.picklist(SOURCE_KINDS),
	title: v.string(),
	datasets: v.array(DatasetSummary),
	unlisted: v.array(Unlisted),
	truncated: v.boolean(),
});
export type SourceListing = v.InferOutput<typeof SourceListing>;

const DatasetsSchema = v.object({ sources: v.array(SourceListing) });

const ColumnInfo = v.object({
	name: v.string(),
	arrow_type: v.string(),
	nullable: v.boolean(),
	vector_dim: v.nullish(v.number()),
	is_blob: v.optional(v.boolean(), false),
});

const TableInfo = v.object({
	name: v.string(),
	row_count: v.number(),
	version: v.number(),
	columns: v.array(ColumnInfo),
	indexes: v.array(
		v.object({ name: v.string(), index_type: v.string(), columns: v.array(v.string()) }),
	),
});
export type TableInfo = v.InferOutput<typeof TableInfo>;

const DatasetFacts = v.object({
	id: v.string(),
	source: v.string(),
	kind: v.picklist(SOURCE_KINDS),
	stamped: v.boolean(),
	tables: v.record(v.string(), TableInfo),
	capabilities: v.array(v.string()),
	problem: v.nullable(v.string()),
	unreadable: v.record(v.string(), v.string()),
	read_only: v.nullable(v.string()),
	/** What a set-up of a table's views can name (the widest vector the viewer projects). */
	setup_limits: v.object({ vector_max_dim: v.number() }),
});
export type DatasetFacts = v.InferOutput<typeof DatasetFacts>;

const COLUMN_KINDS = [
	'text',
	'integer',
	'float',
	'decimal',
	'boolean',
	'timestamp',
	'date',
	'time',
	'duration',
	'vector',
	'json',
	'binary',
	'blob',
	'other',
] as const;
export type ColumnKind = (typeof COLUMN_KINDS)[number];

const ColumnView = v.object({
	name: v.string(),
	arrow_type: v.string(),
	kind: v.picklist(COLUMN_KINDS),
	sortable: v.boolean(),
	text_search: v.boolean(),
	similar: v.boolean(),
	dim: v.nullable(v.number()),
});
export type ColumnView = v.InferOutput<typeof ColumnView>;

const PlainRow = v.object({
	rowid: v.number(),
	cells: v.record(v.string(), v.unknown()),
	media: v.optional(v.record(v.string(), v.string()), {}),
	score: v.nullable(v.number()),
});
export type PlainRow = v.InferOutput<typeof PlainRow>;

export const RowsPage = v.object({
	table: v.string(),
	qualified: v.string(),
	version: v.number(),
	columns: v.array(ColumnView),
	total: v.number(),
	offset: v.number(),
	limit: v.number(),
	rows: v.array(PlainRow),
});
export type RowsPage = v.InferOutput<typeof RowsPage>;

/** What one page of a plain table asks. */
export interface RowsQuery {
	offset: number;
	limit: number;
	sort: string | null;
	order: 'asc' | 'desc';
	/** A full-text query, over the first column with a full-text index. */
	q: string | null;
	/** Rank by similarity to this row (Lance's row id) on the first column with a vector index. */
	near: number | null;
}

/** A failed read as the page shows it: SvelteKit's `error`, with the service's own status and detail
 *  (0, unreachable, reads as 503). */
export function orFail<T>(result: ApiResult<T>): T {
	if (result.ok) return result.data;
	error(result.status === 0 ? 503 : result.status, result.detail);
}

const datasetPath = (id: string): string => `/api/datasets/${encodeURIComponent(id)}`;

export const fetchDatasets = query(async () =>
	orFail(await readJSON(serviceUrl('viewer', '/api/datasets'), DatasetsSchema, 'the viewer')),
);

/** One dataset's facts; the dataset's page reads them first, and its descriptor only when it has one. */
export const datasetFacts = query(v.string(), async (id) =>
	orFail(await readJSON(serviceUrl('viewer', datasetPath(id)), DatasetFacts, 'the viewer')),
);

/** The query string one page of rows asks with: a search, a similarity ranking, or a sort. */
function rowsParams(rows: RowsQuery): URLSearchParams {
	const params = new URLSearchParams({ offset: String(rows.offset), limit: String(rows.limit) });
	if (rows.q) params.set('q', rows.q);
	else if (rows.near !== null) params.set('near', String(rows.near));
	else if (rows.sort) {
		params.set('sort', rows.sort);
		params.set('order', rows.order);
	}
	return params;
}

/** One page of a table's rows, from its schema alone. */
export const tableRows = query(
	'unchecked',
	async ({ dataset, table, rows }: { dataset: string; table: string; rows: RowsQuery }) => {
		const path = `${datasetPath(dataset)}/tables/${encodeURIComponent(table)}/rows?${rowsParams(rows)}`;
		return orFail(await readJSON(serviceUrl('viewer', path), RowsPage, 'the viewer'));
	},
);

/** One row in full, at the version its page was read at. */
export const tableRow = query(
	'unchecked',
	async ({
		dataset,
		table,
		rowid,
		version,
	}: {
		dataset: string;
		table: string;
		rowid: number;
		version: number;
	}) => {
		const path = `${datasetPath(dataset)}/tables/${encodeURIComponent(table)}/rows/${rowid}?version=${version}`;
		return orFail(await readJSON(serviceUrl('viewer', path), PlainRow, 'the viewer'));
	},
);
