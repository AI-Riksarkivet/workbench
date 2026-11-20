/**
 * The text canvas's rules: which rows are lines and in what order, how a line is cut where spans overlap, and how far a
 * dragged edge may go.
 */
import { describe, expect, it } from 'vitest';
import type { AnnoRow } from '../annotation-rows.ts';
import { movedEdge, segmentText, textLines } from './spans.ts';

const row = (over: Partial<AnnoRow>): AnnoRow => ({
	index: 0,
	id: 'r',
	label: '',
	status: 'accepted',
	shape: 'text',
	group: '',
	text: '',
	source: 'human',
	confidence: null,
	modelVersion: null,
	uncertainty: null,
	tStart: null,
	tEnd: null,
	parentId: '',
	charStart: null,
	charEnd: null,
	y: null,
	metadata: '{}',
	order: null,
	...over,
});

describe('the lines of an item', () => {
	it('are the rows with text that name no parent, in reading order, never a span or a whole-item choice', () => {
		const lines = textLines([
			row({ index: 0, id: 'low', shape: 'bbox', text: 'lower', y: 90 }),
			row({ index: 1, id: 'span', text: 'ow', parentId: 'low', charStart: 1, charEnd: 3 }),
			row({ index: 2, id: 'high', shape: 'bbox', text: 'upper', y: 10 }),
			row({ index: 3, id: 'tag', shape: 'tag', text: 'a letter' }),
			row({ index: 4, id: 'first', shape: 'bbox', text: 'declared first', y: 99, order: 0 }),
			row({ index: 5, id: 'blank', shape: 'bbox', text: '' }),
		]);
		expect(lines.map((l) => l.id)).toEqual(['first', 'high', 'low']);
	});
});

describe('a line cut at its spans’ edges', () => {
	it('covers the whole text, each stretch with the spans over all of it', () => {
		const segments = segmentText('Gustav Vasa crowned', [
			{ index: 1, start: 0, end: 11, label: 'person' },
			{ index: 2, start: 7, end: 19, label: 'event' },
		]);
		expect(segments.map((s) => [s.text, s.covering.map((c) => c.index)])).toEqual([
			['Gustav ', [1]],
			['Vasa', [1, 2]],
			[' crowned', [2]],
		]);
	});
});

describe('a line with a character outside the BMP', () => {
	it('is cut at code points, the offsets a span is saved with', () => {
		// '𝔄' is one code point and two UTF-16 units: 'Visby' starts at code point 12.
		const segments = segmentText('Brev till 𝔄 Visby', [
			{ index: 1, start: 12, end: 17, label: 'place' },
		]);
		expect(segments.map((s) => s.text)).toEqual(['Brev till 𝔄 ', 'Visby']);
	});
});

describe('a dragged edge', () => {
	it('stays inside the line and leaves the span at least a character', () => {
		expect(movedEdge({ start: 4, end: 8 }, 'end', 99, 10)).toEqual({ start: 4, end: 10 });
		expect(movedEdge({ start: 4, end: 8 }, 'start', 9, 10)).toEqual({ start: 7, end: 8 });
		expect(movedEdge({ start: 4, end: 8 }, 'end', 2, 10)).toEqual({ start: 4, end: 5 });
		expect(movedEdge({ start: 4, end: 8 }, 'start', 4, 10)).toBeNull();
	});
});
