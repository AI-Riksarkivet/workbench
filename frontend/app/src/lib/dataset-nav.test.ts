/**
 * The dataset's views follow what its descriptor declares, asserted against real descriptors parsed by
 * the real schema.
 */
import { DatasetDescriptorSchema, DatasetView } from '@workbench/explorer-api/descriptor';
import * as v from 'valibot';
import { describe, expect, it } from 'vitest';
import { datasetPages, type DatasetPage } from './dataset-nav.ts';

/** The minimum a descriptor must carry; everything conditional is added per test. */
function descriptor(extra: Record<string, unknown> = {}): unknown {
	return {
		id: 'corpus',
		tables: {
			items: {
				name: 'items',
				row_count: 1,
				version: 1,
				columns: [{ name: 'item_id', arrow_type: 'string', nullable: false }],
				indexes: [],
			},
		},
		declared: {
			identity: { key_fields: ['item_id'], doc_key: 'item_id', doc_key_pattern: '.*' },
			document: { table: 'items', media_blob: 'blob', mime: 'mime' },
			time: null,
			display: { title: ['label'], body: 'caption', caption: null, metadata: [] },
			search: {
				row_table: 'items',
				fts: { table: 'items', column: 'caption', language: 'English' },
				vectors: {},
				filterable: [],
				rerank: false,
			},
			atlas: [],
			capabilities: {},
			...extra,
		},
	};
}

const view = (extra: Record<string, unknown> = {}): DatasetView =>
	new DatasetView(v.parse(DatasetDescriptorSchema, descriptor(extra)));

const ATLAS_SPACE = {
	name: 'text',
	x: 'atlas_x',
	y: 'atlas_y',
	cluster: 'atlas_cluster',
	source_column: 'semantic_vec',
	table: 'items',
	channels: [],
};

const openable = (entries: ReturnType<typeof datasetPages>): DatasetPage[] =>
	entries.filter((e) => e.unavailable === null).map((e) => e.page);

const why = (entries: ReturnType<typeof datasetPages>, page: DatasetPage): string | null =>
	entries.find((e) => e.page === page)?.unavailable ?? null;

describe('the views follow what the dataset declares', () => {
	it('lists every view, in one order, and opens none on a dataset that declares nothing', () => {
		const entries = datasetPages(view());
		expect(entries.map((e) => e.name)).toEqual(['Atlas', 'Graph', 'Tree']);
		expect(openable(entries)).toEqual([]);
	});

	it('opens each view by its own declaration, and a capability for something else opens none', () => {
		expect(openable(datasetPages(view({ atlas: [ATLAS_SPACE] })))).toEqual(['atlas']);
		expect(openable(datasetPages(view({ capabilities: { graph: 'kg' } })))).toEqual(['graph']);
		expect(openable(datasetPages(view({ capabilities: { topics: 'topics.tree' } })))).toEqual([
			'tree',
		]);
		expect(openable(datasetPages(view({ capabilities: { frames: 'chunks.image' } })))).toEqual([]);
	});

	it('a closed view says why, about the dataset; an open one carries no reason', () => {
		const closed = datasetPages(view());
		expect(why(closed, 'graph')).toMatch(/dataset declares no knowledge graph/);
		expect(why(closed, 'tree')).toMatch(/topic/);
		expect(why(closed, 'atlas')).toMatch(/atlas/);
		expect(why(datasetPages(view({ capabilities: { graph: 'kg' } })), 'graph')).toBeNull();
	});

	it('while the descriptor is read, every view is closed as loading', () => {
		const entries = datasetPages(null);
		expect(openable(entries)).toEqual([]);
		expect(why(entries, 'graph')).toMatch(/still being read/);
	});
});

describe('the atlas follows the table in view', () => {
	const spaceOn = (table: string) => ({ ...ATLAS_SPACE, table });

	it('opens when a space is bound to that table, or to any table when none is chosen', () => {
		expect(openable(datasetPages(view({ atlas: [spaceOn('pages')] }), 'pages'))).toContain('atlas');
		expect(openable(datasetPages(view({ atlas: [spaceOn('pages')] }), null))).toContain('atlas');
	});

	it('closes, naming the table, when every space belongs to another table', () => {
		const entries = datasetPages(view({ atlas: [spaceOn('pages')] }), 'lines');
		expect(openable(entries)).not.toContain('atlas');
		expect(why(entries, 'atlas')).toMatch(/table/);
	});

	it('leaves the graph and the tree to the dataset: capabilities are declared once per dataset', () => {
		const declared = view({ capabilities: { topics: 'topics.tree', graph: 'kg' } });
		expect(openable(datasetPages(declared, 'pages'))).toEqual(['graph', 'tree']);
	});
});
