import { describe, expect, it } from 'vitest';
import { clampView, follow, panBy, timeOfX, xOf, zoomAt } from './view.ts';

describe('a view of the time axis', () => {
	it('zooms about a moment, which stays where it was on the axis', () => {
		const view = zoomAt({ start: 0, end: 10 }, 5, 2, 100);
		expect(view).toEqual({ start: 2.5, end: 7.5 });
		expect(xOf(view, 5, 200)).toBe(100);
	});

	it('is kept inside the recording, and no shorter than a tenth of a second', () => {
		expect(clampView({ start: -3, end: 4 }, 10)).toEqual({ start: 0, end: 7 });
		expect(
			zoomAt({ start: 0, end: 1 }, 0.5, 100, 10).end -
				zoomAt({ start: 0, end: 1 }, 0.5, 100, 10).start,
		).toBeCloseTo(0.1);
		expect(panBy({ start: 8, end: 10 }, 5, 10)).toEqual({ start: 8, end: 10 });
	});

	it('moves to show a playhead that left it, and not while it is inside', () => {
		const view = { start: 0, end: 10 };
		expect(follow(view, 5, 100)).toBe(view);
		expect(follow(view, 12, 100, 0.1)).toEqual({ start: 3, end: 13 });
	});

	it('reads a pointer’s x back as the moment it is at', () => {
		expect(timeOfX({ start: 2, end: 4 }, 50, 100)).toBe(3);
	});
});
