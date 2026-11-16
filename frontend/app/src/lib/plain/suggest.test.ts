import { describe, expect, it } from 'vitest';
import {
	prefillAtlas,
	prefillGraph,
	prefillTree,
	suggest,
	type Profile,
	type ProfiledColumn,
} from './suggest.ts';

const column = (
	name: string,
	kind: string,
	more: Partial<ProfiledColumn> = {},
): ProfiledColumn => ({
	name,
	kind,
	distinct: 100,
	nulls: 0,
	...more,
});

const profile = (columns: ProfiledColumn[], read = 100): Profile => ({
	extent: { read, total: read, sampled: false },
	columns,
});

describe('what the set-up forms offer and fill in', () => {
	it('fills the atlas in where one vector, one named pair, one low-cardinality colour and one label are offered', () => {
		const vector = suggest(
			profile([
				column('embedding', 'vector', { dim: 16, distinct: null }),
				column('site', 'text', { distinct: 4 }),
				column('note', 'text'),
				column('weight_kg', 'float'),
			]),
		);
		expect(prefillAtlas(vector)).toEqual({
			place: 'vector',
			vector: 'embedding',
			color: 'site',
			label: 'note',
		});
		expect(vector.atlas.place[0]?.reason).toBe(
			'a vector of 16 values, projected to two dimensions',
		);

		const pair = suggest(profile([column('umap_x', 'float'), column('umap_y', 'float')]));
		expect([prefillAtlas(pair), pair.atlas.place[0]?.reason]).toEqual([
			{ place: 'columns', x: 'umap_x', y: 'umap_y', color: undefined, label: undefined },
			'named as a umap projection (x and y)',
		]);
	});

	it('offers two plausible answers with their reasons and fills in neither', () => {
		const two = suggest(
			profile([
				column('embedding', 'vector', { dim: 8 }),
				column('lon', 'double'.replace('double', 'float')),
				column('lat', 'float'),
				column('kind', 'text', { distinct: 3 }),
				column('site', 'text', { distinct: 5 }),
			]),
		);
		expect(two.atlas.place.map((c) => c.reason)).toEqual([
			'a vector of 8 values, projected to two dimensions',
			'named as longitude and latitude',
		]);
		expect(two.atlas.color.map((c) => [c.value, c.reason])).toEqual([
			['kind', '3 distinct values'],
			['site', '5 distinct values'],
		]);
		expect(prefillAtlas(two)).toEqual({ color: undefined, label: undefined });
	});

	it('links an edge table by its named ends, and a self-referencing one by the column whose values are its ids', () => {
		const edges = suggest(
			profile([column('src', 'text'), column('dst', 'text'), column('weight', 'float')]),
		);
		expect(prefillGraph(edges)).toEqual({
			shape: 'edges',
			source: 'src',
			target: 'dst',
			weight: 'weight',
			label: undefined,
		});

		const self = suggest(
			profile([
				column('id', 'integer'),
				column('points_at', 'integer', { distinct: 40, refers_to: { id: 0.9 } }),
			]),
		);
		expect(self.graph.links).toEqual([
			{
				value: { shape: 'self', source: 'id', target: 'points_at' },
				reason: 'each row points to another: 90% of its values are id values',
			},
		]);
	});

	it('nests by a path column most of whose values hold a separator, or by an id and a named parent', () => {
		const path = suggest(
			profile([
				column('path', 'text', { separator: '/', separated: 0.75 }),
				// Free text holds a colon as often as a path does: it is not offered unless named as one.
				column('note', 'text', { separator: ':', separated: 1 }),
			]),
		);
		expect([prefillTree(path), path.tree.nesting[0]?.reason]).toEqual([
			{ shape: 'path', node: 'path', separator: '/', label: undefined },
			'75% of its values hold “/”',
		]);
		const parent = suggest(
			profile([
				column('id', 'text'),
				column('parent', 'text', { distinct: 30, refers_to: { id: 0.2 } }),
				column('title', 'text'),
			]),
		);
		expect(prefillTree(parent)).toEqual({
			shape: 'parent',
			node: 'id',
			parent: 'parent',
			label: 'title',
		});
	});
});
