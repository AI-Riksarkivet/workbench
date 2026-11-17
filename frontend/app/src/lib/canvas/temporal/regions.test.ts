import { describe, expect, it } from 'vitest';
import { laneOf, neighbour, snapTargets, snapTo, type Region } from './regions.ts';

const R = (index: number, start: number, end: number): Region => ({ index, start, end });

describe('regions on the time line', () => {
	it('sit on the first lane free when they start, so overlapping ones stack and the rest share the top lane', () => {
		// Region 4 starts the moment region 0 ends: they touch, and share the top lane.
		expect(
			[...laneOf([R(2, 3, 5), R(0, 0, 2), R(1, 1, 4), R(3, 4.5, 6), R(4, 2, 2.5)])].sort(),
		).toEqual([
			[0, 0],
			[1, 1],
			[2, 0],
			[3, 1],
			[4, 0],
		]);
	});

	it('snap an edge to the nearest target within the tolerance, and leave it where it is past it', () => {
		const targets = snapTargets([R(0, 1, 2), R(1, 3, 4)], 1, 2.6);
		expect(targets).toEqual([2.6, 1, 2]);
		expect([
			snapTo(2.05, targets, 0.1),
			snapTo(2.5, targets, 0.15),
			snapTo(2.3, targets, 0.1),
		]).toEqual([2, 2.6, 2.3]);
	});

	it('step to the next or previous one by start, from the ends when none is selected', () => {
		const regions = [R(5, 4, 5), R(7, 0, 1), R(9, 2, 3)];
		expect([
			neighbour(regions, null, 1)?.index,
			neighbour(regions, null, -1)?.index,
			neighbour(regions, 7, 1)?.index,
			neighbour(regions, 5, 1),
		]).toEqual([7, 5, 9, null]);
	});
});
