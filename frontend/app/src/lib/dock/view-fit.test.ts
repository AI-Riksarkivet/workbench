import { describe, expect, it } from 'vitest';
import type { LayoutConfig } from 'horizon-layout';
import type { ViewSnapshot } from './store.ts';
import { fitView } from './view-fit.ts';

const layout: LayoutConfig = { root: { tabs: ['results'], activeTabIndex: 0 } };
const view: ViewSnapshot = {
	layout,
	table: 'observations',
	setups: {
		atlas: { place: 'vector', vector: 'embedding', color: 'site' },
		graph: { shape: 'edges', source: 'src', target: 'east' },
	},
	q: 'salt',
	node: { of: 'graph', id: 's:Kalmar' },
};

describe('a view held against the dataset it opens on', () => {
	it('is applied whole when its table and every column it names are held', () => {
		const tables = { observations: ['embedding', 'site', 'src', 'east'] };
		expect(fitView(view, tables)).toEqual({ view, missing: [] });
	});

	it('leaves out a set-up naming a column the table no longer has, and the node drawn from it, by name', () => {
		const fitted = fitView(view, { observations: ['embedding', 'site', 'src'] });
		expect(fitted.view).toEqual({ ...view, setups: { atlas: view.setups.atlas }, node: null });
		expect(fitted.missing).toEqual([
			"This view's graph set-up names the column east, which the table observations no longer has: it was left out.",
		]);
	});

	it('keeps only the arrangement of a view whose table the dataset no longer holds, and names the table', () => {
		const fitted = fitView(view, { other: ['embedding'] });
		expect(fitted.view).toEqual({ layout, table: null, setups: {}, q: null, node: null });
		expect(fitted.missing).toEqual([
			'This view names the table observations, which this dataset no longer holds: its set-ups and search were left out.',
		]);
	});

	it('names a table the dataset lists but the reader may not read by the dataset’s reason, never as gone', () => {
		const refused = 'the catalog lists it, but does not let you read its rows';
		const fitted = fitView(view, { other: ['embedding'] }, { observations: refused });
		expect(fitted.view).toEqual({ layout, table: null, setups: {}, q: null, node: null });
		expect(fitted.missing).toEqual([
			`This view names the table observations, which you cannot read here (${refused}): its set-ups and search were left out.`,
		]);
	});

	it('holds a view naming no table against nothing', () => {
		const stamped = { ...view, table: null, setups: {}, node: null };
		expect(fitView(stamped, {})).toEqual({ view: stamped, missing: [] });
	});
});
