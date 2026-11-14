/**
 * A task's picture: the signed link to the cell `taskMediaItem` names, from the page's one batched loader. Undefined
 * while it is being signed, and when the item names no cell or the viewer signs none for it, which the tile shows as the
 * media type's glyph.
 */
import { mediaLinks } from '#lib/api/media.api.ts';
import { taskMediaItem } from './queue-view.ts';
import type { TaskDetail } from './types.ts';

export function taskImageUrl(task: TaskDetail): string | undefined {
	const address = taskMediaItem(task);
	return address === null
		? undefined
		: (mediaLinks.link(address.dataset, address.item)?.url ?? undefined);
}
