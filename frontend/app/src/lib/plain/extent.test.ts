import { describe, expect, it } from 'vitest';
import { extentNote } from './extent.ts';

describe("a derived read's extent as a status line says it", () => {
	it.each([
		[{ total: 10, read: 10, sampled: false, cut: null }, ''],
		[
			{ total: 3_000_000, read: 1_000_000, sampled: true, cut: null },
			'from 1,000,000 of 3,000,000 rows, sampled',
		],
		[
			{
				total: 3_000_000,
				read: 182_000,
				sampled: true,
				cut: 'the read stopped at 96 MB of values',
			},
			'from 182,000 of 3,000,000 rows: the read stopped at 96 MB of values',
		],
	])('%j reads %j', (extent, note) => {
		expect(extentNote(extent)).toBe(note);
	});
});
