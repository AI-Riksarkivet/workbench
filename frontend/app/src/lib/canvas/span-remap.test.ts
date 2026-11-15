/**
 * Span offsets FOLLOW the text they annotate (§8d.1). Before this existed, correcting one typo
 * at the start of a line silently re-pointed every entity after it at the wrong characters —
 * annotations corrupted by the act of improving the data they annotate.
 */

import { describe, expect, it } from 'vitest';

import { editedRegion, remapSpans } from './span-remap.ts';

// 'Gustaf Eriksson Vasa reste till Dalarna' — span 'Vasa' at [16, 20), 'Dalarna' at [32, 39).
const TEXT = 'Gustaf Eriksson Vasa reste till Dalarna';

describe('editedRegion', () => {
	it('recovers the single replaced region from prefix + suffix', () => {
		// 'Eriksson' → 'Erikson': one char deleted inside.
		expect(editedRegion(TEXT, TEXT.replace('Eriksson', 'Erikson'))).toMatchObject({ from: 12 });
	});

	it('equal strings yield an empty region', () => {
		const r = editedRegion(TEXT, TEXT);
		expect(r.from).toBe(r.toOld);
		expect(r.toOld).toBe(r.toNew);
	});
});

describe('remapSpans', () => {
	const SPANS = [
		{ index: 1, start: 16, end: 20 }, // 'Vasa'
		{ index: 2, start: 32, end: 39 }, // 'Dalarna'
	];

	it('an edit BEFORE a span shifts it; an edit AFTER leaves it alone', () => {
		// 'Eriksson' → 'Erikssonn' (+1 char at offset ~15): both spans sit after → both shift.
		const grown = TEXT.replace('Eriksson', 'Erikssonn');
		expect(remapSpans(TEXT, grown, SPANS)).toEqual([
			{ index: 1, start: 17, end: 21 },
			{ index: 2, start: 33, end: 40 },
		]);
		// Append at the very end: nothing before it moves.
		expect(remapSpans(TEXT, `${TEXT} 1520`, SPANS)).toEqual(SPANS);
	});

	it('an edit INSIDE a span stretches the span with its text', () => {
		// 'Dalarna' → 'Dalecarlia' (+3): the span grows, start pinned.
		const edited = TEXT.replace('Dalarna', 'Dalecarlia');
		const [, dalarna] = remapSpans(TEXT, edited, SPANS);
		expect(dalarna).toEqual({ index: 2, start: 32, end: 42 });
	});

	it('a span whose anchored text is PARTIALLY edited away is dropped, never guessed', () => {
		// Delete 'Vasa reste' — the Vasa span's text is gone; Dalarna shifts.
		const cut = TEXT.replace('Vasa reste till ', '');
		const [vasa, dalarna] = remapSpans(TEXT, cut, SPANS);
		expect(vasa).toEqual({ index: 1, start: null, end: null });
		expect(dalarna).toEqual({ index: 2, start: 16, end: 23 });
	});
});
