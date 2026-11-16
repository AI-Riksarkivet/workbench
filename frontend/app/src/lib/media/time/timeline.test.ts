import { describe, expect, it } from 'vitest';
import { formatClock, spanAt } from './timeline.ts';

const spans = [
	{ start: 1, end: 2 },
	{ start: 2, end: 4 },
	{ start: 6, end: 8 },
];

describe('spanAt', () => {
	it('is the span that holds the playhead', () => {
		expect(spanAt(spans, 1)).toBe(0);
		expect(spanAt(spans, 3.9)).toBe(1);
		expect(spanAt(spans, 7)).toBe(2);
	});
	it('in a gap, is the span that began last before the playhead', () => {
		expect(spanAt(spans, 5)).toBe(1);
		expect(spanAt(spans, 9)).toBe(2);
	});
	it('is -1 before the first span begins, and for no spans', () => {
		expect(spanAt(spans, 0.5)).toBe(-1);
		expect(spanAt([], 3)).toBe(-1);
	});
});

describe('formatClock', () => {
	it('reads minutes and seconds, and hours from an hour on', () => {
		expect(formatClock(4.4)).toBe('0:04');
		expect(formatClock(3725)).toBe('1:02:05');
	});
	it('reads 0:00 for a length not yet known', () => {
		expect(formatClock(Number.NaN)).toBe('0:00');
	});
});
