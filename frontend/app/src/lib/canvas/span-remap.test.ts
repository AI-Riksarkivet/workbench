/**
 * Span offsets FOLLOW the text they annotate (§8d.1). Before this existed, correcting one typo
 * at the start of a line silently re-pointed every entity after it at the wrong characters —
 * annotations corrupted by the act of improving the data they annotate.
 */

import { describe, expect, it } from 'vitest';

import { editedRegions, remapSpans } from './span-remap.ts';

// 'Gustaf Eriksson Vasa reste till Dalarna' — span 'Vasa' at [16, 20), 'Dalarna' at [32, 39).
const TEXT = 'Gustaf Eriksson Vasa reste till Dalarna';

describe('editedRegions', () => {
	it('recovers each replaced region, and none for equal strings', () => {
		// 'Eriksson' → 'Erikson': one char deleted inside.
		expect(editedRegions(TEXT, TEXT.replace('Eriksson', 'Erikson'))).toEqual([
			{ from: 12, toOld: 13, delta: -1 },
		]);
		expect(editedRegions(TEXT, TEXT)).toEqual([]);
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

	it('a line corrected at once, words moved, keeps each span on the words it marked', () => {
		// 'Vasa' moves behind a new opening, and the ending is rewritten: two regions, one before both spans.
		const moved = 'Kung Gustaf Eriksson Vasa reste till Dalarna år 1520';
		expect(remapSpans(TEXT, moved, SPANS)).toEqual([
			{ index: 1, start: 21, end: 25 },
			{ index: 2, start: 37, end: 44 },
		]);
	});

	it('counts code points, so a character outside the BMP is one character before the spans it moves', () => {
		// '𝔄' (U+1D504) is two UTF-16 units and one code point; 'Vasa' moves by one.
		expect(remapSpans(TEXT, `𝔄${TEXT}`, SPANS)[0]).toEqual({ index: 1, start: 17, end: 21 });
	});

	it('a span whose every word is deleted is dropped, not kept empty', () => {
		const gone = TEXT.replace('Vasa', '');
		expect(remapSpans(TEXT, gone, SPANS)[0]).toEqual({ index: 1, start: null, end: null });
	});

	it('a span whose anchored text is PARTIALLY edited away is dropped, never guessed', () => {
		// Delete 'Vasa reste' — the Vasa span's text is gone; Dalarna shifts.
		const cut = TEXT.replace('Vasa reste till ', '');
		const [vasa, dalarna] = remapSpans(TEXT, cut, SPANS);
		expect(vasa).toEqual({ index: 1, start: null, end: null });
		expect(dalarna).toEqual({ index: 2, start: 16, end: 23 });
	});
});
