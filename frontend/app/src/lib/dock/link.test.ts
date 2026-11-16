import { describe, expect, it } from 'vitest';
import { readDockLink, writeDockLink, type DockLink } from './link.ts';

const EMPTY: DockLink = {
	q: null,
	mode: null,
	topic: null,
	row: null,
	doc: null,
	t: null,
	entity: null,
	panels: [],
	max: null,
};

describe('a dataset page’s link', () => {
	it('reads back what it wrote, and keeps the page’s own parameters in their place', () => {
		const link: DockLink = {
			q: 'harbour master',
			mode: 'fts',
			topic: 'Letters',
			row: ['letter/001', '2'],
			doc: null,
			t: 12.5,
			entity: 'a000000000000001',
			panels: ['results', 'graph'],
			max: 'graph',
		};
		const written = writeDockLink(new URLSearchParams('project=acme&q=old&table=chunks'), link);
		expect([...written.keys()].slice(0, 2)).toEqual(['project', 'table']);
		expect(written.getAll('row')).toEqual(['letter/001', '2']);
		expect(written.toString()).toContain('panel=results&panel=graph');
		expect(readDockLink(written)).toEqual(link);
	});

	it('writes nothing of an empty view, and names a moment of a document by `doc` only without a row', () => {
		expect(writeDockLink(new URLSearchParams('project=acme'), EMPTY).toString()).toBe(
			'project=acme',
		);
		const moment = writeDockLink(new URLSearchParams(), { ...EMPTY, doc: 'letter-001', t: 12 });
		expect(moment.toString()).toBe('doc=letter-001&t=12');
	});

	it('drops what it cannot use: an unknown panel, a malformed entity, a bad time', () => {
		const link = readDockLink(
			new URLSearchParams('panel=atlas&panel=nope&panel=atlas&max=nope&entity=DROP&t=soon'),
		);
		expect(link).toEqual({ ...EMPTY, panels: ['atlas'] });
	});
});
