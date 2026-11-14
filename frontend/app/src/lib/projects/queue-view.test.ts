/**
 * The queue as a GRID.
 *
 * A list is how you audit a queue; a grid is how you scan one. For page images the grid answers
 * "which of these are letters" in a glance.
 *
 * The tile's cell is the part worth pinning: addressed differently from the explorer's frames, the viewer
 * signs nothing for it and every tile shows a glyph with nothing saying why.
 */

import { describe, expect, it } from 'vitest';

import { cardCaption, parseView, taskMediaItem } from './queue-view.ts';
import type { TaskDetail } from './types.ts';

function task(over: Record<string, unknown> = {}): TaskDetail {
	return {
		task_id: 't1',
		project_id: 'p1',
		state: 'unassigned',
		source: { kind: 'chunks', keys: ['doc-a/1/12'], where: null },
		media: { kind: 'image', image_url: null, media_url: null },
		prediction: [],
		review_notes: [],
		legal_events: [],
		...over,
	} as unknown as TaskDetail;
}

describe('which view mode is in force', () => {
	it('reads a stored grid preference', () => {
		expect(parseView('grid')).toBe('grid');
	});

	it('falls back to LIST for anything unrecognised', () => {
		// List works for every corpus, including ones with no imagery to put in a grid. A stored value
		// from a future mode must not leave the queue rendering nothing.
		expect(parseView(null)).toBe('list');
		expect(parseView('')).toBe('list');
		expect(parseView('mosaic')).toBe('list');
	});
});

describe('the tile image', () => {
	it('signs the frame of the chunk the item key names, in the dataset it came from', () => {
		const t = task({ source: { kind: 'chunks', keys: ['doc-a/1/12'], where: 'vasa' } });

		expect(taskMediaItem(t)).toEqual({
			dataset: 'vasa',
			item: { role: 'frame', doc_id: 'doc-a', keys: [1, 12], frame_idx: 0 },
		});
	});

	it('signs the document thumbnail when the key names a document alone', () => {
		const t = task({ source: { kind: 'chunks', keys: ['doc-a'], where: 'vasa' } });

		expect(taskMediaItem(t)).toEqual({
			dataset: 'vasa',
			item: { role: 'thumbnail', doc_id: 'doc-a' },
		});
	});

	it('names nothing for an item with no dataset or no key, so the tile shows a glyph', () => {
		expect(taskMediaItem(task())).toBeNull();
		expect(taskMediaItem(task({ source: { kind: 'scope', keys: [], where: 'vasa' } }))).toBeNull();
	});
});

describe('the tile caption', () => {
	it('drops the document part, which is identical across the grid', () => {
		// `fe00cd746463ad2c/1/12` is unreadable at card size, and the leading hash is the same on
		// every tile of a document — so it carries no information where it costs the most room.
		expect(cardCaption(task())).toBe('1/12');
	});

	it('keeps a single-part key whole', () => {
		expect(cardCaption(task({ source: { kind: 'chunks', keys: ['solo'], where: null } }))).toBe(
			'solo',
		);
	});

	it('falls back to the task id when there is no key at all', () => {
		const t = task({ source: { kind: 'scope', keys: [], where: null } });

		expect(cardCaption(t)).toBe('t1');
	});
});
