/**
 * The queue's two view modes, and what a task looks like as a CARD.
 *
 * A list is how you audit a queue; a grid is how you scan one. For a corpus of page images the grid
 * is the mode that answers "which of these are letters" in one glance, and it is the mode Label
 * Studio's Data Manager opens in for image data. A list alone makes judging forty items a
 * read of forty rows of keys.
 *
 * Both modes share ONE selection (the same `rowSelection`, keyed by task id), so the bulk actions
 * work identically from either. That is the property that makes a grid worth having rather than a
 * second, weaker table.
 */

import type { MediaItem } from '#lib/api/media-links.svelte.ts';
import { rowKeyFor } from './corpus-rows.ts';
import type { TaskDetail } from './types.ts';

/** How the queue is being read right now. */
export type QueueView = 'list' | 'grid';

export const QUEUE_VIEW_KEY = 'workbench-queue-view';

/** Parse a stored view mode. Anything unrecognised is `list` — the mode that works for every corpus,
 *  including the ones with no imagery to put in a grid. */
export function parseView(raw: string | null): QueueView {
	return raw === 'grid' ? 'grid' : 'list';
}

/**
 * Which cell shows a task, as the viewer's signed-media door names it: the dataset the item came from and its chunk's
 * frame (the key path after the doc key), or the document's thumbnail when the key names a document alone. `null` when
 * the item names no dataset or no key: its tile shows the media type's glyph rather than a broken image. A multi-key
 * item shows its first key.
 */
export function taskMediaItem(task: TaskDetail): { dataset: string; item: MediaItem } | null {
	const dataset = task.source.where;
	const key = rowKeyFor(task.source.keys);
	if (!dataset || key === null) return null;
	const item: MediaItem =
		key.keys.length === 0
			? { role: 'thumbnail', doc_id: key.doc_id }
			: { role: 'frame', doc_id: key.doc_id, keys: key.keys, frame_idx: 0 };
	return { dataset, item };
}

/**
 * A one-line caption for a card — what the tile says about the item beyond its picture.
 *
 * The key alone is unreadable at card size (`fe00cd746463ad2c/1/12`), so this leads with the part
 * that varies within a document and keeps the whole key as the tooltip. Deliberately NOT the label:
 * the label is rendered as its own badge, because a suggested class must be visually distinct from
 * the item's identity.
 */
export function cardCaption(task: TaskDetail): string {
	const key = task.source.keys[0] ?? task.task_id;
	const parts = key.split('/');
	return parts.length > 1 ? parts.slice(1).join('/') : key;
}
