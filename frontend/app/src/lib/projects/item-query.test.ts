import { describe, expect, it } from 'vitest';
import { DEFAULT_QUERY, doorParams, readItemQuery, writeItemQuery } from './item-query.ts';

describe('the item query in the address', () => {
	it('reads back what it wrote, leaving the defaults out and the other names in', () => {
		const query = {
			...DEFAULT_QUERY,
			states: ['in_review' as const, 'accepted' as const],
			label: 'seal',
			sort: 'updated' as const,
			order: 'desc' as const,
			page: 3,
		};
		const written = writeItemQuery(query, new URLSearchParams({ tab: 'items', page: '9' }));

		expect(written.toString()).toBe(
			'tab=items&state=in_review&state=accepted&label=seal&sort=updated&order=desc&page=3',
		);
		expect(readItemQuery(written)).toEqual(query);
	});

	it('reads an address naming no sort in the order given for it, and writes item order out where that is not it', () => {
		const written = writeItemQuery(
			{ ...DEFAULT_QUERY, sort: 'item' },
			new URLSearchParams(),
			'sampling',
		);

		expect([
			readItemQuery(new URLSearchParams(), 'sampling').sort,
			written.toString(),
			readItemQuery(written, 'sampling').sort,
		]).toEqual(['sampling', 'sort=item', 'item']);
	});

	it.each([
		['state=lost&state=claimed', { states: ['claimed'] }],
		['sort=assignee%3B+drop&order=sideways', { sort: 'item', order: 'asc' }],
		['size=7&page=0', { size: 25, page: 1 }],
		['page=999999', { page: 1 }],
		['since=2026-13-45&until=yesterday', { since: null, until: null }],
		[`label=${'x'.repeat(513)}`, { label: null }],
	])('leaves %s at its defaults', (address, expected) => {
		expect(readItemQuery(new URLSearchParams(address))).toMatchObject(expected);
	});

	it('names the reader as the holder of their own work and reads whole days', () => {
		const door = doorParams(
			{ ...DEFAULT_QUERY, whose: 'mine', since: '2026-11-01', until: '2026-11-01' },
			'dex~ada',
		);

		expect(door.get('holder')).toBe('dex~ada');
		expect(Date.parse(door.get('until') ?? '') - Date.parse(door.get('since') ?? '')).toBe(
			86_400_000,
		);
	});
});
