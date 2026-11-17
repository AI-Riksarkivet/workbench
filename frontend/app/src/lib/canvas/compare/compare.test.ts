/**
 * Two versions of an item, paired: each kind of shape matched by its own overlap, a pair alike only when it overlaps
 * almost wholly and carries one class, and a shape with no partner standing alone.
 */
import { describe, expect, it } from 'vitest';
import { diffVersions, type CompareShape } from './compare.ts';

const shape = (over: Partial<CompareShape> & { index: number }): CompareShape => ({
	id: `s${over.index}`,
	shape: 'bbox',
	label: 'stamp',
	box: null,
	time: null,
	span: null,
	...over,
});

const kinds = (mine: CompareShape[], theirs: CompareShape[]) =>
	diffVersions(mine, theirs).map((e) => [e.kind, e.mine?.index ?? null, e.theirs?.index ?? null]);

describe('two versions of a picture', () => {
	it('pair boxes by overlap: alike, moved, relabelled, and one each alone', () => {
		const mine = [
			shape({ index: 0, box: { x: 0, y: 0, w: 100, h: 100 } }),
			shape({ index: 1, box: { x: 200, y: 0, w: 100, h: 100 } }),
			shape({ index: 2, label: 'seal', box: { x: 400, y: 0, w: 100, h: 100 } }),
			shape({ index: 3, box: { x: 600, y: 600, w: 10, h: 10 } }),
		];
		const theirs = [
			shape({ index: 0, box: { x: 1, y: 1, w: 100, h: 100 } }),
			shape({ index: 1, box: { x: 230, y: 0, w: 100, h: 100 } }),
			shape({ index: 2, box: { x: 400, y: 0, w: 100, h: 100 } }),
			shape({ index: 3, box: { x: 900, y: 900, w: 10, h: 10 } }),
		];
		expect(kinds(mine, theirs)).toEqual([
			['same', 0, 0],
			['changed', 1, 1],
			['changed', 2, 2],
			['mine', 3, null],
			['theirs', null, 3],
		]);
	});
});

describe('other kinds of shape', () => {
	it('pair regions of time by their stretch, spans by their characters in the same text, choices by class', () => {
		const mine = [
			shape({ index: 0, shape: 'segment', time: { start: 0, end: 10 } }),
			shape({ index: 1, shape: 'text', span: { text: 'Gustav Vasa', start: 0, end: 6 } }),
			shape({ index: 2, shape: 'tag', label: 'letter' }),
		];
		const theirs = [
			shape({ index: 0, shape: 'segment', time: { start: 1, end: 10 } }),
			shape({ index: 1, shape: 'text', span: { text: 'another line', start: 0, end: 6 } }),
			shape({ index: 2, shape: 'tag', label: 'map' }),
		];
		expect(kinds(mine, theirs)).toEqual([
			['same', 0, 0],
			['mine', 1, null],
			['mine', 2, null],
			['theirs', null, 1],
			['theirs', null, 2],
		]);
	});
});
