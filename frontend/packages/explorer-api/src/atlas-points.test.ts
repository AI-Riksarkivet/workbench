/**
 * The atlas's points payload is decoded only when every per-point column holds one value per point; a
 * payload that disagrees with its own count is refused, naming the column, rather than drawn.
 */
import {
	Dictionary,
	Float16,
	Int32,
	Int64,
	Table,
	Utf8,
	tableToIPC,
	vectorFromArray,
} from 'apache-arrow';
import * as v from 'valibot';
import { describe, expect, it } from 'vitest';

import { ApiError, decodeAtlasPoints } from './api';
import { DatasetDescriptorSchema, DatasetView } from './descriptor';

const VIEW = new DatasetView(
	v.parse(DatasetDescriptorSchema, {
		id: 'acme$letters',
		tables: {
			chunks: {
				name: 'chunks',
				row_count: 3,
				version: 1,
				columns: [{ name: 'doc_id', arrow_type: 'string', nullable: false }],
				indexes: [],
			},
		},
		declared: {
			identity: { key_fields: ['doc_id', 'chunk_id'], doc_key: 'doc_id', doc_key_pattern: '.*' },
			atlas: [
				{
					name: 'umap',
					x: 'x',
					y: 'y',
					cluster: 'topic',
					source_column: 'v',
					table: 'chunks',
					channels: [{ name: 'collection', column: 'collection' }],
				},
			],
		},
	}),
);

/** The viewer's `build_points` shape for three points, with `count` as given. */
function payload(count: string): Uint8Array {
	const table = new Table({
		x: vectorFromArray([0.5, 1.5, -2], new Float16()),
		y: vectorFromArray([1, -1, 0.25], new Float16()),
		chunk_id: vectorFromArray([0, 1, 0], new Int32()),
		rowid: vectorFromArray([0n, 1n, 2n], new Int64()),
		doc: vectorFromArray(['a', 'a', 'b'], new Dictionary(new Utf8(), new Int32())),
		cluster: vectorFromArray([0, 0, 1], new Int32()),
		collection: vectorFromArray(
			['Letters', 'Letters', 'Maps'],
			new Dictionary(new Utf8(), new Int32()),
		),
	});
	table.schema.metadata.set('count', count);
	table.schema.metadata.set('space', 'umap');
	return tableToIPC(table, 'stream');
}

describe('decodeAtlasPoints', () => {
	it('decodes a payload whose columns all hold one value per point', () => {
		const points = decodeAtlasPoints(payload('3'), VIEW, 'umap');
		expect(points.count).toBe(3);
		expect(Array.from(points.x)).toEqual([0.5, 1.5, -2]);
		expect(points.docs).toEqual(['a', 'b']);
		expect(Array.from(points.doc)).toEqual([0, 0, 1]);
		expect(points.rowid).toEqual([0, 1, 2]);
		expect(points.keys.chunk_id).toEqual([0, 1, 0]);
		expect(Array.from(points.channels.collection!.codes)).toEqual([0, 0, 1]);
	});

	it('refuses a payload whose columns disagree with its count, naming the column', () => {
		expect(() => decodeAtlasPoints(payload('4'), VIEW, 'umap')).toThrow(
			new ApiError(
				502,
				'malformed /api/atlas/points payload: column x holds 3 values for 4 points',
			),
		);
	});

	it('refuses a count that is no row count', () => {
		expect(() => decodeAtlasPoints(payload('many'), VIEW, 'umap')).toThrow(
			/count many is no row count/,
		);
	});
});
